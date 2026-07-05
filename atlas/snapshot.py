"""Message snapshot — the wiki's own copy of its source record.

Extraction already reads every message; `save_snapshot` persists them to
`<chat_dir>/messages.json.gz` so everything downstream (the MCP server, render,
audits) serves from the artifact instead of re-opening live sources. That
removes the Full Disk Access requirement for GUI-spawned servers, pins
citations to exactly what was extracted, and makes a wiki fully portable —
it keeps working even if the source platform or machine is gone.
"""
from __future__ import annotations

import gzip
import json
from datetime import datetime
from pathlib import Path

from sources.imessage.db import Message

_NAME = "messages.json.gz"


def save_snapshot(chat_dir, msgs) -> Path:
    path = Path(chat_dir) / _NAME
    rows = [{"i": m.rowid, "t": m.ts.isoformat(), "s": m.sender, "x": m.text,
             "sys": m.system or "", "a": m.attachments, "p": m.attachment_paths,
             "r": m.reactions}
            for m in msgs]
    tmp = path.with_suffix(".tmp")
    with gzip.open(tmp, "wt") as f:
        json.dump(rows, f)
    tmp.replace(path)
    return path


def load_snapshot(chat_dir):
    """Messages from the snapshot, or None when no snapshot exists yet."""
    path = Path(chat_dir) / _NAME
    if not path.exists():
        return None
    with gzip.open(path, "rt") as f:
        rows = json.load(f)
    return [Message(ts=datetime.fromisoformat(r["t"]), sender=r["s"], text=r["x"],
                    is_from_me=False, rowid=r["i"], system=r["sys"] or None,
                    attachments=r["a"], attachment_paths=r["p"], reactions=r["r"])
            for r in rows]


def snapshot_mtime(chat_dir) -> float:
    path = Path(chat_dir) / _NAME
    return path.stat().st_mtime if path.exists() else 0.0
