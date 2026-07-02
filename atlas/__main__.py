"""atlas CLI — run Layer 1 over a chat.

    python3 -m atlas <slug> --chats 101,102        # first run: which chat rows
    python3 -m atlas <slug>                        # later: resume / retry failures
    python3 -m atlas <slug> --fresh                # discard the run and redo it

Everything for a run lives in chats/<slug>/ (observations.json, manifest.json,
chunks/, traces/). Any ExtractConfig knob is overridable via flags.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from dataclasses import fields
from pathlib import Path

from .config import ExtractConfig
from .extract import build_observations


def _chat_ids(args, chat_dir: Path):
    if args.chats:
        return [int(x) for x in args.chats.replace(",", " ").split()]
    manifest = chat_dir / "manifest.json"
    if manifest.exists():                # resume: reuse the ids the run was started with
        ids = json.loads(manifest.read_text()).get("config", {}).get("chat_ids")
        if ids:
            return ids
    sys.exit("no --chats given and no prior run to resume — start with --chats <ids>")


def main(argv=None):
    p = argparse.ArgumentParser(prog="atlas", description="Build a cited wiki over a chat.")
    p.add_argument("slug", help="name of this wiki; everything lives in chats/<slug>/")
    p.add_argument("--chats", help="comma-separated chat row ids (see `imsg chats`); "
                                   "only needed on the first run")
    p.add_argument("--fresh", action="store_true", help="discard the existing run and redo it")
    p.add_argument("--redo", help="comma-separated chunk indexes to re-run (e.g. after inspecting)")
    p.add_argument("--limit", type=int, help="only run this many chunks (for trying things out)")
    for f in fields(ExtractConfig):      # every config knob is a flag, defaults from the dataclass
        flag = f"--{f.name.replace('_', '-')}"
        if isinstance(f.default, bool):
            p.add_argument(flag, action=argparse.BooleanOptionalAction, default=f.default)
        else:
            p.add_argument(flag, type=type(f.default), default=f.default,
                           help=f"default: {f.default}")
    args = p.parse_args(argv)

    chat_dir = Path("chats") / args.slug
    if args.redo:                        # mark specific chunks pending so the run redoes them
        path = chat_dir / "manifest.json"
        manifest = json.loads(path.read_text())
        for i in args.redo.replace(",", " ").split():
            manifest["chunks"][int(i)]["status"] = "pending"
        path.write_text(json.dumps(manifest, indent=2))
    config = ExtractConfig(**{f.name: getattr(args, f.name) for f in fields(ExtractConfig)})
    observations = build_observations(chat_dir, _chat_ids(args, chat_dir), config,
                                      resume=not args.fresh, limit_chunks=args.limit)
    types = Counter(o.type for o in observations)
    print("types: " + ", ".join(f"{t} ({n})" for t, n in types.most_common()))


if __name__ == "__main__":
    main()
