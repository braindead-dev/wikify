"""The single narrow write path (P2).

The model proposes a list of typed edit ops (plain dicts, as emitted by the LLM);
`apply_edits` validates the *whole batch* against an in-memory working copy and
commits atomically — either every op lands or none do. Citations are verified
here (P3): no text carrying an unresolvable `[#id]` is ever written.

Edits address markdown by *section heading*, not by fuzzy text match — because we
control the page structure, this is more robust than a diff. Ops:

    create_page {id, type, title, aliases?, body?}
    append      {page, text}                      # idempotent on verbatim repeat
    section     {page, heading, text}             # replace/create a "## heading"
    meta        {page, title?, add_aliases?}
    link        {from, to}                         # ensure a [[to]] cross-link
    merge       {from, into}                       # fold + leave a redirect stub
    retire      {page, redirect?}                  # tombstone, keeps links resolving
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .page import CITE_RE, Page
from .store import Store, slugify, valid_id


class EditError(Exception):
    """Raised when a batch fails validation. Message is compact, for LLM retry."""


@dataclass
class ChangeSet:
    created: list = field(default_factory=list)
    modified: list = field(default_factory=list)
    retired: list = field(default_factory=list)

    @property
    def touched(self) -> list:
        return sorted(set(self.created + self.modified + self.retired))

    def summary(self) -> str:
        bits = []
        if self.created:
            bits.append(f"+{len(self.created)} new")
        if self.modified:
            bits.append(f"~{len(self.modified)} edited")
        if self.retired:
            bits.append(f"-{len(self.retired)} retired")
        return ", ".join(bits) or "no changes"


def apply_edits(store: Store, ops: list, resolves) -> ChangeSet:
    """Validate then commit a batch. `resolves(message_id:int) -> bool` verifies a
    citation against the source transcript. Raises EditError (nothing written) on
    any invalid op or unresolvable citation."""
    working: dict = {}          # id -> Page (loaded lazily, mutated in memory)
    retired: set = set()
    changed: set = set()
    created: set = set()

    def load(pid: str):
        if pid not in working:
            page = store.read(pid)
            if page is None:
                return None
            working[pid] = page
        return working[pid]

    for i, op in enumerate(ops):
        try:
            _apply_one(op, store, working, retired, changed, created, load)
        except EditError as e:
            raise EditError(f"op {i} ({op.get('op','?')}): {e}")

    # batch-level validation ------------------------------------------------
    problems = []
    known_ids = set(store.all_ids()) | set(working)   # retired pages stay as redirects
    for pid in changed | created:
        page = working[pid]
        for mid in page.sources:
            if not resolves(mid):
                problems.append(f"{pid} cites #{mid} which does not resolve")
        for target in page.links:
            if target not in known_ids:
                problems.append(f"{pid} links to [[{target}]] which does not exist")
    if problems:
        raise EditError("; ".join(problems[:8]))

    # commit ----------------------------------------------------------------
    for pid in changed | created:
        working[pid].body = _normalize(working[pid].body)
        store.write(working[pid])
    return ChangeSet(
        created=sorted(created),
        modified=sorted(changed - created - retired),
        retired=sorted(retired),
    )


# ---------------------------------------------------------------- one op
def _apply_one(op, store, working, retired, changed, created, load):
    kind = op.get("op")

    if kind == "create_page":
        pid = op.get("id", "")
        if not valid_id(pid):
            raise EditError(f"invalid id {pid!r}")
        if pid.split("/")[0] != op.get("type", "") and pid != "index":
            raise EditError(f"id/type mismatch ({pid} vs {op.get('type')})")
        if store.exists(pid) or pid in working:
            raise EditError(f"{pid} already exists — append instead")
        working[pid] = Page(
            id=pid, type=op.get("type", pid.split("/")[0]), title=op.get("title", pid),
            aliases=op.get("aliases", []) or [], body=(op.get("body", "") or "").strip(),
        )
        created.add(pid); changed.add(pid)
        return

    if kind == "merge":
        src, dst = _need(op, "from"), _need(op, "into")
        s, d = load(src), load(dst)
        if s is None or d is None:
            raise EditError(f"merge needs both pages ({src}, {dst})")
        d.body = _append(d.body, s.body)
        s.body = f"Merged into [[{dst}]]."
        changed.add(dst); changed.add(src); retired.add(src)
        return

    if kind == "retire":
        pid = _need(op, "page")
        page = load(pid)
        if page is None:
            raise EditError(f"no page {pid}")
        red = op.get("redirect")
        page.body = f"Merged into [[{red}]]." if red else "Retired."
        changed.add(pid); retired.add(pid)
        return

    if kind == "rewrite":                          # full-body replace (consolidation only)
        pid = _need(op, "page")
        page = load(pid)
        if page is None:
            raise EditError(f"no page {pid}")
        page.body = (op.get("body", "") or "").strip()
        changed.add(pid)
        return

    # ops below all target one existing page --------------------------------
    pid = _need(op, "page") if kind != "link" else _need(op, "from")
    page = load(pid)
    if page is None:
        raise EditError(f"no page {pid} — create it first")

    if kind == "append":
        page.body = _append(page.body, _need(op, "text"))
    elif kind == "section":
        page.body = _set_section(page.body, _need(op, "heading"), _need(op, "text"))
    elif kind == "meta":
        if op.get("title"):
            page.title = op["title"]
        for a in op.get("add_aliases", []) or []:
            if a not in page.aliases:
                page.aliases.append(a)
    elif kind == "link":
        to = _need(op, "to")
        page.body = _add_bullet(page.body, "Related", f"[[{to}]]")
    else:
        raise EditError(f"unknown op {kind!r}")
    changed.add(pid)


def _need(op, key):
    val = op.get(key)
    if not val:
        raise EditError(f"missing {key!r}")
    return val


# ---------------------------------------------------------------- markdown
def _append(body: str, text: str) -> str:
    text = (text or "").strip()
    if not text or text in body:               # verbatim dedup aids idempotency
        return body
    return (body.rstrip("\n") + "\n\n" + text).strip("\n") if body.strip() else text


def _set_section(body: str, heading: str, text: str) -> str:
    """Replace the content under `## heading`, or create the section if absent."""
    target = f"## {heading}"
    lines = body.split("\n")
    out, i, replaced = [], 0, False
    while i < len(lines):
        if lines[i].strip() == target:
            out.append(target)
            out.append(text.strip())
            i += 1
            while i < len(lines) and not lines[i].startswith("## "):
                i += 1
            replaced = True
        else:
            out.append(lines[i]); i += 1
    result = "\n".join(out).strip("\n")
    if not replaced:
        result = (result + f"\n\n{target}\n{text.strip()}").strip("\n")
    return result


def _normalize(body: str) -> str:
    """Keep a single `## Related` link section pinned to the bottom. It holds only
    bullet lines, so prose written across chunks stays contiguous above it (and
    multiple Related sections merge + dedupe)."""
    lines = body.split("\n")
    bullets, rest, has_related, i = [], [], False, 0
    while i < len(lines):
        if lines[i].strip() == "## Related":
            has_related = True
            j = i + 1
            while j < len(lines) and (lines[j].lstrip().startswith("- ") or not lines[j].strip()):
                if lines[j].strip():
                    bullets.append(lines[j].strip())
                j += 1
            i = j
        else:
            rest.append(lines[i])
            i += 1
    out = "\n".join(rest).strip("\n")
    if has_related and bullets:
        out = (out + "\n\n## Related\n" + "\n".join(dict.fromkeys(bullets))).strip("\n")
    return out


def _add_bullet(body: str, heading: str, item: str) -> str:
    """Add `- item` under `## heading` (creating it), if not already present."""
    if f"[[{item}]]" in body or f"- {item}" in body:
        return body
    target = f"## {heading}"
    if target in body.split("\n"):
        lines = body.split("\n")
        # insert after the last bullet of that section
        start = next(i for i, l in enumerate(lines) if l.strip() == target)
        j = start + 1
        while j < len(lines) and not lines[j].startswith("## "):
            j += 1
        lines.insert(j, f"- {item}")
        return "\n".join(lines).strip("\n")
    return (body.rstrip("\n") + f"\n\n{target}\n- {item}").strip("\n")
