"""The reducer (L3) — reduce(store, chunk): propose edits, apply, self-correct.

This is the only layer that "thinks". It is a fixed pipeline, not an autonomous
agent (P6): assemble context → ask the model for edits → apply through the single
write path → on rejection, feed the compact error back once and retry. The model
proposes; L2 disposes.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from ..store import EditError, apply_edits
from .chunk import render_chunk

_PROMPTS = Path(__file__).resolve().parent.parent / "prompts"


@lru_cache(maxsize=None)
def _prompt(name: str) -> str:
    return (_PROMPTS / name).read_text()


def _fill(template: str, **kw) -> str:
    """Placeholder substitution that is safe against braces in the content."""
    for key, val in kw.items():
        template = template.replace("{" + key + "}", val)
    return template


class Reducer:
    def __init__(self, store, llm, title, resolves):
        self.store = store
        self.llm = llm
        self.title = title
        self.resolves = resolves

    # -- context assembly (P5: we decide exactly what the model sees) --------
    def _context(self) -> dict:
        people = self.store.by_type("person")
        others = [p for p in self.store.all_pages()
                  if p.type not in ("person", "index")]
        roster = "\n".join(
            f"- {p.id}  ({p.title}" + (f"; aka {', '.join(p.aliases)}" if p.aliases else "") + ")"
            for p in people) or "(none)"
        pages = "\n".join(f"- {p.id}  ({p.title})" for p in others) or "(none yet)"
        profiles = "\n\n".join(
            f"### {p.id}\n{p.body.strip() or '(empty)'}" for p in people) or "(none)"
        return {"roster": roster, "pages": pages, "profiles": profiles}

    def _ask(self, user: str) -> list:
        out = self.llm.complete_json(_prompt("system.md"), user)
        edits = out.get("edits", []) if isinstance(out, dict) else out
        return edits if isinstance(edits, list) else []

    # -- the pipeline for one chunk -----------------------------------------
    def reduce_chunk(self, messages: list):
        ctx = self._context()
        user = _fill(_prompt("chunk.md"), title=self.title, transcript=render_chunk(messages),
                     roster=ctx["roster"], pages=ctx["pages"], profiles=ctx["profiles"])
        edits = self._ask(user)
        try:
            return apply_edits(self.store, edits, self.resolves)
        except EditError as first:
            # one corrective pass — tell the model exactly what was wrong (codex #9)
            retry = user + (f"\n\n---\nYour previous edits were REJECTED: {first}\n"
                            "Return corrected JSON with the same intent.")
            return apply_edits(self.store, self._ask(retry), self.resolves)
