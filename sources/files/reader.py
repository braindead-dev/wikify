"""Read a folder of plain-text/markdown documents into the shared Message
primitive — the universal adapter: anything exportable as text files becomes
ingestable (`files:~/notes`, `files:./docs`).

Each file becomes a stream of paragraph items (blank-line separated, long
paragraphs split), so citations point at paragraphs and `resolve` shows the
surrounding section. Every file owns a stable hash-derived id block, so adding
or editing other files never shifts a file's ids; appending to a file keeps
its existing ids stable.
"""
from __future__ import annotations

import hashlib
from datetime import datetime
from pathlib import Path

from ..imessage.db import Message
from ..imessage.identity import load_identities

FILE_ID_BASE = 2_000_000_000_000            # far above every chat id space
_EXTS = {".md", ".txt", ".markdown", ".text", ".org", ".rst"}
_MAX_PARA_CHARS = 1200


def file_block(key: str) -> int:
    """Each file owns a stable 100k-id block derived from its relative path."""
    h = int(hashlib.sha1(key.encode()).hexdigest(), 16)
    return FILE_ID_BASE + (h % 10_000_000) * 100_000


def _paragraphs(text: str) -> list:
    out = []
    for block in text.split("\n\n"):
        block = block.strip()
        if not block:
            continue
        while len(block) > _MAX_PARA_CHARS:         # split runaway paragraphs
            cut = block.rfind("\n", 0, _MAX_PARA_CHARS)
            cut = cut if cut > 0 else _MAX_PARA_CHARS
            out.append(block[:cut].strip())
            block = block[cut:].strip()
        if block:
            out.append(block)
    return out


class FilesSource:
    def __init__(self, root, identities="identities.json"):
        self.root = Path(root).expanduser()
        if not self.root.is_dir():
            raise FileNotFoundError(f"no folder at {self.root}")
        ident = load_identities(identities if Path(str(identities)).exists() else None)
        self.author = ident.get("me") or "me"

    def files(self) -> list:
        return sorted(p for p in self.root.rglob("*")
                      if p.suffix.lower() in _EXTS and p.is_file())

    def messages(self, until=None) -> list:
        out = []
        for f in self.files():
            rel = f.relative_to(self.root).as_posix()
            ts = datetime.fromtimestamp(f.stat().st_mtime)
            if until and ts >= until:
                continue
            base = file_block(f"{self.root.name}/{rel}")
            for i, para in enumerate(_paragraphs(f.read_text(errors="replace"))):
                out.append(Message(ts=ts, sender=self.author, text=f"({rel}) {para}",
                                   is_from_me=True, rowid=base + i,
                                   src=f"files:{self.root}"))
        return out
