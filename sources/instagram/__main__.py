"""List the threads in an Instagram export, to pick what a wiki ingests.

    python3 -m sources.instagram chats [--match TEXT] [--root data/instagram]

Use a thread's key with atlas as `ig:<key>`, alongside iMessage chat ids.
"""
from __future__ import annotations

import argparse
import sys

from .reader import DEFAULT_ROOT, InstagramExport


def main(argv=None):
    p = argparse.ArgumentParser(prog="instagram")
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("chats", help="list threads in the export")
    c.add_argument("--match", help="filter by title/participant substring")
    c.add_argument("--root", default=str(DEFAULT_ROOT), help="unpacked export directory")
    c.add_argument("--limit", type=int, default=25)
    args = p.parse_args(argv)

    try:
        export = InstagramExport(args.root)
    except FileNotFoundError as exc:
        sys.exit(f"  {exc}")
    threads = export.threads()
    if args.match:
        needle = args.match.lower()
        threads = [t for t in threads if needle in t.title.lower()
                   or any(needle in x.lower() for x in t.participants)]
    fmt = "%Y-%m-%d"
    print(f"\n  {'msgs':>6}  {'span':<23}  key  (title · participants)")
    print("  " + "-" * 88)
    for t in threads[:args.limit]:
        span = f"{t.first:{fmt}} -> {t.last:{fmt}}" if t.first else "?"
        who = ", ".join(t.participants[:6]) + ("…" if len(t.participants) > 6 else "")
        print(f"  {t.message_count:>6}  {span:<23}  ig:{t.key}")
        print(f"          {t.title[:40]} · {who}")
    print()


if __name__ == "__main__":
    main()
