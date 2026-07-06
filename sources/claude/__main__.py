"""List Claude Code projects available for ingestion.

    python3 -m sources.claude projects [--match TEXT]
"""
from __future__ import annotations

import argparse

from .reader import ClaudeSessions


def main(argv=None):
    p = argparse.ArgumentParser(prog="sources.claude")
    sub = p.add_subparsers(dest="cmd", required=True)
    pr = sub.add_parser("projects", help="list projects (newest activity first)")
    pr.add_argument("--match")
    pr.add_argument("--limit", type=int, default=25)
    args = p.parse_args(argv)
    projects = ClaudeSessions().projects()
    if args.match:
        projects = [x for x in projects if args.match.lower() in x.slug.lower()]
    for x in projects[:args.limit]:
        print(f"  {x.sessions:>4} sessions  last {x.last:%Y-%m-%d}  claude:{x.slug}")


if __name__ == "__main__":
    main()
