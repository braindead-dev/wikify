"""Read an unpacked Instagram data export into the shared Message primitive.

Layout: <root>/your_instagram_activity/messages/inbox/<thread>_<id>/message_N.json
with media files alongside (photos/, videos/, audio/). Export strings are
mojibake (UTF-8 bytes decoded as latin-1) — fixed on read.

Instagram messages have no native row ids; each selected thread set gets stable
synthetic ids from ID_BASE upward, assigned in chronological order.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ..imessage.db import Message

DEFAULT_ROOT = Path("data/instagram")
ID_BASE = 50_000_000                     # keeps synthetic ids clear of chat.db rowids


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
    def __init__(self, root=None):
        self.root = Path(root) if root else DEFAULT_ROOT
        self.inbox = self.root / "your_instagram_activity" / "messages" / "inbox"
        if not self.inbox.exists():
            raise FileNotFoundError(
                f"no Instagram export at {self.inbox} — unzip the official "
                "'Download Your Information' (JSON) export there")

    def _pages(self, key):
        return sorted((self.inbox / key).glob("message_*.json"),
                      key=lambda p: int(p.stem.split("_")[1]))

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
                participants=[_fix(p["name"]) for p in first_page.get("participants", [])],
                message_count=count,
                first=datetime.fromtimestamp(min(stamps) / 1000) if stamps else None,
                last=datetime.fromtimestamp(max(stamps) / 1000) if stamps else None))
        out.sort(key=lambda t: -t.message_count)
        return out

    def messages(self, thread_keys, until=None) -> list:
        raw = []
        for key in thread_keys:
            for page in self._pages(key):
                raw.extend(json.loads(page.read_text()).get("messages", []))
        raw.sort(key=lambda m: m["timestamp_ms"])
        out = []
        for m in raw:
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
                reactions.setdefault(_fix(r.get("reaction", "")), []).append(_fix(r.get("actor", "")))
            out.append(Message(ts=ts, sender=_fix(m.get("sender_name", "")), text=text,
                               is_from_me=False, attachments=tags, attachment_paths=paths,
                               reactions=reactions))
        for i, m in enumerate(out):          # stable synthetic ids, chronological
            m.rowid = ID_BASE + i
        return out
