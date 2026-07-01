"""Chat Wiki CLI.

    python3 -m wiki build --match "book club"        # create + ingest (first run)
    python3 -m wiki build book-club                  # continue ingesting (delta)
    python3 -m wiki build book-club --chunks 3       # just a few chunks
    python3 -m wiki list                             # your wikis
    python3 -m wiki status book-club
    python3 -m wiki pages  book-club [--type person]
    python3 -m wiki show   book-club person/alice
    python3 -m wiki timeline book-club [--limit 40] [--page person/alice]
    python3 -m wiki verify book-club                 # citation + link integrity

A wiki lives at chats/<slug>/. `build` remembers its source chats + title in
state.json, so every later command only needs the slug.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .reduce import Runner
from .store import slugify

CHATS = Path("chats")


# ---------------------------------------------------------------- resolution
def _resolve_source(args):
    """Turn --chats/--group/--match into (chat_ids, title). Needs the source DB."""
    from imessage import MessagesDB
    db = MessagesDB()
    if args.chats:
        ids = [int(x) for x in args.chats.replace(",", " ").split()]
        by_id = {c.rowid: c for c in db.chats()}
        title = args.title or next((by_id[i].title for i in ids if i in by_id), "chat")
    elif args.group:
        ids, title = db.group(args.group), args.title or args.group
    elif args.match:
        needle = args.match.lower()
        hits = [c for c in db.chats() if needle in c.title.lower()]
        if not hits:
            sys.exit(f"  no chats match {args.match!r}")
        ids, title = [c.rowid for c in hits], args.title or hits[0].title
        print(f"  matched {len(hits)} chat row(s): {ids}  ({title})")
    else:
        sys.exit("  first build needs --chats, --group, or --match (plus optional --title)")
    return ids, title


def _open(slug: str, chunk_size=300) -> Runner:
    """Load an existing wiki by slug (from its state.json)."""
    import json
    state_file = CHATS / slug / "state.json"
    if not state_file.exists():
        sys.exit(f"  no wiki {slug!r} — run `build` first (see `wiki list`).")
    st = json.loads(state_file.read_text())
    return Runner(CHATS / slug, st["chat_ids"], st["title"],
                  model=st.get("model", "deepseek-v4-flash"), chunk_size=chunk_size)


# ---------------------------------------------------------------- commands
def cmd_build(args):
    slug = args.slug
    if slug and (CHATS / slug / "state.json").exists():
        r = _open(slug, args.size)                       # continue an existing wiki
    else:
        ids, title = _resolve_source(args)
        slug = slug or slugify(title)
        r = Runner(CHATS / slug, ids, title, model=args.model, chunk_size=args.size)
    print(f"  wiki: {slug}  ·  {r.title}  ·  chats {r.chat_ids}")
    r.ingest(after=_date(args.after), before=_date(args.before), max_chunks=args.chunks)
    probs = r.verify()
    print(f"\n  integrity: {'clean ✓' if not probs else str(len(probs)) + ' problem(s) — run `verify`'}")


def cmd_list(args):
    if not CHATS.exists():
        print("  no wikis yet.")
        return
    import json
    rows = []
    for d in sorted(CHATS.iterdir()):
        sf = d / "state.json"
        if sf.exists():
            st = json.loads(sf.read_text())
            pages = len(list((d / "kb").rglob("*.md"))) if (d / "kb").exists() else 0
            rows.append((d.name, st.get("title", ""), st.get("chunks_done", 0), pages))
    print(f"\n  {'slug':<22}  {'chunks':>6}  {'pages':>5}  title")
    print("  " + "-" * 60)
    for slug, title, chunks, pages in rows:
        print(f"  {slug:<22}  {chunks:>6}  {pages:>5}  {title}")
    print()


def cmd_status(args):
    r = _open(args.slug)
    st = r.load_state()
    pages = list(r.store.all_pages())
    by_type = {}
    for p in pages:
        by_type[p.type] = by_type.get(p.type, 0) + 1
    cites = sum(len(p.sources) for p in pages)
    print(f"\n  {r.title}  ({args.slug})")
    print(f"    chats:      {r.chat_ids}")
    print(f"    model:      {st.get('model')}")
    print(f"    chunks:     {st.get('chunks_done', 0)} done, watermark #{st.get('watermark', 0)}")
    print(f"    pages:      {len(pages)}  (" + ", ".join(f"{n} {t}" for t, n in sorted(by_type.items())) + ")")
    print(f"    citations:  {cites}")
    fails = st.get("failures", [])
    if fails:
        print(f"    failures:   {len(fails)} (last: {fails[-1]['span']})")
    print()


def cmd_pages(args):
    r = _open(args.slug)
    pages = [p for p in r.store.all_pages() if not args.type or p.type == args.type]
    pages.sort(key=lambda p: (p.type, -len(p.sources)))
    print()
    for p in pages:
        star = "★" if p.pinned else " "
        print(f"  {star} {p.id:<28} {len(p.sources):>3} cites  {p.title}")
    print()


def cmd_show(args):
    r = _open(args.slug)
    page = r.store.read(args.page)
    if page is None:
        sys.exit(f"  no page {args.page!r} (see `wiki pages {args.slug}`)")
    print(page.to_markdown())


def cmd_timeline(args):
    r = _open(args.slug)
    tl = r.timeline()
    if args.page:
        tl = [e for e in tl if e["page"] == args.page or e["page"].startswith(args.page)]
    if args.limit:
        tl = tl[-args.limit:]
    print()
    for e in tl:
        print(f"  {e['ts']:%Y-%m-%d %H:%M}  {e['page']:<24} #{e['message_id']}  {e['text'][:70]}")
    print(f"\n  {len(tl)} entries\n")


def cmd_verify(args):
    r = _open(args.slug)
    probs = r.verify()
    if not probs:
        print("  clean ✓ — every citation resolves, every link exists.")
    else:
        print(f"  {len(probs)} problem(s):")
        for p in probs[:50]:
            print(f"    - {p}")


def cmd_consolidate(args):
    r = _open(args.slug)
    print(f"  consolidating {r.title}…")
    done = r.consolidate(page_ids=[args.page] if args.page else None, min_cites=args.min_cites)
    probs = r.verify()
    print(f"\n  {len(done)} page(s) consolidated · integrity: "
          f"{'clean ✓' if not probs else str(len(probs)) + ' problem(s)'}")


def cmd_eval(args):
    from .eval import grounding, stats
    r = _open(args.slug)
    total = len(r._messages())
    probs = r.verify()
    s = stats(r.store, total)
    print(f"\n  {r.title}  ({args.slug})")
    print(f"    integrity:  {'clean ✓' if not probs else str(len(probs)) + ' problem(s)'}")
    print(f"    pages:      {s['pages']}  (" + ", ".join(f"{n} {t}" for t, n in sorted(s['by_type'].items())) + ")")
    print(f"    claims:     {s['claims']}  ·  {s['citations']} citations")
    print(f"    coverage:   {s['distinct_messages_cited']}/{total} messages cited ({s['coverage']:.1%})")
    if args.sample:
        from .llm import LLMClient
        judge = LLMClient(args.judge)
        print(f"\n    grounding (judge {args.judge}, n={args.sample})…")
        g = grounding(r, judge, n=args.sample)
        print(f"    supported:  {g['supported']}/{g['sampled']}  ({g['rate']:.0%})")
        for f in g["failures"]:
            print(f"      ✗ {f['page']}: {f['text'][:60]}  → {f['reason']}")
    print()


# ---------------------------------------------------------------- wiring
def _date(s):
    if not s:
        return None
    from datetime import datetime
    for fmt in ("%Y-%m-%d", "%Y-%m"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    sys.exit(f"  bad date {s!r} — use YYYY-MM-DD or YYYY-MM")


def main(argv=None):
    p = argparse.ArgumentParser(prog="wiki", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("build", help="create or continue a wiki (ingest messages)")
    b.add_argument("slug", nargs="?", help="wiki slug (omit on first build to derive from title)")
    b.add_argument("--chats", help="source chat rowids, comma-separated")
    b.add_argument("--group", help="named group from identities.json")
    b.add_argument("--match", help="find source chats by title substring")
    b.add_argument("--title", help="wiki title")
    b.add_argument("--after", help="only ingest messages after YYYY-MM-DD / YYYY-MM")
    b.add_argument("--before", help="only ingest messages up to YYYY-MM-DD / YYYY-MM")
    b.add_argument("--chunks", type=int, help="cap the number of chunks this run")
    b.add_argument("--size", type=int, default=300, help="messages per chunk (default 300)")
    b.add_argument("--model", default="deepseek-v4-flash", help="model key (see wiki/llm/config.py)")
    b.set_defaults(func=cmd_build)

    for name, fn, help_ in [("list", cmd_list, "list your wikis"),
                            ("status", cmd_status, "show a wiki's state"),
                            ("pages", cmd_pages, "list pages"),
                            ("verify", cmd_verify, "check citation + link integrity")]:
        s = sub.add_parser(name, help=help_)
        if name != "list":
            s.add_argument("slug")
        if name == "pages":
            s.add_argument("--type", help="filter by page type (person/event/topic)")
        s.set_defaults(func=fn)

    s = sub.add_parser("show", help="print a page")
    s.add_argument("slug")
    s.add_argument("page", help="page id, e.g. person/alice")
    s.set_defaults(func=cmd_show)

    s = sub.add_parser("timeline", help="derived timeline of cited claims")
    s.add_argument("slug")
    s.add_argument("--limit", type=int, help="show only the most recent N")
    s.add_argument("--page", help="filter to a page id or prefix")
    s.set_defaults(func=cmd_timeline)

    s = sub.add_parser("eval", help="integrity, coverage, and judged grounding")
    s.add_argument("slug")
    s.add_argument("--sample", type=int, default=0, help="judge N sampled claims for grounding")
    s.add_argument("--judge", default="deepseek-v4-flash", help="judge model key")
    s.set_defaults(func=cmd_eval)

    s = sub.add_parser("consolidate", help="refactor grown pages into clean, organized wholes")
    s.add_argument("slug")
    s.add_argument("--page", help="consolidate just one page id")
    s.add_argument("--min-cites", type=int, default=8, help="only pages with >= N citations")
    s.set_defaults(func=cmd_consolidate)

    args = p.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
