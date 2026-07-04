"""Fused CLI over every source — the source is just part of the id.

    python3 -m sources chats [--match TEXT]     # all chats, all sources
    python3 -m sources show <id> [--context N]  # resolve any citation id

Chat listings print the spec atlas takes (`512` for iMessage, `ig:<thread>` for
Instagram). `show` resolves iMessage row ids and Instagram synthetic ids alike.
"""
from __future__ import annotations

import argparse
import sys

from .imessage import MessagesDB
from .imessage.render import format_message
from .instagram import DEFAULT_ROOT, InstagramExport
from .instagram.reader import thread_block


def _identities():
    from pathlib import Path
    return "identities.json" if Path("identities.json").exists() else None


def cmd_chats(args):
    print("\n== iMessage ==")
    try:
        chats = MessagesDB(identities=_identities()).chats()
        if args.match:
            chats = [c for c in chats if args.match.lower() in c.title.lower()]
        for c in chats[:args.limit]:
            print(f"  {c.rowid:>5}  {c.message_count:>7} msgs  {c.title[:44]}")
    except (FileNotFoundError, PermissionError) as exc:
        print(f"  (unavailable: {exc})")
    print("\n== Instagram ==")
    try:
        threads = InstagramExport().threads()
        if args.match:
            threads = [t for t in threads if args.match.lower() in t.title.lower()
                       or any(args.match.lower() in p.lower() for p in t.participants)]
        for t in threads[:args.limit]:
            print(f"  {t.message_count:>7} msgs  {t.title[:34]:<34}  ig:{t.key}")
    except FileNotFoundError:
        print(f"  (no export unpacked at {DEFAULT_ROOT})")
    print()


def cmd_show(args):
    from .instagram.reader import ID_BASE
    if args.id < ID_BASE:
        db = MessagesDB(identities=_identities())
        try:
            window = db.message(args.id, context=args.context)
        except KeyError as exc:
            sys.exit(f"  {exc}")
    else:
        export = InstagramExport()
        candidates = [d.name for d in export.inbox.iterdir()
                      if thread_block(d.name) <= args.id < thread_block(d.name) + 1_000_000]
        window = None
        for key in candidates:                # hash blocks can collide; verify membership
            msgs = export.thread_messages(key)
            idx = next((i for i, m in enumerate(msgs) if m.rowid == args.id), None)
            if idx is not None:
                window = msgs[max(0, idx - args.context):idx + args.context + 1]
                break
        if window is None:
            sys.exit(f"  no Instagram message with id {args.id}")
    for m in window:
        marker = "►" if m.rowid == args.id else " "
        print(f"  {marker} {format_message(m, ids=True, with_date=True)}")


def main(argv=None):
    p = argparse.ArgumentParser(prog="sources")
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("chats", help="list chats across all sources")
    c.add_argument("--match")
    c.add_argument("--limit", type=int, default=25)
    c.set_defaults(fn=cmd_chats)
    s = sub.add_parser("show", help="resolve any citation id back to its message")
    s.add_argument("id", type=int)
    s.add_argument("--context", type=int, default=2)
    s.set_defaults(fn=cmd_show)
    args = p.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
