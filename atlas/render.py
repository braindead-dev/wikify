"""Render a built wiki into a Wikipedia-style static site — no LLM, fully
deterministic, isolated from the pipeline.

Reads `<chat_dir>/wiki/*/*.md` (+ plan.json for the tree, + the message DB to
resolve citations) and writes a self-contained site to `<chat_dir>/site/`:

- every `[#rowid]` citation becomes a numbered footnote whose Reference entry is
  the ORIGINAL message (sender, date, verbatim text), with hover preview
- `[[page]]` links are blue; links to planned-but-unwritten pages are red
- each page gets an infobox (type, aliases, observations, first/last cited),
  a Contents box, a Categories bar, and "What links here" backlinks
- index.html is the Main Page, with client-side search

Re-running rebuilds the folder from scratch (it is a pure function of the wiki).
"""
from __future__ import annotations

import html
import json
import re
import shutil
from pathlib import Path
from urllib.parse import urlsplit

from sources.fetch import fetch

from .compose import msg_text

CSS = """
*{box-sizing:border-box}
body{margin:0;font-family:sans-serif;font-size:14px;line-height:1.6;color:#202122;background:#f8f9fa}
a{color:#3366cc;text-decoration:none}a:hover{text-decoration:underline}
a.new{color:#dd3333}
.header{background:#fff;border-bottom:1px solid #a2a9b1;padding:10px 20px;display:flex;align-items:baseline;gap:18px;flex-wrap:wrap}
.header .logo{font-family:'Linux Libertine',Georgia,serif;font-size:22px;color:#202122;font-weight:normal}
.header .tagline{color:#54595d;font-size:12px}
.header .nav{margin-left:auto;font-size:13px}
.page{max-width:1000px;margin:14px auto;background:#fff;border:1px solid #a2a9b1;padding:24px 32px 40px}
h1{font-family:'Linux Libertine',Georgia,serif;font-size:28px;font-weight:normal;border-bottom:1px solid #a2a9b1;margin:0 0 4px;padding-bottom:4px}
.subtitle{color:#54595d;font-size:12px;margin-bottom:16px}
h2{font-family:'Linux Libertine',Georgia,serif;font-size:22px;font-weight:normal;border-bottom:1px solid #a2a9b1;margin:22px 0 10px;padding-bottom:3px}
h3{font-size:16px;margin:18px 0 8px}
p{margin:10px 0}
ul,ol{margin:10px 0 10px 26px;padding:0}
li{margin:4px 0}
sup.ref{font-size:11px;line-height:0}
sup.ref a{color:#3366cc}
.infobox{float:right;width:280px;margin:0 0 16px 20px;border:1px solid #a2a9b1;background:#f8f9fa;font-size:12.5px;border-spacing:6px 3px}
.infobox caption{font-weight:bold;font-size:14px;padding:6px 4px 2px}
.infobox th{text-align:left;vertical-align:top;white-space:nowrap;padding-right:8px}
.infobox td{vertical-align:top}
.toc{display:inline-block;border:1px solid #a2a9b1;background:#f8f9fa;padding:10px 18px 10px 10px;font-size:13px;margin:6px 0}
.toc .toctitle{font-weight:bold;text-align:center;margin-bottom:4px}
.toc ol{margin:0 0 0 20px}
.references{font-size:12.5px;column-count:2;column-gap:30px}
.references li{margin:3px 0}
.references .who{font-weight:bold}
.references .when{color:#54595d}
.catbar{border:1px solid #a2a9b1;background:#f8f9fa;padding:6px 10px;font-size:12.5px;margin-top:28px}
.linkshere{color:#54595d;font-size:12.5px;margin-top:10px}
.searchbox{padding:7px 10px;font-size:14px;border:1px solid #a2a9b1;width:min(420px,100%);margin:10px 0}
.cols{column-count:3;column-gap:28px}
.cols li{break-inside:avoid}
.stats{color:#54595d;font-size:12.5px}
@media(max-width:720px){.infobox{float:none;width:100%;margin:0 0 14px}.references{column-count:1}.cols{column-count:1}.page{padding:16px}}
"""

_CITE_RE = re.compile(r"\[#[0-9,#\s]+\]")
_LINK_RE = re.compile(r"\[\[([^\]|]+)(?:\|([^\]]+))?\]\]")
_MDLINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_ITAL_RE = re.compile(r"(?<!\*)\*([^*\n]+)\*(?!\*)")
_LINK_SCHEMES = {"http", "https", "mailto"}


def _frontmatter(text):
    _, _, rest = text.partition("---\n")
    head, _, body = rest.partition("\n---")
    meta = {}
    for line in head.splitlines():
        k, _, v = line.partition(":")
        meta[k.strip()] = v.strip()
    try:
        meta["aliases"] = json.loads(meta.get("aliases", "[]"))
    except json.JSONDecodeError:
        meta["aliases"] = []
    return meta, body.lstrip("\n")


class _Page:
    """Per-page render state: footnote numbering in order of first use."""

    def __init__(self, pid, by_id):
        self.pid = pid
        self.by_id = by_id
        self.refs = {}                       # rowid -> footnote number

    def cite(self, match):
        out = []
        for raw in re.findall(r"\d+", match.group(0)):
            rid = int(raw)
            if rid not in self.refs:
                self.refs[rid] = len(self.refs) + 1
            n = self.refs[rid]
            m = self.by_id.get(rid)
            tip = f"{m.sender} ({m.ts:%Y-%m-%d}): {msg_text(m)[:160]}" if m else f"message {rid}"
            out.append(f'<sup class="ref" id="use-{n}"><a href="#cite-{n}" '
                       f'title="{html.escape(tip, quote=True)}">[{n}]</a></sup>')
        return "".join(out)


def _inline(s, page, tree):
    s = html.escape(s, quote=False)
    s = _CITE_RE.sub(page.cite, s)
    def wikilink(m):
        pid, label = m.group(1), m.group(2)
        info = tree.get(pid)
        if not info:
            return html.escape(label or pid)
        text = html.escape(label or info["title"])
        if info.get("written"):
            up = "../" * (page.pid.count("/") if page.pid else 1)
            return f'<a href="{up}{pid}.html">{text}</a>'
        return (f'<a class="new" title="page not written (too few observations)">{text}</a>')

    def markdown_link(m):
        # Model output and imported documents are untrusted. Decode character
        # references before validating so `javascript&#58;...` cannot disguise a
        # dangerous scheme, then quote-escape the final attribute value.
        href = m.group(2).strip()
        for _ in range(3):
            decoded = html.unescape(href)
            if decoded == href:
                break
            href = decoded
        parsed = urlsplit(href)
        if ((parsed.scheme and parsed.scheme.lower() not in _LINK_SCHEMES)
                or (not parsed.scheme and parsed.netloc)):
            return m.group(1)
        href = html.escape(href.replace(".md", ".html"), quote=True)
        return f'<a href="{href}">{m.group(1)}</a>'

    s = _LINK_RE.sub(wikilink, s)
    s = _MDLINK_RE.sub(markdown_link, s)
    s = _BOLD_RE.sub(r"<b>\1</b>", s)
    s = _ITAL_RE.sub(r"<i>\1</i>", s)
    return s


def _body_html(body, page, tree):
    out, paras, sections = [], [], []

    def flush():
        if paras:
            out.append("<p>" + " ".join(paras) + "</p>")
            paras.clear()

    lines = body.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if not stripped:
            flush()
        elif stripped.startswith("#"):
            flush()
            level = min(len(stripped) - len(stripped.lstrip("#")), 3)
            text = stripped.lstrip("#").strip()
            anchor = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
            if level <= 2:
                sections.append((anchor, text))
            out.append(f'<h{max(level,2)} id="{anchor}">{_inline(text, page, tree)}</h{max(level,2)}>')
        elif re.match(r"^[-*]\s", stripped):
            flush()
            items = []
            while i < len(lines) and re.match(r"^[-*]\s", lines[i].strip()):
                items.append(f"<li>{_inline(lines[i].strip()[2:], page, tree)}</li>")
                i += 1
            out.append("<ul>" + "".join(items) + "</ul>")
            continue
        elif re.match(r"^\d+\.\s", stripped):
            flush()
            items = []
            while i < len(lines) and re.match(r"^\d+\.\s", lines[i].strip()):
                item = re.sub(r"^\d+\.\s*", "", lines[i].strip())
                items.append(f"<li>{_inline(item, page, tree)}</li>")
                i += 1
            out.append("<ol>" + "".join(items) + "</ol>")
            continue
        else:
            paras.append(_inline(stripped, page, tree))
        i += 1
    flush()
    return "\n".join(out), sections


def _toc(sections):
    if len(sections) < 3:
        return ""
    items = "".join(f'<li><a href="#{a}">{html.escape(t)}</a></li>' for a, t in sections)
    return f'<div class="toc"><div class="toctitle">Contents</div><ol>{items}</ol></div>'


def _references(page):
    if not page.refs:
        return ""
    items = []
    for rid, n in page.refs.items():
        m = page.by_id.get(rid)
        if m:
            text = html.escape(msg_text(m)[:220]) or "<i>[attachment]</i>"
            body = (f'<span class="who">{html.escape(m.sender)}</span> '
                    f'<span class="when">({m.ts:%Y-%m-%d %H:%M})</span>: “{text}”')
        else:
            body = f"message {rid}"
        items.append(f'<li id="cite-{n}"><a href="#use-{n}">↑</a> {body}</li>')
    return "<h2>References</h2><ol class=\"references\">" + "".join(items) + "</ol>"


_FACT_LABELS = [("full_name", "Full name"), ("born", "Born"), ("hometown", "Hometown"),
                ("education", "Education"), ("occupation", "Occupation"),
                ("relationship", "Partner"), ("family", "Family")]


def _infobox(pid, meta, page, tree):
    rows = [("Type", meta.get("type", "")),]
    aliases = [a for a in meta.get("aliases", []) if a and a != meta.get("title")]
    if aliases:
        rows.append(("Also known as", ", ".join(dict.fromkeys(aliases))))
    try:
        facts = json.loads(meta.get("facts", "{}"))
    except json.JSONDecodeError:
        facts = {}
    for key, label in _FACT_LABELS:
        if facts.get(key) and facts[key] != meta.get("title"):
            rows.append((label, facts[key]))
    if meta.get("observations") not in (None, "", "0"):
        rows.append(("Observations", meta["observations"]))
    times = sorted(page.by_id[r].ts for r in page.refs if r in page.by_id)
    if times:
        rows.append(("First cited", f"{times[0]:%b %d, %Y}"))
        rows.append(("Last cited", f"{times[-1]:%b %d, %Y}"))
    if meta.get("updated"):
        rows.append(("Updated", meta["updated"]))
    body = "".join(f"<tr><th>{k}</th><td>{html.escape(str(v))}</td></tr>" for k, v in rows)
    return (f'<table class="infobox"><caption>{html.escape(meta.get("title", pid))}</caption>'
            f"{body}</table>")


def _shell(title, wiki_title, content, depth):
    home = "../" * depth + "index.html"
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)} - {html.escape(wiki_title)}</title>
<style>{CSS}</style></head><body>
<div class="header"><span class="logo">{html.escape(wiki_title)}</span>
<span class="tagline">the free encyclopedia</span>
<span class="nav"><a href="{home}">Main page</a></span></div>
<div class="page">
{content}
</div></body></html>"""


def render_site(chat_dir, out=None, verbose=True):
    chat_dir = Path(chat_dir)
    wiki_dir = chat_dir / "wiki"
    site = Path(out) if out else chat_dir / "site"
    plan = json.loads((wiki_dir / "plan.json").read_text())
    data = json.loads((chat_dir / "observations.json").read_text())

    msgs, db = fetch(data["chat_ids"])
    by_id = {m.rowid: m for m in msgs}
    participants = sorted({m.sender for m in msgs if not m.system and m.sender})
    titles = ({c.rowid: c.title for c in db.chats() if c.rowid in set(data["chat_ids"])}
              if db else {})
    wiki_title = next((t for t in titles.values() if t), None) or chat_dir.name

    files = {p.relative_to(wiki_dir).with_suffix("").as_posix(): p
             for p in wiki_dir.rglob("*.md") if p.parent != wiki_dir}
    tree = {pid: {"title": p["title"], "type": p["type"], "written": pid in files}
            for pid, p in plan["pages"].items()}

    # backlinks: who links to whom
    links_to = {}
    raw = {pid: _frontmatter(path.read_text()) for pid, path in files.items()}
    for pid, (_, body) in raw.items():
        for m in _LINK_RE.finditer(body):
            links_to.setdefault(m.group(1), set()).add(pid)

    shutil.rmtree(site, ignore_errors=True)
    total_refs = 0
    for pid, (meta, body) in raw.items():
        page = _Page(pid, by_id)
        content, sections = _body_html(body, page, tree)
        total_refs += len(page.refs)
        up = "../" * pid.count("/")
        backs = sorted(links_to.get(pid, ()))
        linkshere = ""
        if backs:
            linkshere = ('<div class="linkshere">What links here: '
                         + " · ".join(f'<a href="{up}{b}.html">{html.escape(tree[b]["title"])}</a>'
                                           for b in backs if b in tree) + "</div>")
        cat = meta.get("type", "page")
        content = (f"<h1>{html.escape(meta.get('title', pid))}</h1>"
                   f'<div class="subtitle">From {html.escape(wiki_title)}, the free encyclopedia</div>'
                   + _infobox(pid, meta, page, tree) + _toc(sections) + content
                   + _references(page)
                   + f'<div class="catbar"><a href="{up}index.html#{cat}">Category: '
                     f'{html.escape(cat.capitalize())}</a></div>' + linkshere)
        path = site / (pid + ".html")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_shell(meta.get("title", pid), wiki_title, content,
                               depth=pid.count("/")))

    # ---- main page
    by_type = {"person": [], "topic": [], "event": [], "analysis": []}
    for pid in sorted(files):
        t = tree[pid]
        by_type.setdefault(t["type"], []).append(
            f'<li><a href="{pid}.html">{html.escape(t["title"])}</a></li>')
    span = f"{msgs[0].ts:%B %Y} – {msgs[-1].ts:%B %Y}"
    sections_html = "".join(
        f'<h2 id="{t}">{label} <span class="stats">({len(items)})</span></h2>'
        f'<ul class="cols">{"".join(items)}</ul>'
        for t, label, items in (("analysis", "Analyses", by_type["analysis"]),
                                ("person", "People", by_type["person"]),
                                ("topic", "Topics", by_type["topic"]),
                                ("event", "Events", by_type["event"])) if items)
    content = (f"<h1>Main Page</h1>"
               f'<div class="subtitle">Welcome to {html.escape(wiki_title)}, '
               f"the encyclopedia of a group chat of {len(participants)} people, "
               f"{span} · {len(msgs):,} messages · {len(files)} articles · "
               f"{total_refs:,} references</div>"
               f'<input class="searchbox" id="q" placeholder="Search {len(files)} articles…" '
               f'oninput="f()" autofocus>' + sections_html +
               "<script>function f(){var q=document.getElementById('q').value.toLowerCase();"
               "document.querySelectorAll('.cols li').forEach(function(li){"
               "li.style.display=li.textContent.toLowerCase().includes(q)?'':'none'})}</script>")
    (site / "index.html").write_text(_shell("Main Page", wiki_title, content, depth=0))

    if verbose:
        print(f"[render] {len(files)} articles + main page → {site} "
              f"· {total_refs:,} resolved references", flush=True)
    return site
