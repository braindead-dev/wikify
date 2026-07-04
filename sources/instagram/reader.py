"""Read an unpacked Instagram data export into the shared Message primitive.

Layout: <root>/your_instagram_activity/messages/inbox/<thread>_<id>/message_N.json
with media files alongside (photos/, videos/, audio/). Export strings are
mojibake (UTF-8 bytes decoded as latin-1) — fixed on read.

Instagram messages have no native row ids; each selected thread set gets stable
synthetic ids from ID_BASE upward, assigned in chronological order.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ..imessage.db import Message
from ..imessage.identity import load_identities

DEFAULT_ROOT = Path("data/instagram")
ID_BASE = 50_000_000                     # keeps synthetic ids clear of chat.db rowids


def thread_block(key: str) -> int:
    """Each thread owns a stable million-id block (hash-based, so adding or
    removing other threads never shifts a thread's ids)."""
    return ID_BASE + (int(hashlib.sha1(key.encode()).hexdigest(), 16) % 100_000) * 1_000_000


def _fix(s: str) -> str:
    """Undo the export's mojibake (UTF-8 bytes stored as latin-1 text)."""
    try:
        return s.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return s


@dataclass
class Thread:
    key: str                             # inbox directory name, e.g. bookclub_123
    title: str
    participants: list
    message_count: int
    first: datetime | None
    last: datetime | None


class InstagramExport:
    def __init__(self, root=None, identities="identities.json"):
        self.root = Path(root) if root else DEFAULT_ROOT
        self.inbox = self.root / "your_instagram_activity" / "messages" / "inbox"
        if not self.inbox.exists():
            raise FileNotFoundError(
                f"no Instagram export at {self.inbox} — unzip the official "
                "'Download Your Information' (JSON) export there")
        # display-name → canonical participant label, from identities.json
        # ("sources": {"instagram": {...}}), so the same human resolves to the
        # same name across every source
        ident = load_identities(identities if Path(str(identities)).exists() else None)
        self.names = ident.get("sources", {}).get("instagram", {})

    def _name(self, s: str) -> str:
        fixed = _fix(s)
        return self.names.get(fixed, fixed)

    def _pages(self, key):
        return sorted((self.inbox / key).glob("message_*.json"),
                      key=lambda p: int(p.stem.split("_")[1]))

    def title(self, key) -> str:
        pages = self._pages(key)
        if not pages:
            return key
        return _fix(json.loads(pages[0].read_text()).get("title", key))

    def threads(self) -> list:
        out = []
        for d in sorted(self.inbox.iterdir()):
            pages = self._pages(d.name)
            if not pages:
                continue
            first_page = json.loads(pages[0].read_text())
            stamps, count = [], 0
            for page in pages:
                msgs = json.loads(page.read_text()).get("messages", [])
                count += len(msgs)
                stamps += [msgs[0]["timestamp_ms"], msgs[-1]["timestamp_ms"]] if msgs else []
            out.append(Thread(
                key=d.name, title=_fix(first_page.get("title", d.name)),
                participants=[self._name(p["name"]) for p in first_page.get("participants", [])],
                message_count=count,
                first=datetime.fromtimestamp(min(stamps) / 1000) if stamps else None,
                last=datetime.fromtimestamp(max(stamps) / 1000) if stamps else None))
        out.sort(key=lambda t: -t.message_count)
        return out

    def thread_messages(self, key, until=None) -> list:
        """One thread's messages, chronological, with stable ids in the thread's
        own hash block (ids never shift when other threads are added)."""
        raw = []
        for page in self._pages(key):
            raw.extend(json.loads(page.read_text()).get("messages", []))
        raw.sort(key=lambda m: m["timestamp_ms"])
        base, out = thread_block(key), []
        for i, m in enumerate(raw):
            ts = datetime.fromtimestamp(m["timestamp_ms"] / 1000)
            if until and ts >= until:
                continue
            text = _fix(m.get("content", "") or "")
            share = m.get("share") or {}
            if share:
                extra = _fix(share.get("share_text") or share.get("link") or "")[:200]
                if extra:
                    text = (text + " " if text else "") + f"[share: {extra}]"
            tags, paths = [], []
            for kind, tag in (("photos", "img"), ("videos", "video"), ("audio_files", "audio")):
                for a in m.get(kind, []):
                    tags.append(tag)
                    paths.append(str(self.root / a["uri"]) if a.get("uri") else "")
            reactions = {}
            for r in m.get("reactions", []):
                reactions.setdefault(_fix(r.get("reaction", "")), []).append(
                    self._name(r.get("actor", "")))
            out.append(Message(ts=ts, sender=self._name(m.get("sender_name", "")), text=text,
                               is_from_me=False, rowid=base + i, attachments=tags,
                               attachment_paths=paths, reactions=reactions))
        return out

    def messages(self, thread_keys, until=None) -> list:
        blocks = {thread_block(k) for k in thread_keys}
        if len(blocks) != len(set(thread_keys)):
            raise ValueError("selected threads collide in the id space — "
                             "rename one thread directory to disambiguate")
        out = []
        for key in thread_keys:
            out += self.thread_messages(key, until=until)
        out.sort(key=lambda m: m.ts)
        return out
