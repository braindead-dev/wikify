"""The reducer (L3) — reduce(store, chunk): propose edits, apply, self-correct.

This is the only layer that "thinks". It is a fixed pipeline, not an autonomous
agent (P6): assemble context → ask the model for edits → apply through the single
write path → on rejection, feed the compact error back once and retry. The model
proposes; L2 disposes.
"""
from __future__ import annotations

import json
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
    def __init__(self, store, llm, title, resolves, trace_dir=None):
        self.store = store
        self.llm = llm
        self.title = title
        self.resolves = resolves
        self.trace_dir = trace_dir
        self._n = 0

    # -- context assembly (P5: we decide exactly what the model sees) --------
    def _context(self) -> dict:
        people = self.store.by_type("person")
        roster = "\n".join(
            f"- {p.id}  ({p.title}" + (f"; aka {', '.join(p.aliases)}" if p.aliases else "") + ")"
            for p in people) or "(none)"
        # full current content of EVERY page (except the index) so the model
        # integrates into what exists instead of restating it (kills redundancy)
        docs = "\n\n".join(
            f"### {p.id}  ({p.title})\n{p.body.strip() or '(empty)'}"
            for p in self.store.all_pages() if p.type != "index") or "(none yet)"
        return {"roster": roster, "pages": docs}

    def _ask(self, user: str) -> list:
        out = self.llm.complete_json(_prompt("system.md"), user)
        edits = out.get("edits", []) if isinstance(out, dict) else out
        return edits if isinstance(edits, list) else []

    # -- the pipeline for one chunk -----------------------------------------
    def reduce_chunk(self, messages: list):
        ctx = self._context()
        user = _fill(_prompt("chunk.md"), title=self.title, transcript=render_chunk(messages),
                     roster=ctx["roster"], pages=ctx["pages"])
        edits = self._ask(user)
        retried = None
        try:
            cs = apply_edits(self.store, edits, self.resolves)
        except EditError as first:
            # one corrective pass — tell the model exactly what was wrong (codex #9)
            retried = str(first)
            retry = user + (f"\n\n---\nYour previous edits were REJECTED: {first}\n"
                            "Return corrected JSON with the same intent.")
            edits = self._ask(retry)
            cs = apply_edits(self.store, edits, self.resolves)
        self._dump(messages, user, edits, cs, retried)
        return cs

    def _dump(self, messages, user, edits, cs, retried):
        if not self.trace_dir:
            return
        self._n += 1
        span = f"{messages[0].ts:%Y%m%d}-{messages[-1].ts:%Y%m%d}"
        out = Path(self.trace_dir)
        out.mkdir(parents=True, exist_ok=True)
        (out / f"{self._n:03d}_{span}.json").write_text(json.dumps({
            "span": span, "messages": len(messages),
            "prompt": user, "edits": edits, "retried_after": retried,
            "applied": {"created": cs.created, "modified": cs.modified, "retired": cs.retired},
        }, indent=2))
