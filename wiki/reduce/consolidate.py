"""Consolidation — the second reducer mode (design §4): periodically refactor a
page that grew by incremental appends into a clean, organized whole.

Correctness guard: a consolidation that would drop any citation is reverted. A
messy-but-complete page always beats a tidy page that lost provenance (P3).
"""
from __future__ import annotations

from ..store import apply_edits
from .reducer import _fill, _prompt

_SYS = ("You are a careful wiki editor. Follow the instructions exactly, preserve "
        "every citation, and output JSON only.")


class Consolidator:
    def __init__(self, store, llm, resolves):
        self.store = store
        self.llm = llm
        self.resolves = resolves

    def consolidate_page(self, page_id):
        """Return (ChangeSet|None, dropped_citations). None + dropped => reverted."""
        page = self.store.read(page_id)
        if page is None:
            raise KeyError(page_id)
        before = set(page.sources)
        user = _fill(_prompt("consolidate.md"), id=page.id, type=page.type,
                     title=page.title, body=page.body)
        out = self.llm.complete_json(_SYS, user)
        new_body = (out.get("body", "") if isinstance(out, dict) else "").strip()
        if not new_body:
            return None, set()

        cs = apply_edits(self.store, [{"op": "rewrite", "page": page_id, "body": new_body}],
                         self.resolves)
        dropped = before - set(self.store.read(page_id).sources)
        if dropped:
            self.store.write(page)                # revert to the original
            return None, dropped
        return cs, set()
