"""Chunking — split a conversation into content-sized, day-aligned slices, and
render each as an ID-tagged transcript the model can cite from.

Chunks scale with information, not calls (P7): we cut at day boundaries once a
size target is reached, so a slice is a coherent stretch of conversation.
"""
from __future__ import annotations

from imessage.render import format_message


def chunk_messages(messages: list, size: int = 300) -> list:
    """Group messages into ~`size` slices, only cutting at day boundaries."""
    chunks, cur = [], []
    for i, m in enumerate(messages):
        cur.append(m)
        last = i + 1 == len(messages)
        day_ends = last or messages[i + 1].ts.date() != m.ts.date()
        if len(cur) >= size and day_ends:
            chunks.append(cur)
            cur = []
    if cur:
        chunks.append(cur)
    return chunks


def render_chunk(messages: list) -> str:
    """ID-tagged, day-grouped transcript (the same format L1 exports with --ids)."""
    lines, day = [], None
    for m in messages:
        d = m.ts.strftime("%Y-%m-%d")
        if d != day:
            lines.append(f"== {d} ==")
            day = d
        lines.append(format_message(m, ids=True))
    return "\n".join(lines)
