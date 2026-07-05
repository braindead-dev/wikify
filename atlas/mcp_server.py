"""MCP server over a built wiki — the knowledge base as tools.

    python3 -m atlas mcp my-chat

Exposes any client-agnostic MCP host (Claude, ChatGPT, Cursor, …) to the wiki:
`overview` injects the dynamic context (page tree, sources, freshness), the
read/search/resolve tools mirror how coding agents explore a repository, and
`correct` is the ONLY write path — hand-edits to pages would be regenerated
away on the next build, while corrections attach durable premises that every
future rewrite honors.
"""
from __future__ import annotations

import json
import re
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from mcp.server.fastmcp import FastMCP, Image

from sources.fetch import fetch, parse_specs

from .retrieval import bm25_find, bm25_index
from .store_db import fts_search, load_items, log_access
from sources.imessage.render import format_message

INSTRUCTIONS = """{subject}

Cast and recurring subjects include: {cast}.
{npages} cited wiki pages built from {nobs} observations over the full history.

USE THESE TOOLS whenever the user mentions this group or any of these people,
or asks about their history, jokes, events, relationships, or anything this
corpus could know — none of it is in your training data, all of it is here.

Start with `overview` (page tree + freshness). For questions, call `context` and synthesize the answer yourself from the returned pages — cite the [#id]s you use. Read pages before searching —
the wiki is the synthesized layer and usually answers directly. Drop to
`search` over observations/messages when the wiki lacks detail; use `resolve`
to quote the original messages behind any [#id] citation.

Never suggest editing page files directly — pages are regenerated and
hand-edits are lost. To fix anything wrong, call `correct` with the true fact
in plain words; it becomes a durable premise."""


def _identity(chat_dir, wiki_dir, state) -> dict:
    """Compose the server's self-description FROM the wiki, so any host model
    can tell when this knowledge base is the right tool. Nothing hardcoded:
    the cast comes from the largest person pages, the subject line from the
    page that best matches the wiki's own name."""
    pages = {pid: p for pid, p in state["pages"].items() if p["status"] == "written"}
    cast = [t for _, t in sorted(((len(p["obs"]), p["title"]) for pid, p in pages.items()
                                  if p["type"] == "person" and pid.count("/") == 1),
                                 reverse=True)[:10]]
    subject = f"This server is the knowledge base of \"{chat_dir.name}\"."
    hits = bm25_find(bm25_index(state, wiki_dir), chat_dir.name.replace("-", " "), k=1)
    if hits:
        body = (wiki_dir / (hits[0][1] + ".md")).read_text().split("---", 2)[-1]
        para = next((ln.strip() for ln in body.split("\n\n") if len(ln.strip()) > 80), "")
        para = re.sub(r"\[#[0-9, #]+\]|\[\[([^|\]]+\|)?|\]\]|\*\*", "", para)
        if para:
            subject = ("This server is the collective memory and knowledge base of: "
                       + para[:420])
    return {"subject": subject, "cast": ", ".join(cast) or "(see overview)",
            "npages": len(pages), "nobs": "{:,}".format(
                sum(len(p["obs"]) for p in pages.values()))}


def build_server(chat_dir: Path) -> FastMCP:
    chat_dir = Path(chat_dir)
    wiki_dir = chat_dir / "wiki"
    if not (wiki_dir / "plan.json").exists():
        raise SystemExit(f"no built wiki at {wiki_dir} — run `atlas wiki` first")
    state0 = json.loads((wiki_dir / "plan.json").read_text())
    ident = _identity(chat_dir, wiki_dir, state0)
    short = ident["subject"].split(": ", 1)[-1].split(". ")[0][:180]
    mcp = FastMCP(f"{chat_dir.name}-wiki", instructions=INSTRUCTIONS.format(**ident))
    cache = {}

    def data():
        return json.loads((chat_dir / "observations.json").read_text())

    def state():
        return json.loads((wiki_dir / "plan.json").read_text())

    def _store_mtime():
        path = chat_dir / "store.db"
        return path.stat().st_mtime if path.exists() else 0.0

    def messages():
        mtime = _store_mtime() or (chat_dir / "observations.json").stat().st_mtime
        if cache.get("mtime") != mtime:            # wiki rebuilt → reload
            msgs = load_items(chat_dir)            # self-contained artifact first;
            if msgs is None:                       # live sources only as fallback
                msgs, _ = fetch(data()["chat_ids"])
            cache.update(msgs=msgs, by_id={m.rowid: m for m in msgs}, mtime=mtime)
        return cache["msgs"], cache["by_id"]

    def _logged(fn):
        import functools

        @functools.wraps(fn)
        def wrap(*a, **k):
            t0 = time.time()
            out = fn(*a, **k)
            summary = (out.splitlines()[0][:120] + f" ({len(out)}ch)"
                       if isinstance(out, str) else type(out).__name__)
            log_access(chat_dir, "mcp", fn.__name__, k or list(a), summary,
                       (time.time() - t0) * 1000)
            return out
        return wrap

    def page_files():
        return sorted(p for p in wiki_dir.rglob("*.md"))

    @mcp.tool(description=f"{short}. The whole knowledge base at a glance — call this "
              "first: sources, freshness, page tree, main-page summary.")
    @_logged
    def overview() -> str:
        """The whole knowledge base at a glance: sources, freshness, page tree
        (collapsed to directories when large), and the main-page summary. Call
        this first in every session."""
        s, d = state(), data()
        pages = {pid: p for pid, p in s["pages"].items() if p["status"] == "written"}
        im_ids, ig_keys, file_roots = parse_specs(d["chat_ids"])
        sources = ([f"iMessage chats {im_ids}"] if im_ids else []) + \
                  [f"Instagram thread {k}" for k in ig_keys] + \
                  [f"documents {r}" for r in file_roots]
        built = datetime.fromtimestamp((wiki_dir / "plan.json").stat().st_mtime)
        msgs, _ = messages()
        last_seen = {}
        for m in msgs:
            block = m.rowid // 1_000_000
            if m.ts > last_seen.get(block, datetime.min):
                last_seen[block] = m.ts
        freshest = max(last_seen.values()) if last_seen else None
        lines = [f"WIKI: {chat_dir.name} · {len(pages)} pages · "
                 f"{d['count']} observations from {', '.join(sources)}",
                 f"last build: {built:%Y-%m-%d %H:%M} · newest message: "
                 f"{freshest:%Y-%m-%d} · today: {datetime.now():%Y-%m-%d}",
                 "(claims about events after the newest message date are outside "
                 "this brain's knowledge)", ""]
        by_dir = {}
        for pid in pages:
            by_dir.setdefault(pid.split("/")[0], []).append(pid)
        for dirname, pids in sorted(by_dir.items()):
            if len(pages) > 400:                    # overflow: one line per directory
                lines.append(f"{dirname}/ — {len(pids)} pages (list_pages('{dirname}'))")
            else:
                lines.append(f"{dirname}/")
                lines += [f"  {pid}  \"{pages[pid]['title']}\"" for pid in sorted(pids)]
        index = wiki_dir / "index.md"
        if index.exists():
            lines += ["", "MAIN PAGE:", index.read_text()[:3000]]
        return "\n".join(lines)

    @mcp.tool()
    @_logged
    def list_pages(prefix: str = "") -> str:
        """List page ids (optionally under a prefix like 'person' or
        'topic/road') with titles and sizes."""
        s = state()
        out = []
        for pid, p in sorted(s["pages"].items()):
            if p["status"] == "written" and pid.startswith(prefix):
                path = wiki_dir / (pid + ".md")
                words = len(path.read_text().split()) if path.exists() else 0
                out.append(f"{pid}  \"{p['title']}\"  {words} words · {len(p['obs'])} obs")
        return "\n".join(out) or f"no pages under {prefix!r}"

    def _freshness(text: str) -> str:
        _, by_id = messages()
        stamps = [by_id[int(i)].ts for i in re.findall(r"\[#(\d+)", text)
                  if int(i) in by_id]
        return f"{max(stamps):%Y-%m-%d}" if stamps else "?"

    @mcp.tool()
    @_logged
    def read_page(page: str, offset: int = 0, limit: int = 300) -> str:
        """Read a page by id (e.g. 'person/alice'), `limit` lines from `offset`.
        Citations look like [#12345] — resolve them with `resolve`."""
        path = wiki_dir / (page.removesuffix(".md") + ".md")
        if not path.exists():
            return f"no page {page!r} — try list_pages()"
        text = path.read_text()
        head = f"(cited material through {_freshness(text)})\n" if offset == 0 else ""
        lines = text.splitlines()
        chunk = lines[offset:offset + limit]
        tail = f"\n… {len(lines) - offset - limit} more lines (offset={offset + limit})" \
            if len(lines) > offset + limit else ""
        return head + "\n".join(chunk) + tail

    @mcp.tool()
    @_logged
    def search(pattern: str, where: str = "wiki", max_results: int = 40,
               since: str = "", until: str = "") -> str:
        """Regex search (case-insensitive). `where` is one of: wiki (page text),
        observations (extracted facts), messages (raw conversation), all.
        `since`/`until` (YYYY-MM-DD) date-scope the messages stratum."""
        try:
            rx = re.compile(pattern, re.IGNORECASE)
        except re.error as e:
            return f"bad regex: {e}"
        hits = []

        def scan_wiki():
            for path in page_files():
                for n, line in enumerate(path.read_text().splitlines(), 1):
                    if rx.search(line):
                        hits.append(f"{path.relative_to(wiki_dir).with_suffix('')}:{n}: "
                                    f"{line.strip()[:200]}")

        def scan_obs():
            for o in data()["observations"]:
                blob = f"{o['title']} — {o.get('detail', '')}"
                if rx.search(blob):
                    hits.append(f"obs [{','.join(f'#{s}' for s in o['sources'][:3])}]: {blob[:200]}")

        def scan_msgs():
            plain = not re.search(r"[\\^$.|?*+()\[\]{}]", pattern)
            if plain and (chat_dir / "store.db").exists():
                found = fts_search(chat_dir, " ".join(
                    f'"{w}"' for w in pattern.split()), limit=max_results * 2,
                    since=since, until=until)
                hits.extend(f"#{m.rowid} {m.ts:%Y-%m-%d} {m.sender}: {m.text[:180]}"
                            for m in found)
                return
            msgs, _ = messages()
            lo = datetime.fromisoformat(since) if since else None
            hi = datetime.fromisoformat(until) if until else None
            for m in msgs:
                if (lo and m.ts < lo) or (hi and m.ts > hi):
                    continue
                if m.text and rx.search(m.text):
                    hits.append(f"#{m.rowid} {m.ts:%Y-%m-%d} {m.sender}: {m.text[:180]}")

        scans = {"wiki": [scan_wiki], "observations": [scan_obs], "messages": [scan_msgs],
                 "all": [scan_wiki, scan_obs, scan_msgs]}.get(where)
        if not scans:
            return "where must be one of: wiki, observations, messages, all"
        with ThreadPoolExecutor(max_workers=len(scans)) as pool:
            list(pool.map(lambda f: f(), scans))
        total = len(hits)
        return "\n".join(hits[:max_results]) + \
            (f"\n… {total - max_results} more (narrow the pattern)" if total > max_results else "") \
            if hits else "no matches"

    def _bm25_index():
        mtime = max((f.stat().st_mtime for f in page_files()), default=0)
        if cache.get("bm25_mtime") != mtime:
            cache.update(bm25=bm25_index(state(), wiki_dir), bm25_mtime=mtime)
        return cache["bm25"]

    @mcp.tool(description=f"Ranked page lookup in this knowledge base ({short}) for "
              "natural-language queries — use when you don't know exact wording.")
    @_logged
    def find(query: str, max_results: int = 12) -> str:
        """Ranked page lookup (BM25, title-weighted) for natural-language queries
        ("the road trip where the car broke"). Use this when you don't know exact
        wording; use `search` for exact/regex matches."""
        hits = bm25_find(_bm25_index(), query, k=max_results)
        return "\n".join(f"{pid}  '{title}'  ({sc:.1f})" for sc, pid, title in hits) \
            or "nothing scored — try search()"

    @mcp.tool()
    @_logged
    def related(page: str, max_results: int = 12) -> str:
        """Pages most connected to this one: outgoing links, backlinks, and pages
        sharing the most underlying observations — the graph neighborhood."""
        target = page.removesuffix(".md")
        st = state()
        if target not in st["pages"]:
            return f"no page {target!r}"
        scores = {}
        path = wiki_dir / (target + ".md")
        if path.exists():
            for mt in re.finditer(r"\[\[([^|\]]+)", path.read_text()):
                scores[mt.group(1)] = scores.get(mt.group(1), 0) + 3
        rx = re.compile(r"\[\[" + re.escape(target) + r"[|\]]")
        for f in page_files():
            pid = str(f.relative_to(wiki_dir).with_suffix(""))
            if pid != target and rx.search(f.read_text()):
                scores[pid] = scores.get(pid, 0) + 3
        mine = set(st["pages"][target].get("obs", []))
        if mine:
            for pid, pg in st["pages"].items():
                if pid != target and pg["status"] == "written":
                    shared = len(mine & set(pg.get("obs", [])))
                    if shared:
                        scores[pid] = scores.get(pid, 0) + min(shared, 10)
        ranked = sorted(((v, k) for k, v in scores.items() if k in st["pages"]), reverse=True)
        return "\n".join(f"{k}  ({v})" for v, k in ranked[:max_results]) or "no neighbors"

    @mcp.tool()
    @_logged
    def backlinks(page: str) -> str:
        """Every page that links TO this one — traverse the wiki as a graph
        (often surfaces context that text search misses)."""
        target = page.removesuffix(".md")
        rx = re.compile(r"\[\[" + re.escape(target) + r"[|\]]")
        out = [str(f.relative_to(wiki_dir).with_suffix(""))
               for f in page_files() if rx.search(f.read_text())]
        return "\n".join(out) or f"no pages link to {target}"

    @mcp.tool()
    @_logged
    def resolve(citation: int, context: int = 4) -> str:
        """Resolve a [#id] citation to the original message with surrounding
        conversation — the ground truth behind any wiki claim."""
        msgs, by_id = messages()
        if citation not in by_id:
            return f"no message #{citation}"
        idx = msgs.index(by_id[citation])
        out = []
        for m in msgs[max(0, idx - context):idx + context + 1]:
            marker = "►" if m.rowid == citation else " "
            out.append(f"{marker} {format_message(m, ids=True, with_date=True)}")
        return "\n".join(out)

    @mcp.tool()
    @_logged
    def get_image(message_id: int) -> Image:
        """Fetch the photo attached to a message (find message ids via search
        or page citations near [img: …] captions)."""
        _, by_id = messages()
        m = by_id.get(message_id)
        paths = [p for p in (m.attachment_paths if m else []) if p and Path(p).exists()]
        if not paths:
            raise ValueError(f"no image on message #{message_id}")
        src = Path(paths[0])
        if src.suffix.lower() in (".heic", ".heif") or src.stat().st_size > 1_500_000:
            tmp = Path("/tmp") / f"atlas-mcp-{src.stem}.jpg"
            subprocess.run(["sips", "-s", "format", "jpeg", "--resampleWidth", "1200",
                            str(src), "--out", str(tmp)], capture_output=True)
            if tmp.exists():
                src = tmp
        return Image(path=str(src))

    def _gather(question: str, k: int = 6) -> str:
        """The retrieval half shared by `context` and `answer`: the most relevant
        pages assembled into one block, each headed with its freshness."""
        top = find(question, max_results=k + 2)
        if "nothing scored" in top:
            return ""
        material = []
        for line in top.splitlines()[:k]:
            pid = line.split()[0]
            path = wiki_dir / (pid + ".md")
            if path.exists():
                text = path.read_text()
                material.append(f"=== {pid} (cited through {_freshness(text)}) ===\n"
                                + text[:8000])
        msgs, _ = messages()
        return (f"RECORD ENDS: {max(m.ts for m in msgs):%Y-%m-%d} — anything after "
                "this date is outside the knowledge base.\n\n" + "\n\n".join(material))

    @mcp.tool(description=f"Assembled context for any question about {short}: the most "
              "relevant pages, freshness-annotated, for YOU to synthesize from — "
              "you answer, it retrieves. Cite the [#id]s you use.")
    @_logged
    def context(question: str, max_pages: int = 6) -> str:
        """The most relevant pages for a question, assembled raw — no synthesis.
        Citations ([#id]) resolve via `resolve`."""
        return _gather(question, k=max_pages) or \
            "the knowledge base has nothing on this — note that as the answer"


    @mcp.tool()
    @_logged
    def correct(fact: str) -> str:
        """Fix something wrong in the wiki by stating the true fact in plain
        words (e.g. "X and Y are two different people"). Attaches a durable
        premise to the affected pages; the next build rewrites them as if the
        fact had always been known. This is the ONLY way to change pages."""
        from .corrections import add_correction
        pages = add_correction(chat_dir, fact, verbose=False)
        return (f"correction attached to: {', '.join(pages)} — pages will regenerate "
                f"on the next build (`atlas update {chat_dir.name}`)")

    return mcp


def serve(chat_dir):
    build_server(Path(chat_dir)).run()
