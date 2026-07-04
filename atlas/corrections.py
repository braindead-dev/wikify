"""Maintainer corrections — free-text fixes folded into the wiki invisibly.

    python3 -m atlas correct my-chat "Dax's surname is Martinez, not Phillips"

One resolver call maps the correction onto the page tree (verifying any [#id]
cites against the real messages) and attaches an authoritative directive to the
affected pages, which go pending; the next `wiki` run rewrites them with the
correction as ground truth. Directives are permanent page context — any future
rewrite still honors them, so an error in the underlying record can never creep
back. The full history stays auditable in `wiki/corrections.json`; the articles
themselves read as if they were always right.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

from sources.fetch import fetch
from sources.imessage.render import format_message

from .compose import (_atomic_write, _page_tree, _prompt, _save_state,
                      merge_pages, workspace_header)
from .config import ComposeConfig
from .llm import LLMClient


def _correction_schema(page_ids) -> dict:
    return {
        "type": "object",
        "properties": {
            "pages": {"type": "array", "items": {"type": "string", "enum": sorted(page_ids)}},
            "kind": {"type": "string", "enum": ["rename", "attribution", "fact", "remove",
                                               "reframe", "merge", "split"]},
            "directive": {"type": "string"},
            "retitle": {
                "type": "object",
                "properties": {"title": {"type": "string"},
                               "aliases_add": {"type": "array", "items": {"type": "string"}}},
                "required": ["title", "aliases_add"],
                "additionalProperties": False,
            },
            "new_page": {
                "type": "object",
                "properties": {"id": {"type": "string"}, "title": {"type": "string"},
                               "aliases": {"type": "array", "items": {"type": "string"}}},
                "required": ["id", "title", "aliases"],
                "additionalProperties": False,
            },
        },
        "required": ["pages", "kind", "directive", "retitle", "new_page"],
        "additionalProperties": False,
    }


def add_correction(chat_dir, text, config: ComposeConfig = None, verbose=True) -> list:
    """Resolve one correction and attach it; returns the affected page ids."""
    cfg = config or ComposeConfig()
    chat_dir = Path(chat_dir)
    wiki_dir = chat_dir / "wiki"
    state = json.loads((wiki_dir / "plan.json").read_text())
    data = json.loads((chat_dir / "observations.json").read_text())
    msgs, db = fetch(data["chat_ids"])
    by_id = {m.rowid: m for m in msgs}

    user = f"CORRECTION:\n{text}"
    cited = [by_id[int(i)] for i in re.findall(r"#?(\d{4,})", text) if int(i) in by_id]
    if cited:
        user += "\n\nCITED MESSAGES:\n" + "\n".join(
            format_message(m, ids=True, with_date=True) for m in cited)

    participants = sorted({m.sender for m in msgs if not m.system and m.sender})
    system = (_prompt("correct.md")
              .replace("{workspace}", workspace_header(db, data["chat_ids"], msgs, participants))
              + "\n\nPAGE TREE:\n" + _page_tree(state))
    llm = LLMClient(cfg.model)
    out = llm.complete_json(system, user, effort=cfg.effort,
                            schema=_correction_schema(state["pages"]),
                            schema_name="correction", max_tokens=cfg.max_tokens,
                            temperature=cfg.temperature)

    pages = [p for p in out.get("pages", []) if p in state["pages"]]
    directive = (out.get("directive") or "").strip()
    if not pages or not directive:
        raise ValueError(f"correction did not resolve to any page: {out}")

    kind = out.get("kind")
    if kind == "merge" and len(pages) >= 2:
        # same subject under two pages — reuse the standard merge machinery
        merge_pages(state, wiki_dir, pages[0], pages[1])
        pages = pages[:1]
    if kind == "split" and out.get("new_page", {}).get("id"):
        # one page actually covers two people/subjects — create the sibling and
        # share the material; each writer's directive says which claims are whose
        np = out["new_page"]
        nid = np["id"] if "/" in np["id"] else f"person/{np['id']}"
        if nid not in state["pages"]:
            src = state["pages"][pages[0]]
            state["pages"][nid] = {"id": nid, "title": np["title"], "type": src["type"],
                                   "aliases": np.get("aliases", []), "obs": list(src["obs"]),
                                   "status": "pending", "corrections": [directive]}
            pages.append(nid)
    for pid in pages:
        page = state["pages"][pid]
        if directive not in page.setdefault("corrections", []):
            page["corrections"].append(directive)
        page["status"] = "pending"
        if kind == "rename" and out.get("retitle", {}).get("title"):
            old = page["title"]
            page["title"] = out["retitle"]["title"]
            page["aliases"] = sorted(set(page.get("aliases", []) + [old]
                                         + out["retitle"].get("aliases_add", [])))
    _save_state(wiki_dir, state)

    log_path = wiki_dir / "corrections.json"
    log = json.loads(log_path.read_text()) if log_path.exists() else []
    log.append({"text": text, "ts": time.strftime("%Y-%m-%d %H:%M"),
                "kind": kind, "pages": pages, "directive": directive})
    _atomic_write(log_path, log)

    if verbose:
        print(f"[correct] {kind} → {', '.join(pages)}")
        print(f"  directive: {directive}")
        print("  pages marked pending — run `atlas wiki` to fold the correction in", flush=True)
    return pages
