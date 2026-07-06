"""The archive — ONE global SQLite file (`wikis/archive.db`) holding every
item ever imported from any source, plus the global access audit log
(`wikis/log.db`).

Archive once, compile many: sources are imported a single time, tagged with
their canonical spec (`src`), and every wiki is a pure SCOPE over the archive
— `src IN (the wiki's sources)`. Append-forever: items are upserted by id and
never deleted, so citations resolve even after a source platform is gone, and
a new wiki over existing sources costs zero re-import.

The wiki pages stay as markdown on disk — readable, git-syncable — the archive
is the substrate: portable (one file IS the backup), FTS5-indexed, serveable
anywhere with zero extra dependencies (sqlite3 is stdlib)."""
from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime
from pathlib import Path

from sources.imessage.db import Message

ARCHIVE = Path("wikis/archive.db")
LOG = Path("wikis/log.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id       INTEGER PRIMARY KEY,          -- global id space (citations)
    src      TEXT NOT NULL,                -- canonical source spec (the scope key)
    ts       TEXT NOT NULL,
    sender   TEXT NOT NULL,
    text     TEXT NOT NULL,
    system   TEXT,
    extra    TEXT                          -- json: attachments, reactions
);
CREATE INDEX IF NOT EXISTS idx_items_src ON items(src);
CREATE VIRTUAL TABLE IF NOT EXISTS items_fts USING fts5(
    text, content='items', content_rowid='id', tokenize='porter unicode61');
"""

_LOG_SCHEMA = """
CREATE TABLE IF NOT EXISTS access_log (
    ts        TEXT NOT NULL,
    wiki      TEXT NOT NULL,               -- which scope was accessed
    channel   TEXT NOT NULL,               -- e.g. mcp, mcp/grant:<name>, cli
    tool      TEXT NOT NULL,
    args      TEXT,                        -- json summary, truncated
    returned  TEXT,                        -- summary: counts, ids, page ids
    ms        INTEGER
);
CREATE INDEX IF NOT EXISTS idx_access_ts ON access_log(ts);
"""


def _scope(specs):
    """WHERE clause + params limiting a query to a wiki's sources."""
    from sources.fetch import canonical_spec
    canon = [canonical_spec(x) for x in specs]
    return f"src IN ({','.join('?' * len(canon))})", canon


def open_db() -> sqlite3.Connection:
    ARCHIVE.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(ARCHIVE, check_same_thread=False)
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(_SCHEMA)
    return con


def open_log_db() -> sqlite3.Connection:
    """The audit trail lives in its own file so log writes never perturb the
    archive (or its mtime-keyed caches)."""
    con = sqlite3.connect(LOG, check_same_thread=False)
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(_LOG_SCHEMA)
    return con


def import_items(msgs) -> int:
    """Fold items into the global archive. Append-forever: upserted by id,
    never deleted — the archive holds everything ever imported."""
    con = open_db()
    with con:
        con.executemany(
            "INSERT OR REPLACE INTO items VALUES (?,?,?,?,?,?,?)",
            ((m.rowid, m.src, m.ts.isoformat(), m.sender, m.text, m.system,
              json.dumps({"a": m.attachments, "p": m.attachment_paths,
                          "r": m.reactions})) for m in msgs))
        con.execute("INSERT INTO items_fts(items_fts) VALUES('rebuild')")
    n = con.execute("SELECT count(*) FROM items").fetchone()[0]
    con.close()
    return n


def _row_to_message(row) -> Message:
    rid, src, ts, sender, text, system, extra = row
    e = json.loads(extra or "{}")
    return Message(ts=datetime.fromisoformat(ts), sender=sender, text=text,
                   is_from_me=False, rowid=rid, system=system, src=src,
                   attachments=e.get("a", []), attachment_paths=e.get("p", []),
                   reactions=e.get("r", {}))


def load_items(specs):
    """A wiki's items (its scope over the archive), chronological; None if the
    archive has nothing for this scope."""
    if not ARCHIVE.exists():
        return None
    con = open_db()
    where, params = _scope(specs)
    rows = con.execute(f"SELECT * FROM items WHERE {where} ORDER BY ts",
                       params).fetchall()
    con.close()
    return [_row_to_message(r) for r in rows] or None


def iter_items(specs):
    """Memory-bounded iteration over a scope (for full scans at any size)."""
    con = open_db()
    where, params = _scope(specs)
    try:
        for row in con.execute(f"SELECT * FROM items WHERE {where} ORDER BY ts", params):
            yield _row_to_message(row)
    finally:
        con.close()


def fts_search(specs, query, limit=40, since="", until=""):
    """BM25-ranked full-text search over a scope; returns Message objects."""
    con = open_db()
    where, params = _scope(specs)
    sql = ("SELECT items.* FROM items_fts JOIN items ON items.id = items_fts.rowid "
           f"WHERE items_fts MATCH ? AND {where}")
    args = [query] + params
    if since:
        sql += " AND items.ts >= ?"
        args.append(since)
    if until:
        sql += " AND items.ts <= ?"
        args.append(until + "~")
    sql += " ORDER BY rank LIMIT ?"
    args.append(limit)
    try:
        rows = con.execute(sql, args).fetchall()
    finally:
        con.close()
    return [_row_to_message(r) for r in rows]


def log_access(wiki, channel, tool, args, returned, ms) -> None:
    """One audit row. Args/returned are truncated summaries, never payloads."""
    try:
        con = open_log_db()
        with con:
            con.execute("INSERT INTO access_log VALUES (?,?,?,?,?,?,?)",
                        (time.strftime("%Y-%m-%d %H:%M:%S"), str(wiki), channel, tool,
                         json.dumps(args, ensure_ascii=False)[:400],
                         str(returned)[:400], int(ms)))
        con.close()
    except Exception:
        pass                                # auditing must never break serving


def read_log(wiki=None, tail=30) -> list:
    if not LOG.exists():
        return []
    con = open_log_db()
    if wiki:
        rows = con.execute("SELECT * FROM access_log WHERE wiki = ? "
                           "ORDER BY ts DESC, rowid DESC LIMIT ?", (str(wiki), tail)).fetchall()
    else:
        rows = con.execute("SELECT * FROM access_log ORDER BY ts DESC, rowid DESC LIMIT ?",
                           (tail,)).fetchall()
    con.close()
    return rows[::-1]


def item(specs, item_id):
    """One item by id within a scope, or None."""
    con = open_db()
    where, params = _scope(specs)
    row = con.execute(f"SELECT * FROM items WHERE id = ? AND {where}",
                      [item_id] + params).fetchone()
    con.close()
    return _row_to_message(row) if row else None


def item_window(specs, item_id, context=4):
    """The item plus its chronological neighbors within the same SOURCE (a
    conversation's flow never interleaves other channels)."""
    con = open_db()
    where, params = _scope(specs)
    target = con.execute(f"SELECT * FROM items WHERE id = ? AND {where}",
                         [item_id] + params).fetchone()
    if not target:
        con.close()
        return []
    src, ts = target[1], target[2]
    before = con.execute(
        "SELECT * FROM items WHERE src = ? AND ts <= ? AND id != ? "
        "ORDER BY ts DESC, id DESC LIMIT ?", (src, ts, item_id, context)).fetchall()[::-1]
    after = con.execute(
        "SELECT * FROM items WHERE src = ? AND ts >= ? AND id != ? "
        "ORDER BY ts, id LIMIT ?", (src, ts, item_id, context)).fetchall()
    con.close()
    return [_row_to_message(r) for r in before + [target] + after]


def max_ts(specs, ids=None):
    """Newest item timestamp in a scope — overall, or among specific ids."""
    con = open_db()
    where, params = _scope(specs)
    if ids:
        marks = ",".join("?" * len(ids))
        row = con.execute(f"SELECT max(ts) FROM items WHERE id IN ({marks}) AND {where}",
                          list(ids) + params).fetchone()
    else:
        row = con.execute(f"SELECT max(ts) FROM items WHERE {where}", params).fetchone()
    con.close()
    return datetime.fromisoformat(row[0]) if row and row[0] else None
