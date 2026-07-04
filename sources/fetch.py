"""One loader over every source: give it qualified chat specs, get back a single
chronological message stream.

    fetch(["512", "519", "ig:bookclub_123"])  →  (messages, imessage_db_or_None)

A bare number is an iMessage chat row; `ig:<thread>` is an Instagram export
thread. Message ids never collide (Instagram uses a reserved synthetic range),
so citations stay plain `[#id]` everywhere downstream.
"""
from __future__ import annotations

from pathlib import Path

from .imessage import MessagesDB
from .instagram import InstagramExport


def _identities():
    return "identities.json" if Path("identities.json").exists() else None


def parse_specs(specs) -> tuple:
    """Split mixed chat specs into (imessage row ids, instagram thread keys)."""
    im_ids, ig_keys = [], []
    for spec in specs:
        s = str(spec).strip()
        if s.startswith("ig:"):
            ig_keys.append(s[3:])
        elif s.isdigit():
            im_ids.append(int(s))
        else:
            raise ValueError(f"unknown chat spec {s!r} — use a chat row id or ig:<thread>")
    return im_ids, ig_keys


def fetch(specs, until=None):
    """All messages for the given specs, merged and sorted by time. Returns
    (messages, db) where db is the iMessage database when any iMessage chat is
    selected (some callers use it for the contact directory), else None."""
    im_ids, ig_keys = parse_specs(specs)
    msgs, db = [], None
    if im_ids:
        db = MessagesDB(identities=_identities())
        msgs += db.messages(im_ids, until=until)
    if ig_keys:
        msgs += InstagramExport().messages(ig_keys, until=until)
    msgs.sort(key=lambda m: m.ts)
    return msgs, db
