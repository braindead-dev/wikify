"""Layer 2 — compose the wiki from Layer 1 observations.

Three sublayers, the same architecture that made extraction reliable (one holistic
pass, then parallel everything, durable resumable state, full traces):

  PLAN   one pass over every observation title → the complete page tree
  ROUTE  parallel batches: each observation → the page(s) it belongs on
  WRITE  parallel per page: observations + original quoted messages → the article

`wiki/plan.json` is the durable state: the tree, the full observation→page routing,
and per-page status. Init and update are the same pipeline — a re-run routes only
observations it hasn't seen and rewrites only the pages they touch (passing each
writer the existing article to revise).
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import lru_cache
from pathlib import Path

from imessage import MessagesDB

from .config import ComposeConfig
from .llm import LLMClient
from .store import _atomic_write

_PROMPTS = Path(__file__).resolve().parent / "prompts"
_ID_RE = re.compile(r"(person|topic|event)/[a-z0-9][a-z0-9-]*$")


@lru_cache(maxsize=None)
def _prompt(name):
    return (_PROMPTS / name).read_text()


def obs_key(o) -> str:
    """Stable identity for an observation (content hash, so exact duplicates from
    overlapping extraction windows collapse to one)."""
    raw = o["title"] + "|" + ",".join(map(str, sorted(o["sources"])))
    return hashlib.sha1(raw.encode()).hexdigest()[:12]


# ---------------------------------------------------------------- schemas
def plan_schema() -> dict:
    return {
        "type": "object", "additionalProperties": False, "required": ["pages"],
        "properties": {"pages": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["id", "title", "type", "aliases"],
            "properties": {
                "id": {"type": "string",
                       "description": "page id as type/slug in lowercase kebab, e.g. person/alice"},
                "title": {"type": "string", "description": "the article's display title"},
                "type": {"type": "string", "enum": ["person", "topic", "event"]},
                "aliases": {"type": "array", "items": {"type": "string"},
                            "description": "other names this subject goes by (nicknames, variants)"},
            }}}},
    }


def route_schema(page_ids) -> dict:
    return {
        "type": "object", "additionalProperties": False, "required": ["assignments"],
        "properties": {"assignments": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["n", "pages"],
            "properties": {
                "n": {"type": "integer", "description": "the observation number"},
                "pages": {"type": "array", "maxItems": 3,
                          "items": {"type": "string", "enum": list(page_ids)},
                          "description": "the page(s) this observation belongs on; empty = noise"},
            }}}},
    }


ARTICLE_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["article"],
    "properties": {"article": {"type": "string",
                               "description": "the full markdown article body, no frontmatter"}},
}


# ---------------------------------------------------------------- state
def _load_state(wiki_dir: Path) -> dict:
    path = wiki_dir / "plan.json"
    if path.exists():
        return json.loads(path.read_text())
    return {"pages": {}, "routed": {}}


def _save_state(wiki_dir: Path, state) -> None:
    wiki_dir.mkdir(parents=True, exist_ok=True)
    _atomic_write(wiki_dir / "plan.json", state)


# ---------------------------------------------------------------- rendering
def workspace_header(db, chat_ids, msgs, participants) -> str:
    return (f"WIKI WORKSPACE: a group chat of {len(participants)} people "
            f"({', '.join(participants)}), {msgs[0].ts.date()} to {msgs[-1].ts.date()}, "
            f"{len(msgs)} messages.")


def _page_tree(state) -> str:
    return "\n".join(f"- {pid} — {p['title']} ({p['type']})"
                     for pid, p in sorted(state["pages"].items()))


def _obs_line(n, o) -> str:
    people = ",".join(o["people"]) or "-"
    return f"[{n}] ({o['type']}) {o['title']} · {people}"


def _quotes(o, by_id, limit) -> list:
    out = []
    for src in o["sources"][:limit]:
        m = by_id.get(src)
        if m is not None:
            text = (m.text or "").replace("\n", " ")[:150]
            out.append(f'    #{src} {m.sender} ({m.ts:%Y-%m-%d}): "{text}"')
    return out


# ---------------------------------------------------------------- validation
_CITE_RE = re.compile(r"\[#[^\]\[]*\]")
_LINK_RE = re.compile(r"\[\[([^\]]+)\]\]")


def clean_citations(text, allowed) -> str:
    """Keep only citations whose ids were actually in the writer's material."""
    def fix(match):
        ids = [i for i in re.findall(r"\d+", match.group(0)) if int(i) in allowed]
        return "[" + ", ".join(f"#{i}" for i in ids) + "]" if ids else ""
    return _CITE_RE.sub(fix, text)


def clean_links(text, page_ids) -> str:
    """Turn [[id]] links to nonexistent pages into plain text."""
    return _LINK_RE.sub(lambda m: m.group(0) if m.group(1) in page_ids else m.group(1), text)


# ---------------------------------------------------------------- stages
def plan_pages(llm, obs_items, workspace, cfg, trace) -> list:
    lines = "\n".join(_obs_line(n, o) for n, (_, o) in enumerate(obs_items))
    system = _prompt("plan.md").replace("{workspace}", workspace)
    user = f"ALL OBSERVATIONS ({len(obs_items)}):\n{lines}\n\nDesign the complete page tree."
    out = llm.complete_json(system, user, effort=cfg.effort, schema=plan_schema(),
                            schema_name="plan", trace=trace, max_tokens=cfg.max_tokens,
                            temperature=cfg.temperature)
    pages, seen = [], set()
    for p in (out.get("pages") or []):
        pid = str(p.get("id", "")).strip()
        if _ID_RE.fullmatch(pid) and pid not in seen:
            seen.add(pid)
            pages.append({"id": pid, "title": str(p.get("title") or pid).strip(),
                          "type": pid.split("/")[0],
                          "aliases": [str(a).strip() for a in p.get("aliases", []) if str(a).strip()]})
    return pages


def route_batch(llm, batch, state, workspace, cfg, trace) -> dict:
    """batch: list of (key, obs). Returns {key: [page ids]} for every key."""
    system = (_prompt("route.md").replace("{workspace}", workspace)
              + "\n\nPAGE TREE:\n" + _page_tree(state))
    lines = "\n".join(_obs_line(n, o) for n, (_, o) in enumerate(batch))
    user = f"OBSERVATIONS:\n{lines}\n\nRoute every observation."
    out = llm.complete_json(system, user, effort=cfg.effort,
                            schema=route_schema(state["pages"]), schema_name="route",
                            trace=trace, max_tokens=cfg.max_tokens, temperature=cfg.temperature)
    routed = {}
    for a in (out.get("assignments") or []):
        n = a.get("n")
        if isinstance(n, int) and 0 <= n < len(batch):
            routed[batch[n][0]] = list(dict.fromkeys(a.get("pages") or []))
    for key, _ in batch:                      # unanswered observations stay unrouted
        routed.setdefault(key, None)
    return routed


def write_page(llm, pid, state, keyed, by_id, workspace, wiki_dir, cfg, trace) -> str:
    page = state["pages"][pid]
    material, allowed = [], set()
    for n, key in enumerate(page["obs"]):
        o = keyed[key]
        allowed.update(o["sources"])
        material.append(_obs_line(n, o) + " — " + o["detail"])
        material.extend(_quotes(o, by_id, cfg.quotes_per_obs))
    system = (_prompt("write.md").replace("{workspace}", workspace)
              + "\n\nPAGE TREE (for [[cross-links]]):\n" + _page_tree(state))
    user = (f"PAGE TO WRITE: {pid} — \"{page['title']}\" ({page['type']})"
            + (f" · aliases: {', '.join(page['aliases'])}" if page.get("aliases") else ""))
    existing = _page_path(wiki_dir, pid)
    if existing.exists():
        body = existing.read_text().split("---", 2)[-1].strip()
        user += f"\n\nEXISTING ARTICLE (revise with the new material):\n{body}"
    user += "\n\nMATERIAL (observations with original messages):\n" + "\n".join(material)
    out = llm.complete_json(system, user, effort=cfg.effort, schema=ARTICLE_SCHEMA,
                            schema_name="article", trace=trace, max_tokens=cfg.max_tokens,
                            temperature=cfg.temperature)
    article = (out.get("article") or "").strip()
    if not article:
        raise ValueError("writer returned an empty article")
    article = clean_links(clean_citations(article, allowed), set(state["pages"]))
    return article


def _page_path(wiki_dir: Path, pid: str) -> Path:
    return wiki_dir / (pid + ".md")


def _write_page_file(wiki_dir, pid, page, article) -> None:
    path = _page_path(wiki_dir, pid)
    path.parent.mkdir(parents=True, exist_ok=True)
    aliases = json.dumps(page.get("aliases", []), ensure_ascii=False)
    front = (f"---\nid: {pid}\ntitle: {page['title']}\ntype: {page['type']}\n"
             f"aliases: {aliases}\nobservations: {len(page['obs'])}\n"
             f"updated: {time.strftime('%Y-%m-%d')}\n---\n\n")
    tmp = path.with_suffix(".md.tmp")
    tmp.write_text(front + article + "\n")
    tmp.replace(path)


def rebuild_index(wiki_dir, state) -> None:
    by_type = {"person": [], "topic": [], "event": []}
    for pid, p in sorted(state["pages"].items()):
        if p.get("status") == "written":
            by_type[p["type"]].append(f"- [{p['title']}]({pid}.md) · {len(p['obs'])} observations")
    parts = ["# Index\n"]
    for t, label in (("person", "People"), ("topic", "Topics"), ("event", "Events")):
        if by_type[t]:
            parts.append(f"\n## {label}\n\n" + "\n".join(by_type[t]))
    (wiki_dir / "index.md").write_text("\n".join(parts) + "\n")


# ---------------------------------------------------------------- pipeline
def build_wiki(chat_dir, config: ComposeConfig = None, stage="all", limit_pages=None,
               verbose=True) -> dict:
    """Observations → wiki. Resumable at every level; init and update are the
    same call. Returns the final state."""
    cfg = config or ComposeConfig()
    chat_dir = Path(chat_dir)
    wiki_dir = chat_dir / "wiki"

    data = json.loads((chat_dir / "observations.json").read_text())
    keyed, order = {}, []
    for o in data["observations"]:
        k = obs_key(o)
        if k not in keyed:
            keyed[k] = o
            order.append(k)

    ident = "identities.json" if Path("identities.json").exists() else None
    db = MessagesDB(identities=ident)
    msgs = db.messages(data["chat_ids"])
    by_id = {m.rowid: m for m in msgs}
    participants = sorted({m.sender for m in msgs if not m.system and m.sender})
    workspace = workspace_header(db, data["chat_ids"], msgs, participants)

    state = _load_state(wiki_dir)
    llm = LLMClient(cfg.model)
    t0 = time.time()

    def sink(name):
        def _t(rec):
            if cfg.trace or rec.get("status") == "error":
                (wiki_dir / "traces").mkdir(parents=True, exist_ok=True)
                _atomic_write(wiki_dir / "traces" / f"{name}.json", rec)
        return _t

    # ---- PLAN
    if not state["pages"]:
        if verbose:
            print(f"[plan] designing page tree from {len(order)} observations…", flush=True)
        pages = plan_pages(llm, [(k, keyed[k]) for k in order], workspace, cfg, sink("plan"))
        state["pages"] = {p["id"]: {**p, "status": "pending", "obs": []} for p in pages}
        _save_state(wiki_dir, state)
        if verbose:
            kinds = {t: sum(1 for p in state["pages"].values() if p["type"] == t)
                     for t in ("person", "topic", "event")}
            print(f"[plan] {len(state['pages'])} pages: {kinds}", flush=True)
    if stage == "plan":
        return state

    # ---- ROUTE
    todo = [k for k in order if k not in state["routed"]]
    if todo:
        batches = [todo[i:i + cfg.route_batch] for i in range(0, len(todo), cfg.route_batch)]
        workers = cfg.workers or len(batches)
        if verbose:
            print(f"[route] {len(todo)} observations → {len(batches)} batches · x{workers}", flush=True)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(route_batch, llm, [(k, keyed[k]) for k in b],
                                   state, workspace, cfg, sink(f"route-{i:03d}")): i
                       for i, b in enumerate(batches)}
            done = 0
            for fut in as_completed(futures):
                try:
                    routed = fut.result()
                except Exception as e:
                    print(f"\n  route batch {futures[fut]} failed — {str(e)[:80]}", flush=True)
                    continue
                for key, pages in routed.items():
                    if pages is None:
                        continue
                    state["routed"][key] = pages
                    for pid in pages:
                        if pid in state["pages"] and key not in state["pages"][pid]["obs"]:
                            state["pages"][pid]["obs"].append(key)
                            state["pages"][pid]["status"] = "pending"
                _save_state(wiki_dir, state)
                done += 1
                if verbose:
                    print(f"\r  [route] {done}/{len(batches)} batches", end="", flush=True)
        if verbose:
            print(flush=True)
    if stage == "route":
        return state

    # ---- WRITE
    pending = [pid for pid, p in state["pages"].items() if p["status"] == "pending" and p["obs"]]
    if limit_pages:
        pending = pending[:limit_pages]
    if pending:
        workers = cfg.workers or len(pending)
        if verbose:
            print(f"[write] {len(pending)} pages · x{workers}", flush=True)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(write_page, llm, pid, state, keyed, by_id, workspace,
                                   wiki_dir, cfg, sink("write-" + pid.replace("/", "-"))): pid
                       for pid in pending}
            done = 0
            for fut in as_completed(futures):
                pid = futures[fut]
                try:
                    article = fut.result()
                    _write_page_file(wiki_dir, pid, state["pages"][pid], article)
                    state["pages"][pid]["status"] = "written"
                    _save_state(wiki_dir, state)
                except Exception as e:
                    print(f"\n  {pid} failed — {str(e)[:80]}", flush=True)
                done += 1
                if verbose:
                    print(f"\r  [write] {done}/{len(pending)} pages", end="", flush=True)
        if verbose:
            print(flush=True)
    elif verbose:
        print("[write] nothing to write — wiki is up to date", flush=True)

    rebuild_index(wiki_dir, state)
    if verbose:
        written = sum(1 for p in state["pages"].values() if p["status"] == "written")
        print(f"[wiki] {written}/{len(state['pages'])} pages written → {wiki_dir} "
              f"· {llm.usage} · {time.time() - t0:.0f}s", flush=True)
    return state
