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
from sources.imessage.render import format_message

INSTRUCTIONS = """This server is a cited wiki built from a real conversation history
(multiple sources merged into one timeline). Pages are markdown with [#id]
citations pointing at original messages.

Start every session by calling `overview` — it returns the page tree, the
sources, and freshness metadata. Read pages before searching; the wiki is the
synthesized layer and usually answers directly. Drop to `search` over
observations/messages only when the wiki lacks the detail, and use `resolve`
to quote original messages behind any citation.

Never suggest editing page files directly — pages are regenerated from
underlying data and hand-edits are lost. To fix anything wrong, call
`correct` with the fact in plain words; it becomes a durable premise."""


def build_server(chat_dir: Path) -> FastMCP:
    chat_dir = Path(chat_dir)
    wiki_dir = chat_dir / "wiki"
    if not (wiki_dir / "plan.json").exists():
        raise SystemExit(f"no built wiki at {wiki_dir} — run `atlas wiki` first")
    mcp = FastMCP(f"wiki-{chat_dir.name}", instructions=INSTRUCTIONS)
    cache = {}

    def data():
        return json.loads((chat_dir / "observations.json").read_text())

    def state():
        return json.loads((wiki_dir / "plan.json").read_text())

    def messages():
        mtime = (chat_dir / "observations.json").stat().st_mtime
        if cache.get("mtime") != mtime:            # wiki rebuilt → reload
            msgs, _ = fetch(data()["chat_ids"])
            cache.update(msgs=msgs, by_id={m.rowid: m for m in msgs}, mtime=mtime)
        return cache["msgs"], cache["by_id"]

    def page_files():
        return sorted(p for p in wiki_dir.rglob("*.md"))

    @mcp.tool()
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

    @mcp.tool()
    def find(query: str, max_results: int = 12) -> str:
        """Ranked page lookup (BM25, title-weighted) for natural-language queries
        ("the road trip where the car broke"). Use this when you don't know exact
        wording; use `search` for exact/regex matches."""
        hits = bm25_find(_bm25_index(), query, k=max_results)
        return "\n".join(f"{pid}  '{title}'  ({sc:.1f})" for sc, pid, title in hits) \
            or "nothing scored — try search()"

    @mcp.tool()
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
    def backlinks(page: str) -> str:
        """Every page that links TO this one — traverse the wiki as a graph
        (often surfaces context that text search misses)."""
        target = page.removesuffix(".md")
        rx = re.compile(r"\[\[" + re.escape(target) + r"[|\]]")
        out = [str(f.relative_to(wiki_dir).with_suffix(""))
               for f in page_files() if rx.search(f.read_text())]
        return "\n".join(out) or f"no pages link to {target}"

    @mcp.tool()
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

    @mcp.tool()
    def answer(question: str) -> str:
        """Synthesized, cited answer to a question — retrieves the most relevant
        pages, reads them, and writes the answer with [#id] citations plus an
        explicit note on what the knowledge base does NOT cover (gaps and
        staleness). Prefer this for direct questions; use read/search tools when
        you want to explore yourself."""
        from .llm import LLMClient
        top = find(question, max_results=5)
        if "nothing scored" in top:
            return "the knowledge base has nothing on this — note that as the answer"
        pids = [line.split()[0] for line in top.splitlines()]
        material = []
        for pid in pids[:4]:
            path = wiki_dir / (pid + ".md")
            if path.exists():
                text = path.read_text()
                material.append(f"=== {pid} (cited through {_freshness(text)}) ===\n"
                                + text[:9000])
        msgs, _ = messages()
        newest = max(m.ts for m in msgs)
        llm = LLMClient()
        out = llm.complete_json(
            "You answer questions from a cited personal knowledge base. Use ONLY the "
            "pages provided. Keep every [#id] citation that supports a claim you use. "
            "End with a short 'What this doesn't cover' note when relevant: gaps, and "
            "staleness (the record ends at the date given — anything after is unknown). "
            "Be direct and specific. JSON only.",
            f"QUESTION: {question}\n\nRECORD ENDS: {newest:%Y-%m-%d}\n\n"
            + "\n\n".join(material)
            + '\n\nReturn JSON: {"answer": "..."}',
            effort="low", temperature=0.2, max_tokens=4000)
        return str(out.get("answer", "")).strip() or "synthesis failed — read the pages directly"

    @mcp.tool()
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
