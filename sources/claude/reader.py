"""Read Claude Code session logs into the shared Message primitive.

Sessions live at `~/.claude/projects/<project-slug>/<session>.jsonl` — a work
log of you and the agent building things together. Spec `claude:<project-slug>`
ingests a project's sessions as one stream: your prompts and the assistant's
prose (tool noise and subagent sidechains skipped), each session opening with
a system item carrying its title.

Each session file owns a stable hash-derived id block, so new sessions never
shift existing ids and citations stay permanent.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ..imessage.db import Message
from ..imessage.identity import load_identities

DEFAULT_ROOT = Path.home() / ".claude" / "projects"
CLAUDE_ID_BASE = 3_000_000_000_000
_MAX_CHARS = 4000                     # giant pastes get elided, dialogue never


def session_block(key: str) -> int:
    h = int(hashlib.sha1(key.encode()).hexdigest(), 16)
    return CLAUDE_ID_BASE + (h % 10_000_000) * 100_000


def _ts(iso) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone().replace(tzinfo=None)


@dataclass
class Project:
    slug: str
    sessions: int
    last: datetime | None


class ClaudeSessions:
    def __init__(self, root=None, identities="identities.json"):
        self.root = Path(root) if root else DEFAULT_ROOT
        if not self.root.is_dir():
            raise FileNotFoundError(f"no Claude projects at {self.root}")
        ident = load_identities(identities if Path(str(identities)).exists() else None)
        self.author = ident.get("me") or "me"

    def projects(self) -> list:
        out = []
        for d in sorted(self.root.iterdir()):
            files = list(d.glob("*.jsonl")) if d.is_dir() else []
            if files:
                newest = max(f.stat().st_mtime for f in files)
                out.append(Project(d.name, len(files), datetime.fromtimestamp(newest)))
        out.sort(key=lambda p: p.last or datetime.min, reverse=True)
        return out

    def _session_items(self, project, path, until):
        title = ""
        rows = []
        for line in path.read_text(errors="replace").splitlines():
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            t = d.get("type")
            if t == "ai-title":
                title = d.get("title") or title
            if d.get("isSidechain"):
                continue
            if t == "user":
                content = d.get("message", {}).get("content")
                if isinstance(content, str) and content.strip() \
                        and not content.startswith("<"):
                    rows.append((d.get("timestamp"), self.author, content))
            elif t == "assistant":
                parts = [c.get("text", "") for c in (d.get("message", {}).get("content") or [])
                         if isinstance(c, dict) and c.get("type") == "text"]
                text = "\n".join(p for p in parts if p.strip())
                if text.strip():
                    rows.append((d.get("timestamp"), "Claude", text))
        if not rows:
            return []
        base = session_block(f"{project}/{path.stem}")
        out = []
        first_ts = _ts(rows[0][0])
        if not (until and first_ts >= until):
            out.append(Message(ts=first_ts, sender="", text="",
                               is_from_me=False, rowid=base,
                               system=f"Claude Code session started: {title or path.stem[:8]}",
                               src=f"claude:{project}"))
        for i, (iso, sender, text) in enumerate(rows, start=1):
            ts = _ts(iso)
            if until and ts >= until:
                continue
            if len(text) > _MAX_CHARS:
                text = text[:_MAX_CHARS] + " …[elided]"
            out.append(Message(ts=ts, sender=sender, text=text,
                               is_from_me=sender == self.author, rowid=base + i,
                               src=f"claude:{project}"))
        return out

    def messages(self, project, until=None) -> list:
        d = self.root / project
        if not d.is_dir():
            raise FileNotFoundError(f"no Claude project {project!r}")
        out = []
        for path in sorted(d.glob("*.jsonl")):
            out += self._session_items(project, path, until)
        out.sort(key=lambda m: m.ts)
        return out
