"""The wiki's data store — one SQLite file per wiki (`<chat_dir>/store.db`).

Three tables, nothing speculative:

- `items` — the universal source primitive (messages, document paragraphs),
  keyed by the global id space, with FTS5 full-text search over the text.
- `access_log` — the audit trail: every access through every channel (MCP
  client, CLI, future HTTP) records the tool, the arguments, a summary of what
  was returned, and how long it took. Summaries, never payloads — auditable
  without bloat. The foundation for scoped access grants later.

The wiki pages themselves stay as markdown on disk — readable, git-syncable —
this file is the substrate: portable (one file IS the backup), indexed, and
serveable anywhere with zero extra dependencies (sqlite3 is stdlib)."""
from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime
from pathlib import Path

from sources.imessage.db import Message

_SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id       INTEGER PRIMARY KEY,          -- global id space (citations)
    ts       TEXT NOT NULL,
    sender   TEXT NOT NULL,
    text     TEXT NOT NULL,
    system   TEXT,
    extra    TEXT                          -- json: attachments, reactions
);
CREATE VIRTUAL TABLE IF NOT EXISTS items_fts USING fts5(
    text, content='items', content_rowid='id', tokenize='porter unicode61');
"""

_LOG_SCHEMA = """
CREATE TABLE IF NOT EXISTS access_log (
    ts        TEXT NOT NULL,
    channel   TEXT NOT NULL,               -- e.g. mcp/claude-ai, cli
    tool      TEXT NOT NULL,
    args      TEXT,                        -- json summary, truncated
    returned  TEXT,                        -- summary: counts, ids, page ids
    ms        INTEGER
);
CREATE INDEX IF NOT EXISTS idx_access_ts ON access_log(ts);
"""


def open_db(chat_dir) -> sqlite3.Connection:
    path = Path(chat_dir) / "store.db"
    con = sqlite3.connect(path, check_same_thread=False)
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(_SCHEMA)
    return con


def open_log_db(chat_dir) -> sqlite3.Connection:
    """The audit trail lives in its own file so log writes never perturb the
    item store (or its mtime-keyed caches)."""
    con = sqlite3.connect(Path(chat_dir) / "log.db", check_same_thread=False)
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(_LOG_SCHEMA)
    return con


def import_items(chat_dir, msgs) -> int:
    """Fold this message set into the store. Append-forever: existing rows are
    updated by id, rows from sources no longer selected are KEPT — the store is
    the archive of everything ever imported, so citations resolve even after a
    source platform or export is gone."""
    con = open_db(chat_dir)
    with con:
        con.executemany(
            "INSERT OR REPLACE INTO items VALUES (?,?,?,?,?,?)",
            ((m.rowid, m.ts.isoformat(), m.sender, m.text, m.system,
              json.dumps({"a": m.attachments, "p": m.attachment_paths,
                          "r": m.reactions})) for m in msgs))
        con.execute("INSERT INTO items_fts(items_fts) VALUES('rebuild')")
    n = con.execute("SELECT count(*) FROM items").fetchone()[0]
    con.close()
    return n


def _row_to_message(row) -> Message:
    rid, ts, sender, text, system, extra = row
    e = json.loads(extra or "{}")
    return Message(ts=datetime.fromisoformat(ts), sender=sender, text=text,
                   is_from_me=False, rowid=rid, system=system,
                   attachments=e.get("a", []), attachment_paths=e.get("p", []),
                   reactions=e.get("r", {}))


def load_items(chat_dir):
    """All items as Message objects, chronological; None if the store is empty."""
    path = Path(chat_dir) / "store.db"
    if not path.exists():
        return None
    con = open_db(chat_dir)
    rows = con.execute("SELECT * FROM items ORDER BY ts").fetchall()
    con.close()
    return [_row_to_message(r) for r in rows] or None


def fts_search(chat_dir, query, limit=40, since="", until=""):
    """BM25-ranked full-text search over items; returns Message objects."""
    con = open_db(chat_dir)
    sql = ("SELECT items.* FROM items_fts JOIN items ON items.id = items_fts.rowid "
           "WHERE items_fts MATCH ?")
    args = [query]
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


def log_access(chat_dir, channel, tool, args, returned, ms) -> None:
    """One audit row. Args/returned are truncated summaries, never payloads."""
    try:
        con = open_log_db(chat_dir)
        with con:
            con.execute("INSERT INTO access_log VALUES (?,?,?,?,?,?)",
                        (time.strftime("%Y-%m-%d %H:%M:%S"), channel, tool,
                         json.dumps(args, ensure_ascii=False)[:400],
                         str(returned)[:400], int(ms)))
        con.close()
    except Exception:
        pass                                # auditing must never break serving


def read_log(chat_dir, tail=30) -> list:
    path = Path(chat_dir) / "log.db"
    if not path.exists():
        return []
    con = open_log_db(chat_dir)
    rows = con.execute("SELECT * FROM access_log ORDER BY ts DESC, rowid DESC LIMIT ?",
                       (tail,)).fetchall()
    con.close()
    return rows[::-1]


def item(chat_dir, item_id):
    """One item by id, or None."""
    con = open_db(chat_dir)
    row = con.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    con.close()
    return _row_to_message(row) if row else None


def item_window(chat_dir, item_id, context=4):
    """The item plus its chronological neighbors (same conversation flow)."""
    con = open_db(chat_dir)
    row = con.execute("SELECT ts FROM items WHERE id = ?", (item_id,)).fetchone()
    if not row:
        con.close()
        return []
    before = con.execute(
        "SELECT * FROM items WHERE ts <= ? AND id != ? ORDER BY ts DESC, id DESC LIMIT ?",
        (row[0], item_id, context)).fetchall()[::-1]
    target = con.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    after = con.execute(
        "SELECT * FROM items WHERE ts >= ? AND id != ? ORDER BY ts, id LIMIT ?",
        (row[0], item_id, context)).fetchall()
    con.close()
    return [_row_to_message(r) for r in before + [target] + after]


def max_ts(chat_dir, ids=None):
    """Newest item timestamp — overall, or among specific ids."""
    con = open_db(chat_dir)
    if ids:
        marks = ",".join("?" * len(ids))
        row = con.execute(f"SELECT max(ts) FROM items WHERE id IN ({marks})",
                          list(ids)).fetchone()
    else:
        row = con.execute("SELECT max(ts) FROM items").fetchone()
    con.close()
    return datetime.fromisoformat(row[0]) if row and row[0] else None
