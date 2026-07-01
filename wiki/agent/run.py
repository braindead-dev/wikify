"""The orchestrator — a self-sustaining pipeline of agent duties over shared state.

    scouts (parallel, per time-window)  ->  limbo/     (capture cited evidence)
    plan   (one call over limbo)        ->  subjects   (what has matured?)
    curators (parallel, per subject)    ->  kb/        (deep articles from evidence)

Re-runnable: scouts skip windows already swept (watermark); curators re-synthesize
from the full limbo, so updates deepen articles instead of appending to them.
"""
from __future__ import annotations

import json
import re
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from imessage import MessagesDB
from imessage.render import format_message

from ..llm import LLMClient
from ..store import Page


def _today():
    return datetime.now().strftime("%Y-%m-%d")

_PROMPTS = Path(__file__).resolve().parent.parent / "prompts"
_REPO = Path(__file__).resolve().parents[2]


def _prompt(name):
    return (_PROMPTS / name).read_text()


class Context:
    """Shared services every session's Toolbox is built on."""
    def __init__(self, chat_dir, chat_ids, model):
        self.dir = Path(chat_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        ident = self.dir / "identities.json"
        if not ident.exists():
            ident.write_text("{}\n")
        self.ident = str(ident)
        self.chat_ids = list(chat_ids)
        self.title = self.dir.name
        self.llm = LLMClient(model)
        self._lock = threading.Lock()
        self.reload()

    def reload(self):
        """(Re)load messages with the current identities file — call after renames."""
        self.db = MessagesDB(identities=self.ident)
        self.msgs = self.db.messages(self.chat_ids)
        self.valid = {m.rowid for m in self.msgs}
        self.lines = [format_message(m, ids=True, with_date=True) for m in self.msgs]

    def resolves(self, mid):
        return int(mid) in self.valid

    def search_transcript(self, query):
        q = query.lower()
        hits = [ln for ln in self.lines if q in ln.lower()]
        return "\n".join(hits[:120]) or "(no matches)"

    def show(self, mid):
        try:
            return "\n".join(format_message(m, ids=True, with_date=True)
                             for m in self.db.message(int(mid), context=2))
        except KeyError:
            return f"#{mid} does not resolve"

    def imsg(self, args):
        cmd = ["python3", "-m", "imessage"]
        if self.ident:
            cmd += ["--identities", self.ident]
        cmd += list(args)
        with self._lock:
            r = subprocess.run(cmd, cwd=str(_REPO), capture_output=True, text=True, timeout=60)
        return (r.stdout + r.stderr).strip()[:2000]

    def toolbox(self):
        return Toolbox(self.dir, self.resolves, self.search_transcript, self.show, self.imsg)


def windows(messages, size=600):
    """Fixed-size, day-aligned slices — small enough for a fast, faithful scout
    completion, and many enough to parallelize well. Each gets a unique label."""
    out, cur = [], []
    for j, m in enumerate(messages):
        cur.append(m)
        last = j + 1 == len(messages)
        day_end = last or messages[j + 1].ts.date() != m.ts.date()
        if len(cur) >= size and day_end:
            out.append((f"{cur[0].ts:%Y-%m-%d}_{len(out):03d}", cur))
            cur = []
    if cur:
        out.append((f"{cur[0].ts:%Y-%m-%d}_{len(out):03d}", cur))
    return out


def resolve_identities(ctx, verbose=True):
    """One LLM pass over the intro-heavy start of the chat to find confident real
    names (who is "Me"; nickname -> full name), applied via the CLI. Runs FIRST so
    the rest of the pipeline uses real names. Best-effort; skips anything unsure."""
    sample = "\n".join(ln for ln in ctx.lines[:600])
    system = ("You resolve chat participant identities. Output JSON only. Only include "
              "a mapping when the chat clearly reveals it; skip anything uncertain.")
    user = (
        "Below is the start of a group chat where members introduce themselves and "
        "call each other by name. Map each chat label to the person's real name where "
        "it is clearly revealed — especially the literal label \"Me\" (the owner) and "
        "any nicknames or raw phone numbers. Do NOT guess. Skip usernames/gamertags.\n\n"
        'Return JSON: {"renames": [["chat label", "Real Name"]]}.\n\n' + sample[:60000])
    out = ctx.llm.complete_json(system, user)
    renames = out.get("renames", []) if isinstance(out, dict) else []
    # keep only clean 1:1 mappings: never rename the owner label "Me" (too error
    # prone, and merging it into a real person is catastrophic), and never map two
    # different labels to the same name (that merges distinct people).
    from collections import Counter
    clean = []
    for pair in renames:
        if isinstance(pair, list) and len(pair) == 2:
            old, new = str(pair[0]).strip(), str(pair[1]).strip()
            if old and new and old.lower() != "me" and old.lower() != new.lower():
                clean.append((old, new))
    dupes = {n for n, c in Counter(n for _, n in clean).items() if c > 1}
    applied = []
    for old, new in clean:
        if new not in dupes:
            ctx.imsg(["rename", old, new])
            applied.append((old, new))
    if applied:
        ctx.reload()
        if verbose:
            print(f"  identities: {', '.join(f'{o}->{n}' for o, n in applied)}", flush=True)
    return applied


def _state(ctx):
    f = ctx.dir / "state.json"
    return json.loads(f.read_text()) if f.exists() else {"scouted": []}


def _save_state(ctx, st):
    (ctx.dir / "state.json").write_text(json.dumps(st, indent=2))


# ---------------------------------------------------------------- duties
def scout(ctx, label, msgs, verbose=True):
    """A single extraction pass (no tools): read the slice, write cited notes to
    limbo. Fast, parallel, and never trips the tool-path refusal."""
    transcript = "\n".join(format_message(m, ids=True) for m in msgs)
    user = (f"Slice: {label}.\n\nTranscript:\n{transcript}\n\n"
            'Return JSON: {"notes": "<all your markdown notes for this slice>"}')
    try:
        out = ctx.llm.complete_json(_prompt("scout.md"), user, effort="medium")
        notes = out.get("notes", "") if isinstance(out, dict) else ""
    except Exception as e:
        print(f"  scout {label}: FAILED — {str(e)[:80]}", flush=True)
        return ""
    path = ctx.dir / "limbo" / f"{label}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(notes)
    if verbose:
        print(f"  scout {label}: {len(notes)} chars", flush=True)
    return notes


_TOPIC_KEYS = ("joke", "slang", "bit", "incident", "event", "dynamic", "running",
               "meme", "lore", "saga", "nickname", "tension", "obsession")


def plan(ctx, verbose=True):
    people = sorted({m.sender for m in ctx.msgs if not m.system})
    # The per-person notes are for the curators; the planner only needs the
    # topic/event/dynamic material — gathered from EVERY window, compactly.
    material = []
    for f in sorted((ctx.dir / "limbo").glob("*.md")):
        for sec in re.split(r"(?m)^(?=## )", f.read_text()):
            head = sec.split("\n", 1)[0].lower()
            if head.startswith("##") and any(k in head for k in _TOPIC_KEYS):
                material.append(sec.strip())
    notes = "\n".join(material)[:400000]
    system = "You plan a wiki from captured field notes about a group chat. JSON only."
    user = (
        "These are notes on the group's inside jokes, running bits, incidents, and "
        "dynamics, captured across the whole chat history. Decide which deserve their "
        "own article. Always include every PERSON in the roster: " + ", ".join(people) +
        ". Then be GENEROUS with TOPICS (each distinct inside joke, running bit, "
        "recurring obsession, or group dynamic that shows up more than once) and "
        "EVENTS (each specific incident, trip, fight, or milestone worth remembering). "
        "A rich 18-month friendship should yield dozens of topics and events, not a "
        "handful. Only skip a thread that appears once and never returns. Merge "
        "obvious duplicates.\n\n"
        'Return JSON: {"subjects": [{"type": "person|topic|event", "id": '
        '"type/slug", "title": "Readable Title"}]}\n\n'
        f"NOTES:\n{notes}")
    out = ctx.llm.complete_json(system, user)
    subjects = [s for s in (out.get("subjects", []) if isinstance(out, dict) else [])
                if isinstance(s, dict) and s.get("id") and s.get("type")]
    if verbose:
        kinds = {}
        for s in subjects:
            kinds[s.get("type")] = kinds.get(s.get("type"), 0) + 1
        print(f"  planned {len(subjects)} subjects: {kinds}")
    return subjects


_STOP = {"the", "and", "for", "with", "person", "topic", "event", "his", "her",
         "their", "who", "bit", "bits", "running", "group", "etc"}


def _keywords(subject):
    kws = {w for w in re.findall(r"[a-z]{3,}", (subject["title"] + " " + subject["id"]).lower())
           if w not in _STOP}
    kws.add(subject["title"].lower().strip())   # always include the title (handles short names like "Me")
    return kws


def _patterns(subject):
    # match keywords on WORD boundaries — otherwise a short two/three-letter name
    # matches "some"/"general" and drowns the curator in noise (→ 0 valid cites).
    return [re.compile(rf"\b{re.escape(w)}\b") for w in _keywords(subject)]


def _mentioned(subject, text):
    return any(p.search(text) for p in _patterns(subject))


_COMMON = {"me", "i", "an", "he", "she", "it", "we", "us", "him"}


def _gather(ctx, subject):
    """All limbo sections relevant to this subject (their own section plus any
    joke/event/dynamic that mentions them). Keeps each curator focused and bounded
    even when the full limbo is huge. Ultra-common keywords like the pronoun "me"
    only match a section's header (its own section), never body text."""
    kws = _keywords(subject)
    head_pats = [re.compile(rf"\b{re.escape(w)}\b") for w in kws]
    body_pats = [re.compile(rf"\b{re.escape(w)}\b") for w in kws - _COMMON]
    out = []
    for f in sorted((ctx.dir / "limbo").glob("*.md")):
        for sec in re.split(r"(?m)^(?=## )", f.read_text()):
            head, low = sec.split("\n", 1)[0].lower(), sec.lower()
            if any(p.search(head) for p in head_pats) or any(p.search(low) for p in body_pats):
                out.append(sec.strip())
    return ("\n\n".join(out))[:150000] or "(no notes)"


def curate(ctx, subject, verbose=True):
    """Synthesize one deep article from the subject's limbo evidence (a completion,
    not a tool loop — the evidence is already gathered by the scouts)."""
    limbo = _gather(ctx, subject)
    cur_path = ctx.dir / "kb" / f"{subject['id']}.md"
    current = cur_path.read_text() if cur_path.exists() else ""
    user = (
        f"Write the article for the {subject['type']} \"{subject['title']}\" "
        f"(page id {subject['id']}).\n\n"
        "CITED EVIDENCE — notes captured across the whole chat. Use everything "
        "relevant to this subject: their own section AND any joke, event, or dynamic "
        "that involves them.\n\n" + limbo[:160000] + "\n\n"
        "CURRENT ARTICLE (replace with a better whole; keep what's still true):\n"
        + (current or "(none yet)") + "\n\n"
        'Return JSON: {"article": "<the full markdown article body>"}')
    try:
        out = ctx.llm.complete_json(_prompt("writer.md"), user)
    except Exception as e:
        print(f"  curate {subject['id']}: FAILED — {str(e)[:80]}", flush=True)
        return None
    body = out.get("article", "") if isinstance(out, dict) else ""
    if not body.strip():
        return None
    body = _strip_bad_cites(body, ctx.resolves)
    page = Page(id=subject["id"], type=subject["type"], title=subject["title"],
                body=body.strip(), updated=_today())
    cur_path.parent.mkdir(parents=True, exist_ok=True)
    cur_path.write_text(page.to_markdown())
    if verbose:
        print(f"  curate {subject['id']}: {len(body)} chars, {len(page.sources)} cites", flush=True)
    return body


def _strip_bad_cites(body, resolves):
    """Drop unresolvable ids from citation brackets; remove a bracket if all bad."""
    import re

    def fix(m):
        good = [i for i in re.findall(r"\d+", m.group(0)) if resolves(int(i))]
        return "[" + ", ".join(f"#{i}" for i in good) + "]" if good else ""

    return re.sub(r"\[#\s*\d+(?:\s*,\s*#?\s*\d+)*\s*\]", fix, body)


def rebuild_index(ctx):
    """A deterministic hub page linking every article, grouped by type."""
    kb = ctx.dir / "kb"
    groups = [("People", "person"), ("Topics", "topic"), ("Events", "event")]
    lines = [f"# {ctx.title}\n"]
    for label, type_ in groups:
        pages = sorted((kb / type_).glob("*.md")) if (kb / type_).is_dir() else []
        if pages:
            lines.append(f"## {label}")
            for p in pages:
                title = next((l.split(":", 1)[1].strip() for l in p.read_text().splitlines()
                              if l.startswith("title:")), p.stem)
                lines.append(f"- [[{type_}/{p.stem}]] — {title}")
            lines.append("")
    (kb / "index.md").write_text("\n".join(lines))


# ---------------------------------------------------------------- pipeline
def build_wiki(chat_dir, chat_ids, title, model="deepseek-v4-flash",
               size=600, limit=None, workers=8, verbose=True):
    ctx = Context(chat_dir, chat_ids, model)
    ctx.title = title
    st = _state(ctx)
    st["chat_ids"], st["title"], st["model"] = ctx.chat_ids, title, model
    _save_state(ctx, st)

    if not st.get("identities_resolved"):
        if verbose:
            print("[identity] resolving real names…", flush=True)
        resolve_identities(ctx, verbose)
        st["identities_resolved"] = True
        _save_state(ctx, st)

    all_w = windows(ctx.msgs, size)
    todo = [(lab, m) for lab, m in all_w if lab not in st["scouted"]]
    if limit:
        todo = todo[:limit]

    if not todo and st.get("curated_once"):
        if verbose:
            print("  up to date — no new messages to ingest.", flush=True)
        return ctx

    if verbose:
        print(f"[scout] {len(todo)}/{len(all_w)} windows → limbo  (parallel x{workers})", flush=True)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(lambda w: scout(ctx, w[0], w[1], verbose), todo))
    st["scouted"] += [lab for lab, _ in todo]
    _save_state(ctx, st)

    if verbose:
        print("[plan] deciding which subjects have matured…", flush=True)
    subjects = plan(ctx, verbose)

    # On an update, only re-curate subjects that actually have NEW evidence — so a
    # small update maps to small work, not a full rewrite (P7, goal #3).
    if st.get("curated_once"):
        new_text = " ".join(
            (ctx.dir / "limbo" / f"{lab}.md").read_text().lower()
            for lab, _ in todo if (ctx.dir / "limbo" / f"{lab}.md").exists())
        subjects = [s for s in subjects if _mentioned(s, new_text)]
        if verbose:
            print(f"  update: {len(subjects)} subjects touched by new evidence", flush=True)

    if verbose:
        print(f"[curate] {len(subjects)} subjects → kb  (parallel x{workers})", flush=True)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(lambda s: curate(ctx, s, verbose), subjects))
    st["curated_once"] = True
    _save_state(ctx, st)

    rebuild_index(ctx)
    if verbose:
        print(f"\n  done · {ctx.llm.usage}", flush=True)
    return ctx
