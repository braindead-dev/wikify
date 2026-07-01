"""Chat Wiki CLI — build a cited, Wikipedia-style wiki over a conversation.

    python3 -m wiki build --match "book club"     # create + build (first run)
    python3 -m wiki build book-club               # fold in new messages (delta)
    python3 -m wiki build book-club --limit 5     # just a few windows
    python3 -m wiki list
    python3 -m wiki status book-club
    python3 -m wiki pages  book-club [--type person]
    python3 -m wiki show   book-club person/alice
    python3 -m wiki verify book-club              # citation integrity
    python3 -m wiki eval   book-club [--sample 25]

A wiki lives at chats/<slug>/ (kb/ pages, limbo/ evidence, state.json). `build`
remembers its source chats + title, so later commands only need the slug.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .store import Store, slugify, verify

CHATS = Path("chats")


def _state(slug):
    f = CHATS / slug / "state.json"
    if not f.exists():
        sys.exit(f"  no wiki {slug!r} — run `build` first (see `wiki list`).")
    return json.loads(f.read_text())


def _store(slug):
    return Store(CHATS / slug / "kb")


def _valid_ids(chat_ids):
    """The set of citable message ids (for verify/eval), via the source DB."""
    from imessage import MessagesDB
    db = MessagesDB()
    return {m.rowid for m in db.messages(chat_ids)}


def _resolve_source(args):
    from imessage import MessagesDB
    db = MessagesDB()
    if args.chats:
        ids = [int(x) for x in args.chats.replace(",", " ").split()]
        by_id = {c.rowid: c for c in db.chats()}
        title = args.title or next((by_id[i].title for i in ids if i in by_id), "chat")
    elif args.group:
        ids, title = db.group(args.group), args.title or args.group
    elif args.match:
        hits = [c for c in db.chats() if args.match.lower() in c.title.lower()]
        if not hits:
            sys.exit(f"  no chats match {args.match!r}")
        ids, title = [c.rowid for c in hits], args.title or hits[0].title
        print(f"  matched {len(hits)} chat row(s): {ids}  ({title})")
    else:
        sys.exit("  first build needs --chats, --group, or --match (+ optional --title)")
    return ids, title


# ---------------------------------------------------------------- commands
def cmd_build(args):
    from .agent import build_wiki
    slug = args.slug
    if slug and (CHATS / slug / "state.json").exists():
        st = _state(slug)
        ids, title, model = st["chat_ids"], st["title"], st.get("model", args.model)
    else:
        ids, title = _resolve_source(args)
        slug, model = slug or slugify(title), args.model
    print(f"  wiki: {slug}  ·  {title}  ·  chats {ids}")
    build_wiki(CHATS / slug, ids, title, model=model, size=args.size,
               limit=args.limit, workers=args.workers)


def cmd_list(args):
    if not CHATS.exists():
        print("  no wikis yet.")
        return
    print(f"\n  {'slug':<20}  {'pages':>5}  {'windows':>7}  title")
    print("  " + "-" * 56)
    for d in sorted(CHATS.iterdir()):
        sf = d / "state.json"
        if sf.exists():
            st = json.loads(sf.read_text())
            pages = len(list((d / "kb").rglob("*.md"))) if (d / "kb").exists() else 0
            print(f"  {d.name:<20}  {pages:>5}  {len(st.get('scouted', [])):>7}  {st.get('title', '')}")
    print()


def cmd_status(args):
    st = _state(args.slug)
    store = _store(args.slug)
    pages = list(store.all_pages())
    by_type = {}
    for p in pages:
        by_type[p.type] = by_type.get(p.type, 0) + 1
    cites = sum(len(p.sources) for p in pages)
    print(f"\n  {st.get('title')}  ({args.slug})")
    print(f"    chats:      {st.get('chat_ids')}")
    print(f"    model:      {st.get('model')}")
    print(f"    windows:    {len(st.get('scouted', []))} scouted")
    print(f"    pages:      {len(pages)}  (" + ", ".join(f"{n} {t}" for t, n in sorted(by_type.items())) + ")")
    print(f"    citations:  {cites}")
    print()


def cmd_pages(args):
    pages = [p for p in _store(args.slug).all_pages()
             if not args.type or p.type == args.type]
    pages.sort(key=lambda p: (p.type, -len(p.sources)))
    print()
    for p in pages:
        print(f"  {p.id:<34} {len(p.sources):>3} cites  {p.title}")
    print()


def cmd_show(args):
    page = _store(args.slug).read(args.page)
    if page is None:
        sys.exit(f"  no page {args.page!r} (see `wiki pages {args.slug}`)")
    print(page.to_markdown())


def cmd_verify(args):
    st = _state(args.slug)
    resolves = _valid_ids(st["chat_ids"]).__contains__
    probs = verify(_store(args.slug), resolves)
    if not probs:
        print("  clean ✓ — every citation resolves, every link exists.")
    else:
        print(f"  {len(probs)} problem(s):")
        for p in probs[:60]:
            print(f"    - {p}")


def cmd_eval(args):
    from .eval import grounding, stats
    from .agent import Context
    st = _state(args.slug)
    ctx = Context(CHATS / args.slug, st["chat_ids"], st.get("model", "deepseek-v4-flash"))
    store = _store(args.slug)
    total = len(ctx.msgs)
    probs = verify(store, ctx.resolves)
    s = stats(store, total)
    print(f"\n  {st.get('title')}  ({args.slug})")
    print(f"    integrity:  {'clean ✓' if not probs else str(len(probs)) + ' problem(s)'}")
    print(f"    pages:      {s['pages']}  (" + ", ".join(f"{n} {t}" for t, n in sorted(s['by_type'].items())) + ")")
    print(f"    claims:     {s['claims']}  ·  {s['citations']} citations")
    print(f"    coverage:   {s['distinct_messages_cited']}/{total} messages cited")

    class _Runner:                       # grounding() wants .store and .db
        pass
    r = _Runner()
    r.store, r.db = store, ctx.db
    if args.sample:
        print(f"\n    grounding (judge, n={args.sample})…")
        g = grounding(r, ctx.llm, n=args.sample)
        print(f"    supported:  {g['supported']}/{g['sampled']}  ({g['rate']:.0%})")
        for f in g["failures"][:8]:
            print(f"      ✗ {f['page']}: {f['text'][:55]} → {f['reason'][:50]}")
    print()


# ---------------------------------------------------------------- wiring
def main(argv=None):
    p = argparse.ArgumentParser(prog="wiki", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("build", help="create or extend a wiki")
    b.add_argument("slug", nargs="?")
    b.add_argument("--chats"); b.add_argument("--group"); b.add_argument("--match")
    b.add_argument("--title")
    b.add_argument("--size", type=int, default=600, help="messages per window")
    b.add_argument("--limit", type=int, help="cap windows this run")
    b.add_argument("--workers", type=int, default=8, help="parallelism")
    b.add_argument("--model", default="deepseek-v4-flash")
    b.set_defaults(func=cmd_build)

    sub.add_parser("list", help="list your wikis").set_defaults(func=cmd_list)
    for name, fn in [("status", cmd_status), ("verify", cmd_verify)]:
        s = sub.add_parser(name); s.add_argument("slug"); s.set_defaults(func=fn)
    s = sub.add_parser("pages"); s.add_argument("slug")
    s.add_argument("--type"); s.set_defaults(func=cmd_pages)
    s = sub.add_parser("show"); s.add_argument("slug"); s.add_argument("page")
    s.set_defaults(func=cmd_show)
    s = sub.add_parser("eval"); s.add_argument("slug")
    s.add_argument("--sample", type=int, default=0); s.set_defaults(func=cmd_eval)

    args = p.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
