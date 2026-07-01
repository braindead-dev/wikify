"""The Page — the one maintained artifact (P4). Typed markdown + frontmatter.

Serialization is a tiny, deterministic frontmatter codec (we own the schema, so
we don't need a general YAML parser — keeps the store dependency-free). Citations
live inline in the body as `[#<message_id>]`; cross-links as `[[<page_id>]]`.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

FENCE = "---"
# a citation bracket holds one or more ids: [#1024] or [#1024, #1031] or [#1024, 1031]
CITE_RE = re.compile(r"\[#\s*\d+(?:\s*,\s*#?\s*\d+)*\s*\]")
_ID_RE = re.compile(r"\d+")
LINK_RE = re.compile(r"\[\[([^\]]+)\]\]")     # [[event/road-trip]]


def cited_ids(text: str) -> list:
    """Every message id cited in the text, in first-appearance order (deduped).
    Handles both [#123] and [#123, #456] brackets."""
    seen, out = set(), []
    for bracket in CITE_RE.finditer(text):
        for m in _ID_RE.finditer(bracket.group(0)):
            mid = int(m.group(0))
            if mid not in seen:
                seen.add(mid)
                out.append(mid)
    return out


@dataclass
class Page:
    id: str                                   # "type/slug", stable, never reused
    type: str                                 # person | event | topic | place | index | ...
    title: str
    body: str = ""
    aliases: list = field(default_factory=list)
    pinned: bool = False
    updated: str = ""                         # YYYY-MM-DD (stamped by the runner)

    # --- derived-from-body, mirrored in frontmatter for fast integrity checks --
    @property
    def sources(self) -> list:
        """Every message id this page cites, in first-appearance order."""
        return cited_ids(self.body)

    @property
    def links(self) -> list:
        """Every page id this page links to, deduped."""
        return list(dict.fromkeys(m.group(1) for m in LINK_RE.finditer(self.body)))

    # --- serialization -------------------------------------------------------
    def to_markdown(self) -> str:
        fm = {
            "id": self.id, "type": self.type, "title": self.title,
            "aliases": self.aliases, "pinned": self.pinned,
            "sources": self.sources, "updated": self.updated,
        }
        lines = [FENCE]
        for key, val in fm.items():
            lines.append(f"{key}: {_dump_scalar(val)}")
        lines.append(FENCE)
        body = self.body.strip("\n")
        return "\n".join(lines) + ("\n\n" + body + "\n" if body else "\n")

    @classmethod
    def from_markdown(cls, text: str) -> "Page":
        fm, body = _split_frontmatter(text)
        return cls(
            id=fm.get("id", ""), type=fm.get("type", ""), title=fm.get("title", ""),
            body=body, aliases=fm.get("aliases", []) or [],
            pinned=bool(fm.get("pinned", False)), updated=fm.get("updated", ""),
        )


# ---------------------------------------------------------------- frontmatter
def _dump_scalar(val) -> str:
    if isinstance(val, bool):
        return "true" if val else "false"
    if isinstance(val, list):
        return "[" + ", ".join(str(v) for v in val) + "]"
    return str(val)


def _parse_scalar(raw: str):
    raw = raw.strip()
    if raw in ("true", "false"):
        return raw == "true"
    if raw.startswith("[") and raw.endswith("]"):
        inner = raw[1:-1].strip()
        if not inner:
            return []
        return [_parse_scalar(p) for p in inner.split(",")]
    if re.fullmatch(r"-?\d+", raw):
        return int(raw)
    return raw.strip().strip('"').strip("'")


def _split_frontmatter(text: str):
    """Return (frontmatter_dict, body). Only a leading --- ... --- block counts."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != FENCE:
        return {}, text.strip("\n")
    fm: dict = {}
    for i in range(1, len(lines)):
        if lines[i].strip() == FENCE:
            body = "\n".join(lines[i + 1:]).strip("\n")
            return fm, body
        if ":" in lines[i]:
            key, _, raw = lines[i].partition(":")
            fm[key.strip()] = _parse_scalar(raw)
    return fm, ""       # no closing fence → treat all as frontmatter
