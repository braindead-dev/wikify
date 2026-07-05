"""Layer 2 — compose the wiki from Layer 1 observations.

The same architecture that made extraction reliable — holistic passes where
holism matters, parallel everything else, durable resumable state, full traces:

  PLAN      one pass over every observation title → the complete page tree
  REVIEW    one pass over the tree itself → merge duplicates, delete junk
  ROUTE     parallel batches: each observation → the page(s) it belongs on
  WRITE     parallel per page: observations + original quoted messages + the
            canonical-origins table → the article (person pages also emit bio
            facts; a draft that measurably repeats itself gets an edit pass)
  ANALYSES  essays written FROM the finished entity pages — derived views that
            regenerate whenever a source page changes
  QUESTIONS uncertainties for the owner → wiki/questions.json; answers reconcile
            (page merges) at the start of the next build

Separately, `audit_pages` judges every written page against its own cited
messages and marks failing pages pending with the findings attached, so the next
build revises them with the problems in hand.

`wiki/plan.json` is the durable state: the tree, the full observation→page
routing, and per-page status. Init and update are the same pipeline — a re-run
routes only observations it hasn't seen and rewrites only the pages they touch.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import lru_cache
from pathlib import Path

from sources.fetch import fetch, parse_specs
from sources.instagram import InstagramExport

from .config import ComposeConfig
from .llm import LLMClient
from .store import _atomic_write

_PROMPTS = Path(__file__).resolve().parent / "prompts"
_ID_RE = re.compile(r"(person|topic|event)/[a-z0-9][a-z0-9-]*$")
_AID_RE = re.compile(r"analysis/[a-z0-9][a-z0-9-]*$")


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


def review_schema(page_ids) -> dict:
    ids = {"type": "string", "enum": list(page_ids)}
    return {
        "type": "object", "additionalProperties": False, "required": ["merges", "deletes"],
        "properties": {
            "merges": {"type": "array", "items": {
                "type": "object", "additionalProperties": False, "required": ["keep", "absorb"],
                "properties": {
                    "keep": {**ids, "description": "the page that stays"},
                    "absorb": {"type": "array", "items": ids, "minItems": 1,
                               "description": "duplicate pages folded into it"},
                }}},
            "deletes": {"type": "array", "items": ids,
                        "description": "pages that should not exist at all"},
        },
    }


# Normalized biography fields for person infoboxes — filled from the material,
# empty string where the material doesn't say.
PERSON_FACTS = {
    "full_name": "the person's full real name",
    "born": "birthday / age information, as stated in the material",
    "hometown": "where they are from or live",
    "education": "school and field of study",
    "occupation": "job(s) or roles, comma-separated",
    "relationship": "relationship status or partner, as of the latest material",
    "family": "family members who appear in the material",
}


def article_schema(person=False) -> dict:
    props = {"article": {"type": "string",
                         "description": "the full markdown article body, no frontmatter"}}
    required = ["article"]
    if person:
        props["facts"] = {
            "type": "object", "additionalProperties": False,
            "required": list(PERSON_FACTS),
            "properties": {k: {"type": "string", "description": v + "; empty if unknown"}
                           for k, v in PERSON_FACTS.items()},
        }
        required.append("facts")
    return {"type": "object", "additionalProperties": False,
            "required": required, "properties": props}


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
    header = (f"WIKI WORKSPACE: a group chat of {len(participants)} people "
              f"({', '.join(participants)}), {msgs[0].ts.date()} to {msgs[-1].ts.date()}, "
              f"{len(msgs)} messages.")
    im_ids, ig_keys, file_roots = parse_specs(chat_ids)
    if ig_keys and im_ids:
        titles = [InstagramExport().title(k) for k in ig_keys]
        header += (" The group talks across channels — iMessage and Instagram ("
                   + ", ".join(titles) + ") — the same people throughout; treat the "
                   "record as one history.")
    if file_roots:
        header += (" The record also includes personal documents ("
                   + ", ".join(Path(r).expanduser().name for r in file_roots) + ").")
    return header


def _page_tree(state) -> str:
    return "\n".join(f"- {pid} — {p['title']} ({p['type']})"
                     for pid, p in sorted(state["pages"].items()))


def _obs_line(n, o) -> str:
    people = ",".join(o["people"]) or "-"
    return f"[{n}] ({o['type']}) {o['title']} · {people}"


def msg_text(m) -> str:
    """A message's displayable text — system events render as '* <event>'."""
    return m.text or (f"* {m.system}" if m.system else "")


def _quotes(o, by_id, limit) -> list:
    out = []
    for src in o["sources"][:limit]:
        m = by_id.get(src)
        if m is not None:
            text = msg_text(m).replace("\n", " ")[:150]
            out.append(f'    #{src} {m.sender} ({m.ts:%Y-%m-%d}): "{text}"')
    return out


# ---------------------------------------------------------------- validation
_CITE_RE = re.compile(r"\[#[^\]\[]*\]")
_LINK_RE = re.compile(r"\[\[([^\]]+)\]\]")


def clean_citations(text, allowed) -> str:
    """Keep only citations whose ids were actually in the writer's material."""
    def fix(match):
        ids = [i for i in dict.fromkeys(re.findall(r"\d+", match.group(0)))
               if int(i) in allowed]
        return "[" + ", ".join(f"#{i}" for i in ids) + "]" if ids else ""
    return _CITE_RE.sub(fix, text)


def clean_links(text, page_ids) -> str:
    """Normalize [[id]] / [[id|label]] links; links to nonexistent pages become
    plain text."""
    def fix(m):
        pid, _, label = m.group(1).partition("|")
        if pid in page_ids:
            return m.group(0)
        return label or pid
    return _LINK_RE.sub(fix, text)


_UPAIR_RE = re.compile(r"\\u(d[89ab][0-9a-fA-F]{2})\\u(d[c-fC-F][0-9a-fA-F]{2})", re.I)
_UESC_RE = re.compile(r"\\u([0-9a-fA-F]{4})")


def _unescape(text) -> str:
    """Decode literal \\uXXXX escapes the model left in the text — surrogate
    pairs (emoji) first, then singles; any lone surrogate left over is dropped."""
    text = _UPAIR_RE.sub(lambda m: chr(0x10000 + (int(m.group(1), 16) - 0xD800) * 0x400
                                       + int(m.group(2), 16) - 0xDC00), text)
    text = _UESC_RE.sub(lambda m: chr(int(m.group(1), 16)), text)
    return "".join(c for c in text if not 0xD800 <= ord(c) <= 0xDFFF)


def polish(article, allowed, page_ids) -> str:
    """Deterministic cleanup of writer output: decode stray \\uXXXX escapes,
    validate citations and links, drop a leading H1 (the frontmatter carries the
    title), and collapse whitespace left behind by stripped citations."""
    article = _unescape(article)
    article = clean_links(clean_citations(article, allowed), page_ids)
    lines = article.strip().split("\n")
    if lines and re.match(r"#{1,3} ", lines[0].lstrip()):
        lines = lines[1:]
    # long bullet lists can degenerate into verbatim repetition — keep first occurrence
    seen, out = set(), []
    for ln in lines:
        if ln.lstrip().startswith(("-", "*")):
            key = ln.strip().lower()
            if key in seen:
                continue
            seen.add(key)
        out.append(ln)
    article = "\n".join(out).strip()
    article = re.sub(r"[ \t]+([.,;:!?])", r"\1", article)
    return re.sub(r"[ \t]{2,}", " ", article)


# ---------------------------------------------------------------- stages
def _plan_call(llm, lines, count, note, workspace, cfg, trace, effort=None):
    system = _prompt("plan.md").replace("{workspace}", workspace)
    user = f"{note}OBSERVATIONS ({count}):\n{lines}\n\nDesign the complete page tree."
    return llm.complete_json(system, user, effort=effort or cfg.effort, schema=plan_schema(),
                             schema_name="plan", trace=trace, max_tokens=cfg.max_tokens,
                             temperature=cfg.temperature)


def plan_pages(llm, obs_items, workspace, cfg, trace, trace_dir=None) -> list:
    """One holistic pass when the corpus fits; otherwise chronological shards
    each propose pages and a merge pass unifies them into one tree (holism moves
    to the merge — the reviewer then prunes as usual)."""
    budget = 280_000 * 4                                        # chars (~280k tokens)
    for render in (lambda n, o: _obs_line(n, o),
                   lambda n, o: f"[{n}] {o['title'][:70]}",
                   lambda n, o: o["title"][:40]):
        all_lines = [render(n, o) for n, (_, o) in enumerate(obs_items)]
        if sum(len(l) + 1 for l in all_lines) <= budget:
            break
    total = sum(len(l) + 1 for l in all_lines)
    if total <= budget:
        out = _plan_call(llm, "\n".join(all_lines), len(obs_items), "ALL ",
                         workspace, cfg, trace)
    else:
        n_shards = -(-total // budget)
        per = -(-len(all_lines) // n_shards)
        shards = [all_lines[i:i + per] for i in range(0, len(all_lines), per)]
        print(f"  [plan] corpus too large for one pass — {len(shards)} chronological "
              f"shards + merge", flush=True)
        # each shard's proposals persist immediately (keyed by input hash), so an
        # interrupted planning run resumes instead of restarting
        cache_dir = Path(trace_dir) if trace_dir else None

        def one_shard(i, sh):
            text = "\n".join(sh)
            key = hashlib.sha1(text.encode()).hexdigest()[:10]
            cached = cache_dir / f"shard-{i:02d}-{key}.json" if cache_dir else None
            if cached and cached.exists():
                return json.loads(cached.read_text())
            out = _plan_call(
                llm, text, len(sh),
                f"PART {i + 1} of {len(shards)} of the record (chronological) — "
                "propose AT MOST 60 pages for the most significant subjects you "
                "see here (people, recurring topics, real events); parts are "
                "merged afterwards, so skip marginal one-offs. ",
                workspace, cfg, trace, "low")
            if cached:
                cache_dir.mkdir(parents=True, exist_ok=True)
                _atomic_write(cached, out)
            return out

        candidates = []
        with ThreadPoolExecutor(max_workers=len(shards)) as pool:
            futures = [pool.submit(one_shard, i, sh) for i, sh in enumerate(shards)]
            for f in futures:
                candidates += (f.result().get("pages") or [])
        lines = "\n".join(f"- {c.get('id')} \"{c.get('title')}\" ({c.get('type')}) "
                           f"aliases={c.get('aliases')}" for c in candidates)
        merge_note = ("These candidate pages were proposed from chronological parts "
                      "of the record. MERGE them into one final tree: unify duplicates "
                      "(same subject proposed by several parts — union their aliases), "
                      "keep every distinct subject, exactly one page per human. ")
        out = _plan_call(llm, lines, len(candidates), merge_note, workspace, cfg, trace)
    pages, seen = [], set()
    for p in (out.get("pages") or []):
        pid = str(p.get("id", "")).strip()
        if _ID_RE.fullmatch(pid) and pid not in seen:
            seen.add(pid)
            pages.append({"id": pid, "title": str(p.get("title") or pid).strip(),
                          "type": pid.split("/")[0],
                          "aliases": [str(a).strip() for a in p.get("aliases", []) if str(a).strip()]})
    return pages


def review_plan(llm, state, workspace, cfg, trace) -> int:
    """One pass over the tree itself: merge duplicate pages, delete pages that
    shouldn't exist (junk subjects, fragments of one thing split across pages).
    Applied before routing, so absorbed pages never receive observations."""
    system = ("You review the page tree of a wiki about a group chat, as its editor. "
              "Find (1) DUPLICATES: pages that are the same subject under different "
              "names or split across type (merge into the best one — the absorbed "
              "page's aliases carry over), and fragments of one subject spread over "
              "several pages when one richer page would serve a reader better; "
              "(2) JUNK: pages that are not real subjects (a stray word mistaken "
              "for a person, a generic term with no group-specific meaning). "
              "Be conservative: only act where you are confident. JSON only.\n\n"
              + workspace)
    user = "PAGE TREE:\n" + "\n".join(
        f"- {pid} — {p['title']}" + (f" (aliases: {', '.join(p['aliases'])})" if p["aliases"] else "")
        for pid, p in sorted(state["pages"].items()))
    out = llm.complete_json(system, user, effort=cfg.effort,
                            schema=review_schema(state["pages"]), schema_name="review",
                            trace=trace, max_tokens=cfg.max_tokens, temperature=cfg.temperature)
    changed = 0
    for m in (out.get("merges") or []):
        keep = m.get("keep")
        for pid in (m.get("absorb") or []):
            if pid != keep and pid in state["pages"] and keep in state["pages"]:
                gone = state["pages"].pop(pid)
                kept = state["pages"][keep]
                kept["aliases"] = list(dict.fromkeys(
                    kept["aliases"] + [gone["title"]] + gone["aliases"]))
                changed += 1
    for pid in (out.get("deletes") or []):
        if pid in state["pages"]:
            state["pages"].pop(pid)
            changed += 1
    return changed


def extend_plan(llm, new_items, state, workspace, cfg, trace, effort=None) -> list:
    """Update-mode planning: given only the NEW observations, decide whether any
    genuinely new subject has emerged that deserves a page the tree doesn't have.
    Usually returns nothing — most new material belongs on existing pages. Very
    large updates are processed in slices so the call always fits the context."""
    budget = 280_000 * 4                                        # chars (~280k tokens)
    lines = [_obs_line(n, o) for n, (_, o) in enumerate(new_items)]
    if sum(len(l) + 1 for l in lines) > budget:
        added, start = [], 0
        while start < len(new_items):
            size, end = 0, start
            while end < len(new_items) and size + len(lines[end]) + 1 <= budget:
                size += len(lines[end]) + 1
                end += 1
            end = max(end, start + 1)                # a single oversize line still advances
            added += extend_plan(llm, new_items[start:end], state, workspace, cfg,
                                 trace, effort="low")   # scan-sized slice: cap reasoning burn
            start = end
        return added
    system = ("You maintain the page tree of an existing wiki about a group chat. "
              "Below are the tree and a batch of NEW observations. Propose a new "
              "page ONLY for a genuinely new subject with enough material to "
              "sustain an article — a new person entering the group's world, a new "
              "running joke, a new event. Material that belongs on an existing page "
              "needs nothing from you. Usually the answer is no new pages. "
              "Page ids are type/slug (person|topic|event). JSON only.\n\n" + workspace)
    user = ("PAGE TREE:\n" + _page_tree(state) + "\n\nNEW OBSERVATIONS:\n"
            + "\n".join(_obs_line(n, o) for n, (_, o) in enumerate(new_items)))
    out = llm.complete_json(system, user, effort=effort or cfg.effort, schema=plan_schema(),
                            schema_name="plan", trace=trace, max_tokens=cfg.max_tokens,
                            temperature=cfg.temperature)
    added = []
    for p in (out.get("pages") or []):
        pid = str(p.get("id", "")).strip()
        if _ID_RE.fullmatch(pid) and pid not in state["pages"]:
            state["pages"][pid] = {"id": pid, "title": str(p.get("title") or pid).strip(),
                                   "type": pid.split("/")[0],
                                   "aliases": [str(a).strip() for a in p.get("aliases", [])
                                               if str(a).strip()],
                                   "status": "pending", "obs": []}
            added.append(pid)
    return added


def route_batch(llm, batch, state, workspace, cfg, trace) -> dict:
    """batch: list of (key, obs). Returns {key: [page ids]} for every key."""
    entity_pages = {pid: p for pid, p in state["pages"].items() if p["type"] != "analysis"}
    system = (_prompt("route.md").replace("{workspace}", workspace)
              + "\n\nPAGE TREE:\n" + _page_tree({"pages": entity_pages}))
    lines = "\n".join(_obs_line(n, o) for n, (_, o) in enumerate(batch))
    user = f"OBSERVATIONS:\n{lines}\n\nRoute every observation."
    out = llm.complete_json(system, user, effort=cfg.effort,
                            schema=route_schema(entity_pages), schema_name="route",
                            trace=trace, max_tokens=cfg.max_tokens, temperature=cfg.temperature)
    routed = {}
    for a in (out.get("assignments") or []):
        n = a.get("n")
        if isinstance(n, int) and 0 <= n < len(batch):
            routed[batch[n][0]] = list(dict.fromkeys(a.get("pages") or []))
    for key, _ in batch:                      # unanswered observations stay unrouted
        routed.setdefault(key, None)
    return routed


def origins_table(state, keyed, by_id) -> str:
    """Deterministic ground truth for attribution: for every topic and event, the
    earliest recorded message among its routed observations. Injected into every
    writer so no two pages disagree about who said something first."""
    rows = []
    for pid, p in sorted(state["pages"].items()):
        if p["type"] == "person" or not p["obs"]:
            continue
        first = min((by_id[s] for k in p["obs"] for s in keyed[k]["sources"] if s in by_id),
                    key=lambda m: m.ts, default=None)
        if first is not None:
            text = (first.text or "").replace("\n", " ")[:70]
            rows.append(f'- {pid} "{p["title"]}": first recorded {first.ts:%Y-%m-%d} '
                        f'by {first.sender}: "{text}"')
    return "\n".join(rows)


def _repeats_itself(article, shingle=6, threshold=3) -> bool:
    """Deterministic redundancy check: does the draft repeat the same run of
    `shingle` words more than incidentally? This measures the defect itself, so
    the edit pass runs exactly when it has something to fix."""
    words = re.sub(r"\[#[^\]]*\]|[^a-z0-9 ]", "", article.lower()).split()
    shingles = [" ".join(words[i:i + shingle]) for i in range(len(words) - shingle)]
    counts = {}
    for s in shingles:
        counts[s] = counts.get(s, 0) + 1
    return sum(1 for c in counts.values() if c > 1) >= threshold


def _material(page, keyed, by_id, quotes_per_obs):
    material, allowed = [], set()
    for n, key in enumerate(page["obs"]):
        o = keyed[key]
        allowed.update(o["sources"])
        # source ids ride on the line itself, so the writer can cite every claim
        # even when quotes are shed for budget
        cites = ", ".join(f"#{s}" for s in o["sources"][:6])
        material.append(_obs_line(n, o) + " — " + o["detail"] + f" [{cites}]")
        if quotes_per_obs:
            material.extend(_quotes(o, by_id, quotes_per_obs))
    return material, allowed


def write_page(llm, pid, state, keyed, by_id, workspace, wiki_dir, origins, cfg, trace,
               fresh=False):
    """Returns (article, facts) — facts only for person pages. `fresh` ignores the
    existing article (a directed rewrite under current rules, not a revision)."""
    page = state["pages"][pid]
    person = page["type"] == "person"
    # a page with enormous material keeps every observation but sheds quotes
    # until it fits the budget — the observations carry the facts, the quotes
    # are enrichment. If it still exceeds the budget with no quotes at all, the
    # observations themselves are stride-sampled (chronology preserved) and the
    # cap is logged — never silent.
    for q in (cfg.quotes_per_obs, 2, 1, 0):
        material, allowed = _material(page, keyed, by_id, q)
        if sum(len(m) for m in material) // 4 <= cfg.material_budget:
            break
    else:
        q = 0
    size = sum(len(m) for m in material) // 4
    if size > cfg.material_budget:
        keep = max(1, int(len(page["obs"]) * cfg.material_budget / size))
        stride = [page["obs"][round(i * (len(page["obs"]) - 1) / max(keep - 1, 1))]
                  for i in range(keep)]
        capped = dict(page, obs=list(dict.fromkeys(stride)))
        material, allowed = _material(capped, keyed, by_id, 0)
        print(f"\n  [write] {pid}: material capped to {len(capped['obs'])} of "
              f"{len(page['obs'])} observations (budget)", flush=True)
    system = (_prompt("write.md").replace("{workspace}", workspace)
              + "\n\nPAGE TREE (for [[cross-links]]):\n" + _page_tree(state))
    user = (f"PAGE TO WRITE: {pid} — \"{page['title']}\" ({page['type']})"
            + (f" · aliases: {', '.join(page['aliases'])}" if page.get("aliases") else ""))
    if origins:
        user += "\n\nCANONICAL ORIGINS (earliest recorded uses — never contradict):\n" + origins
    existing = _page_path(wiki_dir, pid)
    if existing.exists() and not fresh:
        body = existing.read_text().split("---", 2)[-1].strip()
        user += f"\n\nEXISTING ARTICLE (revise with the new material):\n{body}"
        if page.get("audit_issues"):
            user += ("\n\nAUDIT FINDINGS — fix each of these in the revision:\n- "
                     + "\n- ".join(page["audit_issues"]))
    if page.get("corrections"):
        user += ("\n\nMAINTAINER-VERIFIED FACTS (authoritative — where the material "
                 "disagrees, these win). These are background premises, NOT subject "
                 "matter: never mention, acknowledge, or allude to them in the article. "
                 "No disambiguation asides, no 'is a distinct person from' notes, no "
                 "defensive clarifications — write exactly as if these facts had been "
                 "obvious from the start:\n- " + "\n- ".join(page["corrections"]))
    user += "\n\nMATERIAL (observations with original messages):\n" + "\n".join(material)
    out = llm.complete_json(system, user, effort=cfg.effort, schema=article_schema(person),
                            schema_name="article", trace=trace, max_tokens=cfg.max_tokens,
                            temperature=cfg.temperature)
    article = (out.get("article") or "").strip()
    if not article:
        raise ValueError("writer returned an empty article")
    facts = {k: v.strip() for k, v in (out.get("facts") or {}).items() if v and v.strip()}
    if cfg.edit_pass and _repeats_itself(article):
        # the draft measurably restates the same points — one editing pass
        # merges the repeats the writer let through.
        edited = llm.complete_json(_prompt("edit.md"), article, effort=cfg.effort,
                                   schema=article_schema(False), schema_name="article",
                                   trace=trace, max_tokens=cfg.max_tokens,
                                   temperature=cfg.temperature)
        article = (edited.get("article") or "").strip() or article
    return polish(article, allowed, set(state["pages"])), facts


def _page_path(wiki_dir: Path, pid: str) -> Path:
    return wiki_dir / (pid + ".md")


def _page_body(wiki_dir: Path, pid: str) -> str:
    return _page_path(wiki_dir, pid).read_text().split("---", 2)[-1].strip()


# ---------------------------------------------------------------- analyses
def analyses_schema(page_ids) -> dict:
    return {
        "type": "object", "additionalProperties": False, "required": ["analyses"],
        "properties": {"analyses": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["id", "title", "brief", "sources"],
            "properties": {
                "id": {"type": "string", "description": "analysis/slug, lowercase kebab"},
                "title": {"type": "string"},
                "brief": {"type": "string", "description": "one sentence: the essay's angle"},
                "sources": {"type": "array", "minItems": 2,
                            "items": {"type": "string", "enum": list(page_ids)},
                            "description": "the entity pages this essay draws on"},
            }}}},
    }


def plan_analyses(llm, state, wiki_dir, workspace, cfg, trace) -> list:
    """Decide the wiki's analytical essays — cross-cutting, anthropological pages
    written FROM the entity pages (their material is the wiki itself)."""
    written = {pid: p for pid, p in state["pages"].items()
               if p.get("status") == "written" and p["type"] != "analysis"}
    lines = []
    for pid, p in sorted(written.items()):
        heads = re.findall(r"(?m)^## (.+)$", _page_body(wiki_dir, pid))
        lines.append(f"- {pid} \"{p['title']}\" — sections: {', '.join(heads[:8])}")
    system = ("You plan the ANALYSIS pages of a wiki about a group chat — essays by "
              "its resident anthropologist. Entity pages (below) record what exists; "
              "analysis pages explain what it means: the humor system and how bits are "
              "born and die, the group's private language as a language, its eras, its "
              "philosophy and worldview, roles and status, how conflict works, how the "
              "group plans and decides. Propose 6-10 essays, each with a sharp angle "
              "(a thesis to investigate, not a category) and the source pages it draws "
              "on. JSON only.\n\n" + workspace)
    user = "ENTITY PAGES:\n" + "\n".join(lines)
    out = llm.complete_json(system, user, effort=cfg.effort,
                            schema=analyses_schema(written), schema_name="analyses",
                            trace=trace, max_tokens=cfg.max_tokens, temperature=cfg.temperature)
    added = []
    for a in (out.get("analyses") or [])[:10]:
        pid = str(a.get("id", "")).strip()
        if pid and not pid.startswith("analysis/"):
            pid = "analysis/" + pid
        srcs = [s for s in (a.get("sources") or []) if s in written]
        if _AID_RE.fullmatch(pid) and pid not in state["pages"] and len(srcs) >= 2:
            state["pages"][pid] = {"id": pid, "title": str(a.get("title") or pid).strip(),
                                   "type": "analysis", "aliases": [],
                                   "brief": str(a.get("brief", "")).strip(),
                                   "sources": srcs, "status": "pending", "obs": []}
            added.append(pid)
    return added


def write_analysis(llm, pid, state, wiki_dir, workspace, cfg, trace) -> str:
    page = state["pages"][pid]
    material, allowed = [], set()
    budget = cfg.material_budget * 4          # chars
    for src in page["sources"]:
        if not _page_path(wiki_dir, src).exists():
            continue
        body = _page_body(wiki_dir, src)[: budget // max(len(page["sources"]), 1)]
        allowed.update(int(i) for i in re.findall(r"\[#(\d+)", body))
        material.append(f"==== SOURCE PAGE [[{src}]] \"{state['pages'][src]['title']}\" ====\n{body}")
    system = (_prompt("analysis.md").replace("{workspace}", workspace)
              + "\n\nPAGE TREE (for [[cross-links]]):\n" + _page_tree(state))
    user = f"ESSAY TO WRITE: {pid} — \"{page['title']}\"\nANGLE: {page.get('brief', '')}"
    if page.get("audit_issues"):
        user += ("\n\nAUDIT FINDINGS from the previous version — do not repeat these "
                 "mistakes (cite only what the source pages actually support):\n- "
                 + "\n- ".join(page["audit_issues"]))
    user += "\n\n" + "\n\n".join(material)
    out = llm.complete_json(system, user, effort=cfg.effort, schema=article_schema(False),
                            schema_name="article", trace=trace, max_tokens=cfg.max_tokens,
                            temperature=cfg.temperature)
    article = (out.get("article") or "").strip()
    if not article:
        raise ValueError("writer returned an empty article")
    return polish(article, allowed, set(state["pages"]))


def _write_page_file(wiki_dir, pid, page, article, facts=None) -> None:
    path = _page_path(wiki_dir, pid)
    path.parent.mkdir(parents=True, exist_ok=True)
    aliases = json.dumps(page.get("aliases", []), ensure_ascii=False)
    front = (f"---\nid: {pid}\ntitle: {page['title']}\ntype: {page['type']}\n"
             f"aliases: {aliases}\nobservations: {len(page['obs'])}\n")
    if facts:
        front += f"facts: {json.dumps(facts, ensure_ascii=False)}\n"
    front += f"updated: {time.strftime('%Y-%m-%d')}\n---\n\n"
    tmp = path.with_suffix(".md.tmp")
    tmp.write_text(front + article + "\n")
    tmp.replace(path)


def rebuild_index(wiki_dir, state) -> None:
    by_type = {"person": [], "topic": [], "event": [], "analysis": []}
    for pid, p in sorted(state["pages"].items()):
        if p.get("status") == "written":
            by_type[p["type"]].append(f"- [{p['title']}]({pid}.md) · {len(p['obs'])} observations")
    parts = ["# Index\n"]
    for t, label in (("person", "People"), ("topic", "Topics"), ("event", "Events"),
                     ("analysis", "Analyses")):
        if by_type[t]:
            parts.append(f"\n## {label}\n\n" + "\n".join(by_type[t]))
    (wiki_dir / "index.md").write_text("\n".join(parts) + "\n")


# ---------------------------------------------------------------- questions
def questions_schema(page_ids) -> dict:
    ids = {"type": "string", "enum": list(page_ids)}
    return {
        "type": "object", "additionalProperties": False, "required": ["questions"],
        "properties": {"questions": {"type": "array", "maxItems": 8, "items": {
            "type": "object", "additionalProperties": False,
            "required": ["question", "kind", "subjects", "evidence"],
            "properties": {
                "question": {"type": "string", "description": "a yes/no question for the owner"},
                "kind": {"type": "string", "enum": ["same_person", "merge_pages"]},
                "subjects": {"type": "array", "minItems": 2, "maxItems": 2, "items": ids,
                             "description": "[keep, absorb] if the answer is yes"},
                "evidence": {"type": "string", "description": "why you suspect it"},
            }}}},
    }


def merge_pages(state, wiki_dir, keep, absorb) -> None:
    """Fold one page into another: union aliases and material, re-point routing,
    delete the absorbed page's file, and mark the keeper for rewrite."""
    gone = state["pages"].pop(absorb)
    kept = state["pages"][keep]
    kept["aliases"] = list(dict.fromkeys(kept["aliases"] + [gone["title"]] + gone["aliases"]))
    kept["obs"] = list(dict.fromkeys(kept["obs"] + gone["obs"]))
    kept["status"] = "pending"
    for key, pages in state["routed"].items():
        if absorb in pages:
            state["routed"][key] = list(dict.fromkeys(
                [keep if p == absorb else p for p in pages]))
    _page_path(wiki_dir, absorb).unlink(missing_ok=True)


def ask_questions(llm, state, wiki_dir, workspace, cfg, trace, verbose=True) -> None:
    """The human-feedback channel. Suspected same-person pairs and dubious page
    splits become questions in wiki/questions.json; the owner fills in `answer`
    ("yes"/"no"), and `apply_answers` reconciles on the next build."""
    path = wiki_dir / "questions.json"
    existing = json.loads(path.read_text()) if path.exists() else []
    asked = {tuple(sorted(q["subjects"])) for q in existing if q.get("subjects")}
    system = ("You maintain a wiki about a group chat. Look at the page tree and flag "
              "UNCERTAIN identity or structure questions worth asking the chat's owner: "
              "two person pages that might be the same human (a nickname and a name, "
              "similar labels, an outside name that matches a member), or two pages that "
              "might be one subject. Only genuinely uncertain, consequential cases — "
              "not things the tree already resolves via aliases. JSON only.\n\n" + workspace)
    out = llm.complete_json(system, "PAGE TREE:\n" + _page_tree(state), effort=cfg.effort,
                            schema=questions_schema(state["pages"]), schema_name="questions",
                            trace=trace, max_tokens=cfg.max_tokens, temperature=cfg.temperature)
    added = 0
    for q in (out.get("questions") or []):
        subjects = tuple(sorted(q.get("subjects", [])))
        if len(subjects) == 2 and subjects not in asked:
            existing.append({**q, "answer": ""})
            asked.add(subjects)
            added += 1
    if added or not path.exists():
        _atomic_write(path, existing)
    if verbose and added:
        print(f"[questions] {added} new questions → {path} (fill in \"answer\": yes/no)",
              flush=True)


def apply_answers(state, wiki_dir, verbose=True) -> int:
    path = wiki_dir / "questions.json"
    if not path.exists():
        return 0
    questions = json.loads(path.read_text())
    applied = 0
    for q in questions:
        if q.get("kind") == "face":                  # handled by `atlas faces`
            continue
        answer = str(q.get("answer", "")).strip().lower()
        if q.get("applied") or not answer:
            continue
        if answer in ("yes", "y") and q["kind"] in ("same_person", "merge_pages"):
            keep, absorb = q["subjects"]
            if keep in state["pages"] and absorb in state["pages"]:
                merge_pages(state, wiki_dir, keep, absorb)
                applied += 1
                if verbose:
                    print(f"[questions] merged {absorb} → {keep}", flush=True)
        q["applied"] = True
    _atomic_write(path, questions)
    return applied


# ---------------------------------------------------------------- audit
AUDIT_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["verdict", "issues"],
    "properties": {
        "verdict": {"type": "string", "enum": ["ok", "minor", "rewrite"]},
        "issues": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["kind", "detail"],
            "properties": {"kind": {"type": "string",
                                    "enum": ["accuracy", "redundancy", "structure"]},
                           "detail": {"type": "string"}}}},
    },
}


def audit_pages(chat_dir, config: ComposeConfig = None, verbose=True) -> dict:
    """Judge every written page against its own cited messages (accuracy),
    itself (redundancy), and the house style (structure). Findings are saved to
    wiki/audit.json; pages with a 'rewrite' verdict are marked pending with their
    issues attached, so the next build revises them with the findings in hand."""
    cfg = config or ComposeConfig()
    chat_dir = Path(chat_dir)
    wiki_dir = chat_dir / "wiki"
    state = _load_state(wiki_dir)
    data = json.loads((chat_dir / "observations.json").read_text())
    by_id = {m.rowid: m for m in fetch(data["chat_ids"])[0]}
    llm = LLMClient(cfg.model)
    pages = [pid for pid, p in state["pages"].items()
             if p.get("status") == "written" and _page_path(wiki_dir, pid).exists()]

    def one(pid):
        body = _page_body(wiki_dir, pid)
        ids = [int(i) for i in dict.fromkeys(re.findall(r"\[#(\d+)", body))][:150]
        cited = "\n".join(
            f'#{i} {by_id[i].sender} ({by_id[i].ts:%Y-%m-%d}): "{msg_text(by_id[i])[:400]}"'
            for i in ids if i in by_id)
        user = (f"ARTICLE ({pid}):\n{body}\n\nCITED MESSAGES (all shown, in article "
                f"order; judge only claims whose citations are all here):\n{cited}")
        out = llm.complete_json(_prompt("audit.md"), user, effort=cfg.effort,
                                schema=AUDIT_SCHEMA, schema_name="audit",
                                temperature=cfg.temperature, trace=None,
                                max_tokens=cfg.max_tokens)
        return {"page": pid, "verdict": out.get("verdict", "ok"),
                "issues": out.get("issues", [])}

    if verbose:
        print(f"[audit] {len(pages)} pages · x{cfg.workers or len(pages)}", flush=True)
    results = []
    with ThreadPoolExecutor(max_workers=cfg.workers or len(pages)) as pool:
        futures = {pool.submit(one, pid): pid for pid in pages}
        for fut in as_completed(futures):
            try:
                results.append(fut.result())
            except Exception as e:
                results.append({"page": futures[fut], "verdict": "error", "issues": [],
                                "error": str(e)[:200]})
    _atomic_write(wiki_dir / "audit.json", sorted(results, key=lambda r: r["page"]))
    counts = {}
    for r in results:
        counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1
    flagged = [r for r in results if r["verdict"] == "rewrite"]
    for r in flagged:
        page = state["pages"].get(r["page"])
        if page:
            page["status"] = "pending"
            page["audit_issues"] = [i["detail"] for i in r["issues"]][:8]
    _save_state(wiki_dir, state)
    if verbose:
        print(f"[audit] verdicts: {counts} → {wiki_dir/'audit.json'}", flush=True)
        for r in flagged:
            print(f"  rewrite: {r['page']} — {r['issues'][0]['detail'][:110] if r['issues'] else ''}",
                  flush=True)
        if flagged:
            print(f"[audit] {len(flagged)} pages marked pending — run `atlas wiki` to revise them",
                  flush=True)
    return counts


# ---------------------------------------------------------------- pipeline
def build_wiki(chat_dir, config: ComposeConfig = None, stage="all", limit_pages=None,
               only=None, verbose=True) -> dict:
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

    msgs, db = fetch(data["chat_ids"])
    by_id = {m.rowid: m for m in msgs}
    participants = sorted({m.sender for m in msgs if not m.system and m.sender})
    workspace = workspace_header(db, data["chat_ids"], msgs, participants)

    state = _load_state(wiki_dir)
    # re-extraction can reword observations near a grown chunk boundary; drop
    # references to observation keys that no longer exist (their replacements
    # arrive as new keys and route normally).
    stale = [k for k in state["routed"] if k not in keyed]
    for k in stale:
        del state["routed"][k]
    if stale:
        for p in state["pages"].values():
            p["obs"] = [k for k in p["obs"] if k in keyed]
        if verbose:
            print(f"[wiki] pruned {len(stale)} observations that no longer exist", flush=True)

    llm = LLMClient(cfg.model)
    t0 = time.time()
    if apply_answers(state, wiki_dir, verbose):
        _save_state(wiki_dir, state)

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
        pages = plan_pages(llm, [(k, keyed[k]) for k in order], workspace, cfg,
                           sink("plan"), trace_dir=chat_dir / "plan-shards")
        state["pages"] = {p["id"]: {**p, "status": "pending", "obs": []} for p in pages}
        _save_state(wiki_dir, state)
        if verbose:
            kinds = {t: sum(1 for p in state["pages"].values() if p["type"] == t)
                     for t in ("person", "topic", "event")}
            print(f"[plan] {len(state['pages'])} pages: {kinds}", flush=True)
    if not state.get("reviewed"):
        changed = review_plan(llm, state, workspace, cfg, sink("review"))
        state["reviewed"] = True
        _save_state(wiki_dir, state)
        if verbose:
            print(f"[review] merged/deleted {changed} pages → {len(state['pages'])} remain", flush=True)
    if stage == "plan":
        return state

    # ---- ROUTE
    todo = [k for k in order if k not in state["routed"]]
    # coverage high-water marks (message ids are chronological within each
    # million-id block): an observation whose sources are all below its block's
    # mark is reworded old material — it re-routes, but cannot introduce a new
    # subject, so it never needs the extend pass.
    marks = {int(b): m for b, m in (state.get("covered_blocks") or {}).items()}
    if not marks:                        # migrate: harvest from written articles
        for pid in state["pages"]:
            path = _page_path(wiki_dir, pid)
            for i in (re.findall(r"\[#(\d+)", path.read_text()) if path.exists() else []):
                i = int(i)
                marks[i // 1_000_000] = max(marks.get(i // 1_000_000, -1), i)
    fresh = [k for k in todo
             if any(s > marks.get(s // 1_000_000, -1) for s in keyed[k]["sources"])]
    if fresh and any(p["status"] == "written" for p in state["pages"].values()):
        # update mode: let genuinely new subjects earn new pages before routing
        if verbose and len(fresh) < len(todo):
            print(f"[plan] {len(todo)} unrouted observations — {len(fresh)} cite new "
                  f"messages and go to extend", flush=True)
        added = extend_plan(llm, [(k, keyed[k]) for k in fresh], state, workspace,
                            cfg, sink("extend"))
        if added:
            _save_state(wiki_dir, state)
            if verbose:
                print(f"[plan] new pages from update: {', '.join(added)}", flush=True)
    if todo:
        for k in todo:
            for src in keyed[k]["sources"]:
                marks[src // 1_000_000] = max(marks.get(src // 1_000_000, -1), src)
        state["covered_blocks"] = {str(b): m for b, m in marks.items()}
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
    if only:                              # targeted (re)write of specific pages
        pending = [pid for pid in only if pid in state["pages"] and state["pages"][pid]["obs"]]
    else:
        pending = [pid for pid, p in state["pages"].items()
                   if p["status"] == "pending" and len(p["obs"]) >= cfg.min_obs]
        thin = sum(1 for p in state["pages"].values()
                   if p["status"] == "pending" and 0 < len(p["obs"]) < cfg.min_obs)
        if thin and verbose:
            print(f"[write] skipping {thin} thin pages (< {cfg.min_obs} observations)", flush=True)
    if limit_pages:
        pending = pending[:limit_pages]
    if pending:
        origins = origins_table(state, keyed, by_id)
        workers = cfg.workers or len(pending)
        if verbose:
            print(f"[write] {len(pending)} pages · x{workers}", flush=True)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(write_page, llm, pid, state, keyed, by_id, workspace,
                                   wiki_dir, origins, cfg,
                                   sink("write-" + pid.replace("/", "-")),
                                   fresh=bool(only)): pid
                       for pid in pending}
            done = 0
            for fut in as_completed(futures):
                pid = futures[fut]
                try:
                    article, facts = fut.result()
                    _write_page_file(wiki_dir, pid, state["pages"][pid], article, facts)
                    state["pages"][pid]["status"] = "written"
                    state["pages"][pid].pop("audit_issues", None)
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

    # ---- ANALYSES (derived views over the written wiki; regenerate when any
    # source page is newer than the essay — deterministic trigger, no drift)
    if not any(p["type"] == "analysis" for p in state["pages"].values()):
        added = plan_analyses(llm, state, wiki_dir, workspace, cfg, sink("analyses-plan"))
        _save_state(wiki_dir, state)
        if verbose and added:
            print(f"[analyses] planned {len(added)}: {', '.join(added)}", flush=True)
    if only:
        # a targeted rewrite touches only the analyses explicitly asked for
        stale = [pid for pid in only if state["pages"].get(pid, {}).get("type") == "analysis"]
    else:
        stale = []
        for pid, p in state["pages"].items():
            if p["type"] != "analysis":
                continue
            path = _page_path(wiki_dir, pid)
            srcs = [_page_path(wiki_dir, s) for s in p["sources"]]
            if (not path.exists() or p.get("status") == "pending"
                    or any(s.exists() and s.stat().st_mtime > path.stat().st_mtime for s in srcs)):
                stale.append(pid)
    if stale:
        if verbose:
            print(f"[analyses] {len(stale)} essays to (re)write", flush=True)
        with ThreadPoolExecutor(max_workers=cfg.workers or len(stale)) as pool:
            futures = {pool.submit(write_analysis, llm, pid, state, wiki_dir, workspace,
                                   cfg, sink("write-" + pid.replace("/", "-"))): pid
                       for pid in stale}
            for fut in as_completed(futures):
                pid = futures[fut]
                try:
                    _write_page_file(wiki_dir, pid, state["pages"][pid], fut.result())
                    state["pages"][pid]["status"] = "written"
                    state["pages"][pid].pop("audit_issues", None)
                    _save_state(wiki_dir, state)
                except Exception as e:
                    print(f"\n  {pid} failed — {str(e)[:80]}", flush=True)

    ask_questions(llm, state, wiki_dir, workspace, cfg, sink("questions"), verbose)

    rebuild_index(wiki_dir, state)
    if verbose:
        written = sum(1 for p in state["pages"].values() if p["status"] == "written")
        print(f"[wiki] {written}/{len(state['pages'])} pages written → {wiki_dir} "
              f"· {llm.usage} · {time.time() - t0:.0f}s", flush=True)
    return state
