"""Read-only access to the local iMessage database (chat.db).

Exposes the real entities faithfully:
    MessagesDB.chats()   -> one Chat per chat row (no auto-merging)
    MessagesDB.handles() -> one Handle per stored endpoint (phone/email)
    MessagesDB.messages(chat_ids) -> messages across the given chat rows,
        with reactions, replies, attachments, edits and system events folded
        in the way iMessage actually models them.

Merging chats is something you do explicitly by passing several chat ids to
messages()/export(). It is never inferred.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .identity import Resolver, load_contacts, load_identities

DEFAULT_DB = Path.home() / "Library" / "Messages" / "chat.db"
APPLE_EPOCH = 978307200          # seconds between 2001-01-01 and the unix epoch
OBJ_REPLACEMENT = "￼"       # placeholder char iMessage uses for attachments

# associated_message_type -> tapback label
TAPBACKS = {2000: "Loved", 2001: "Liked", 2002: "Disliked",
            2003: "Laughed", 2004: "Emphasized", 2005: "Questioned"}


@dataclass
class Chat:
    rowid: int
    guid: str
    identifier: str
    display_name: str
    kind: str                     # "group" or "dm"
    service: str
    participants: list            # raw handle values
    participant_names: list       # resolved names
    message_count: int
    first: datetime | None
    last: datetime | None

    @property
    def title(self) -> str:
        return self.display_name or ", ".join(self.participant_names) or "(unknown)"


@dataclass
class Handle:
    rowid: int
    value: str                    # phone/email as stored
    name: str                     # resolved display name
    message_count: int
    chat_ids: list


@dataclass
class Message:
    ts: datetime
    sender: str
    text: str
    is_from_me: bool
    rowid: int = 0                                     # message ROWID (exact watermark)
    attachments: list = field(default_factory=list)   # e.g. ["img", "video"]
    reply_to: str | None = None                        # snippet of the parent
    edited: bool = False
    system: str | None = None                          # rename/add/remove/leave
    reactions: dict = field(default_factory=dict)      # label -> [names]
    guid: str | None = None


def _to_dt(date) -> datetime:
    secs = date / 1e9 if date > 1e11 else date         # ns on modern macOS
    return datetime.fromtimestamp(secs + APPLE_EPOCH)


def _decode_attributed_body(blob) -> str:
    """Extract the message text from an NSAttributedString typedstream blob.

    Newer iMessages leave the `text` column NULL and store the string here.
    """
    if not blob:
        return ""
    i = blob.find(b"NSString")
    if i < 0:
        return ""
    p = i + 8
    plus = blob.find(b"\x2b", p, p + 12)               # class marker, then length
    p = plus + 1 if plus >= 0 else p + 5
    if p >= len(blob):
        return ""
    first = blob[p]
    p += 1
    if first == 0x81:
        length = int.from_bytes(blob[p:p + 2], "little"); p += 2
    elif first == 0x82:
        length = int.from_bytes(blob[p:p + 4], "little"); p += 4
    else:
        length = first
    return blob[p:p + length].decode("utf-8", "replace")


def _attachment_tag(mime: str) -> str:
    mime = mime or ""
    if mime.startswith("image/gif"):
        return "gif"
    if mime.startswith("image/"):
        return "img"
    if mime.startswith("video/"):
        return "video"
    if mime.startswith("audio/"):
        return "audio"
    if "/" in mime:
        return mime.split("/")[-1]
    return "attachment"


def _strip_associated_prefix(guid: str | None) -> str | None:
    if not guid:
        return None
    return guid.split("/")[-1].split(":")[-1]


class MessagesDB:
    """Open chat.db read-only and read entities from it.

        with MessagesDB() as db:
            for c in db.chats():
                ...
    """

    def __init__(self, path=None, contacts=True, identities=None):
        self.path = Path(path) if path else DEFAULT_DB
        if not self.path.exists():
            raise FileNotFoundError(f"chat.db not found at {self.path}")
        try:
            self._con = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
            self._con.execute("SELECT 1 FROM chat LIMIT 1")
        except sqlite3.OperationalError as exc:
            raise PermissionError(
                "Cannot read chat.db. Grant your terminal Full Disk Access "
                "(System Settings > Privacy & Security > Full Disk Access)."
            ) from exc
        cmap = load_contacts() if contacts else {}
        ident = identities if isinstance(identities, dict) else load_identities(identities)
        self.resolver = Resolver(cmap, ident)

    # -- lifecycle ---------------------------------------------------------
    def close(self):
        self._con.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # -- entities ----------------------------------------------------------
    def chats(self) -> list[Chat]:
        """Every chat row, exactly as stored, newest-active first by msg count."""
        cur = self._con.cursor()
        rows = {rid: (name, ident, style, svc) for rid, name, ident, style, svc
                in cur.execute("SELECT ROWID, display_name, chat_identifier, "
                               "style, service_name FROM chat")}
        parts: dict = {}
        for cid, value in cur.execute(
                "SELECT j.chat_id, h.id FROM chat_handle_join j "
                "JOIN handle h ON h.ROWID = j.handle_id"):
            parts.setdefault(cid, []).append(value)
        stats = {cid: (n, mn, mx) for cid, n, mn, mx in cur.execute(
            "SELECT cmj.chat_id, COUNT(*), MIN(m.date), MAX(m.date) "
            "FROM message m JOIN chat_message_join cmj ON cmj.message_id = m.ROWID "
            "GROUP BY cmj.chat_id")}

        chats = []
        for rid, (name, ident, style, svc) in rows.items():
            n, mn, mx = stats.get(rid, (0, None, None))
            if not n:
                continue
            members = parts.get(rid, [])
            chats.append(Chat(
                rowid=rid, guid="", identifier=ident, display_name=(name or "").strip(),
                kind="group" if style == 43 else "dm", service=svc,
                participants=members,
                participant_names=[self.resolver.name(v) for v in members],
                message_count=n,
                first=_to_dt(mn) if mn else None, last=_to_dt(mx) if mx else None))
        chats.sort(key=lambda c: -c.message_count)
        return chats

    def handles(self) -> list[Handle]:
        """Every stored endpoint (phone/email), with its message count."""
        cur = self._con.cursor()
        counts = {rid: n for rid, n in cur.execute(
            "SELECT handle_id, COUNT(*) FROM message GROUP BY handle_id")}
        chat_ids: dict = {}
        for hid, cid in cur.execute("SELECT handle_id, chat_id FROM chat_handle_join"):
            chat_ids.setdefault(hid, []).append(cid)
        out = []
        for rid, value in cur.execute("SELECT ROWID, id FROM handle"):
            out.append(Handle(rowid=rid, value=value, name=self.resolver.name(value),
                              message_count=counts.get(rid, 0),
                              chat_ids=chat_ids.get(rid, [])))
        out.sort(key=lambda h: -h.message_count)
        return out

    def group(self, name: str) -> list[int]:
        """Chat rowids for a named group defined in identities.json ('groups')."""
        ids = self.resolver.groups.get(name)
        if ids is None:
            raise KeyError(f"no group named {name!r} in identities.json")
        return list(ids)

    def max_message_id(self, chat_ids) -> int:
        """Highest message ROWID across the given chats — an exact watermark."""
        chat_ids = [chat_ids] if isinstance(chat_ids, int) else list(chat_ids)
        placeholders = ",".join("?" * len(chat_ids))
        row = self._con.execute(
            f"SELECT MAX(m.ROWID) FROM message m "
            f"JOIN chat_message_join cmj ON cmj.message_id = m.ROWID "
            f"WHERE cmj.chat_id IN ({placeholders})", chat_ids).fetchone()
        return row[0] or 0

    def messages(self, chat_ids, since=None, after_id=None) -> list[Message]:
        """Messages across the given chat rows, in time order.

        Pass one id for a single chat, or several to merge them (e.g. a group
        that was re-created under a new id, or iMessage + SMS copies).
        `since` (a datetime) filters by time; `after_id` (a ROWID) filters
        exactly — everything strictly newer than that message.
        """
        chat_ids = [chat_ids] if isinstance(chat_ids, int) else list(chat_ids)
        cur = self._con.cursor()
        placeholders = ",".join("?" * len(chat_ids))
        where = f"cmj.chat_id IN ({placeholders})"
        params = list(chat_ids)
        if since is not None:
            where += " AND m.date > ?"
            params.append(int((since.timestamp() - APPLE_EPOCH) * 1e9))
        if after_id is not None:
            where += " AND m.ROWID > ?"
            params.append(after_id)

        attachments: dict = {}
        for mid, mime in cur.execute(
                f"SELECT maj.message_id, a.mime_type FROM attachment a "
                f"JOIN message_attachment_join maj ON maj.attachment_id = a.ROWID "
                f"JOIN chat_message_join cmj ON cmj.message_id = maj.message_id "
                f"WHERE cmj.chat_id IN ({placeholders})", chat_ids):
            attachments.setdefault(mid, []).append(_attachment_tag(mime))

        handle_value = {rid: value for rid, value
                        in cur.execute("SELECT ROWID, id FROM handle")}

        rows = cur.execute(
            f"SELECT m.ROWID, m.guid, m.date, m.date_edited, m.is_from_me, h.id, "
            f"m.text, m.attributedBody, m.associated_message_type, "
            f"m.associated_message_guid, m.thread_originator_guid, m.item_type, "
            f"m.group_action_type, m.group_title, m.other_handle "
            f"FROM message m "
            f"JOIN chat_message_join cmj ON cmj.message_id = m.ROWID "
            f"LEFT JOIN handle h ON h.ROWID = m.handle_id "
            f"WHERE {where} "
            f"ORDER BY m.date ASC, m.ROWID ASC", params).fetchall()

        reaction_state: dict = {}     # (target_guid, reactor) -> label or None
        messages: list[Message] = []
        index_by_guid: dict = {}
        snippet_by_guid: dict = {}

        for (rid, guid, date, edited, is_me, hid, text, body_blob, assoc_type,
             assoc_guid, thread_guid, item_type, action, group_title, other) in rows:
            sender = self.resolver.sender(is_me, hid)
            text = (text if (text and len(text) > 0) else _decode_attributed_body(body_blob)) or ""
            text = text.replace(OBJ_REPLACEMENT, "").strip()

            if assoc_type:            # a tapback/reaction — fold onto its target
                target = _strip_associated_prefix(assoc_guid)
                if target is None:
                    continue
                if assoc_type >= 3000:
                    reaction_state[(target, sender)] = None        # reaction removed
                elif assoc_type in TAPBACKS:
                    reaction_state[(target, sender)] = TAPBACKS[assoc_type]
                else:
                    reaction_state[(target, sender)] = text[:8] or "Reacted"
                continue

            tags = attachments.get(rid, [])
            system = None
            if item_type != 0:
                other_name = self.resolver.sender(False, handle_value.get(other)) if other else ""
                if item_type == 2:
                    system = f'named the group "{group_title}"'
                elif item_type == 1:
                    system = ("added " if action == 0 else "removed ") + (other_name or "a participant")
                elif item_type == 3:
                    system = "left the conversation"
                else:
                    system = f"group event (type {item_type})"
            elif not text and not tags:
                continue              # empty noise (e.g. bare placeholder)

            g = _strip_associated_prefix(guid)
            if g and text:
                snippet_by_guid[g] = text[:40]
            msg = Message(ts=_to_dt(date), sender=sender, text=text, is_from_me=bool(is_me),
                          rowid=rid, attachments=tags, reply_to=_strip_associated_prefix(thread_guid),
                          edited=bool(edited), system=system, guid=g)
            if g is not None:
                index_by_guid[g] = len(messages)
            messages.append(msg)

        for (target, reactor), label in reaction_state.items():
            if label and target in index_by_guid:
                messages[index_by_guid[target]].reactions.setdefault(label, []).append(reactor)
        for msg in messages:
            parent = msg.reply_to
            msg.reply_to = snippet_by_guid.get(parent) if (parent and parent != msg.guid) else None

        return messages

    def message(self, rowid, context=0) -> list[Message]:
        """Resolve a citation: the message with this ROWID, plus `context`
        messages on each side (same chat). Raises KeyError if it isn't a
        standalone message (e.g. it's a folded-in reaction)."""
        row = self._con.execute(
            "SELECT chat_id FROM chat_message_join WHERE message_id = ?", (rowid,)).fetchone()
        if row is None:
            raise KeyError(f"no message with rowid {rowid}")
        msgs = self.messages(row[0])
        idx = next((i for i, m in enumerate(msgs) if m.rowid == rowid), None)
        if idx is None:
            raise KeyError(f"message {rowid} is not a standalone message (reaction/empty)")
        return msgs[max(0, idx - context): idx + context + 1]

    def export(self, chat_ids, fmt="txt", title=None, ids=False, header=False):
        """Render the given chats as a transcript string (txt) or dict (json)."""
        return self.render(chat_ids, fmt, title, ids=ids, header=header)[0]

    def render(self, chat_ids, fmt="txt", title=None, ids=False, header=False):
        """Like export(), but also returns (payload, meta, title) for callers
        that need the metadata without re-parsing the output."""
        from . import render as renderer
        chat_ids = [chat_ids] if isinstance(chat_ids, int) else list(chat_ids)
        msgs = self.messages(chat_ids)
        by_id = {c.rowid: c for c in self.chats()}
        if title is None:
            names = [by_id[c].title for c in chat_ids if c in by_id]
            title = names[0] if len(set(names)) == 1 else " + ".join(dict.fromkeys(names))
        body = [m for m in msgs if not m.system]
        ts = lambda m: m.ts.strftime("%Y-%m-%d %H:%M:%S")
        meta = {
            "participants": sorted({n for c in chat_ids if c in by_id
                                    for n in by_id[c].participant_names}),
            "message_count": len(body),
            "first_message": ts(body[0]) if body else None,
            "last_message": ts(body[-1]) if body else None,
            "chat_rowids": list(chat_ids),
        }
        payload = renderer.as_json(title, meta, msgs) if fmt == "json" \
            else renderer.as_txt(title, meta, msgs, ids=ids, header=header)
        return payload, meta, title
