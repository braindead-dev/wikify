"""Derived views over the store (P4) — never maintained, always recomputed.

These are pure functions of the pages (+ injected db callables for timestamp
resolution and citation checking), so they can never drift from the source.
"""
from __future__ import annotations

from .page import CITE_RE, cited_ids


def backlinks(store) -> dict:
    """page_id -> sorted list of page_ids that link to it."""
    out: dict = {}
    for page in store.all_pages():
        for target in page.links:
            out.setdefault(target, set()).add(page.id)
    return {k: sorted(v) for k, v in out.items()}


def entities(store) -> list:
    """The registry view: one entry per person page."""
    return [{"id": p.id, "title": p.title, "aliases": p.aliases}
            for p in store.by_type("person")]


def verify(store, resolves) -> list:
    """Integrity problems. `resolves(message_id) -> bool`. Empty list == healthy.
    This is the mechanical correctness signal (P3) — the eval leans on it."""
    ids = set(store.all_ids())
    problems = []
    for page in store.all_pages():
        for mid in page.sources:
            if not resolves(mid):
                problems.append(f"{page.id}: citation #{mid} does not resolve")
        for target in page.links:
            if target not in ids:
                problems.append(f"{page.id}: dangling link [[{target}]]")
    return problems


def timeline(store, resolve_ts) -> list:
    """Every cited claim as a dated point, sorted. `resolve_ts(message_id) ->
    datetime|None`. Each entry: {ts, page, message_id, text}. Unresolvable cites
    are dropped (verify() reports them)."""
    entries = []
    for page in store.all_pages():
        for line in page.body.split("\n"):
            cites = cited_ids(line)
            if not cites:
                continue
            claim = CITE_RE.sub("", line).strip(" -*\t").strip()
            for mid in cites:
                ts = resolve_ts(mid)
                if ts is not None:
                    entries.append({"ts": ts, "page": page.id,
                                    "message_id": mid, "text": claim})
    entries.sort(key=lambda e: e["ts"])
    return entries
