"""atlas CLI — the layered pipeline.

    atlas extract <slug> --chats 101,102     # Layer 1: chat → observations
    atlas extract <slug>                     # resume / retry failures
    atlas wiki <slug>                        # Layer 2: observations → wiki pages
    atlas wiki <slug> --stage plan           # run/inspect one sublayer at a time

Everything for a run lives in chats/<slug>/ (observations.json, manifest.json,
chunks/, traces/, wiki/). Any config knob is overridable via flags.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from collections import Counter
from dataclasses import fields
from pathlib import Path

from .caption import build_captions
from .compose import audit_pages, build_wiki
from .config import ComposeConfig, ExtractConfig
from .extract import build_observations
from .render import render_site


def _config_flags(parser, config_cls):
    """Every config field becomes a flag, defaults from the dataclass."""
    for f in fields(config_cls):
        flag = f"--{f.name.replace('_', '-')}"
        if isinstance(f.default, bool):
            parser.add_argument(flag, action=argparse.BooleanOptionalAction, default=f.default)
        else:
            parser.add_argument(flag, type=type(f.default), default=f.default,
                                help=f"default: {f.default}")


def _config_from(args, config_cls):
    return config_cls(**{f.name: getattr(args, f.name) for f in fields(config_cls)})


def _chat_ids(args, chat_dir: Path):
    if args.chats:
        return [int(x) for x in args.chats.replace(",", " ").split()]
    manifest = chat_dir / "manifest.json"
    if manifest.exists():                # resume: reuse the ids the run was started with
        ids = json.loads(manifest.read_text()).get("config", {}).get("chat_ids")
        if ids:
            return ids
    sys.exit("no --chats given and no prior run to resume — start with --chats <ids>")


def cmd_extract(args):
    chat_dir = Path("chats") / args.slug
    if args.redo:                        # mark specific chunks pending so the run redoes them
        path = chat_dir / "manifest.json"
        manifest = json.loads(path.read_text())
        for i in args.redo.replace(",", " ").split():
            manifest["chunks"][int(i)]["status"] = "pending"
        path.write_text(json.dumps(manifest, indent=2))
    observations = build_observations(chat_dir, _chat_ids(args, chat_dir),
                                      _config_from(args, ExtractConfig),
                                      resume=not args.fresh, limit_chunks=args.limit)
    types = Counter(o.type for o in observations)
    print("top types: " + ", ".join(f"{t} ({n})" for t, n in types.most_common(12)))


def cmd_wiki(args):
    chat_dir = Path("chats") / args.slug
    if args.questions:
        path = chat_dir / "wiki" / "questions.json"
        open_qs = [q for q in (json.loads(path.read_text()) if path.exists() else [])
                   if not q.get("applied") and not str(q.get("answer", "")).strip()]
        for q in open_qs:
            print(f"[{q['kind']}] {q['question']}\n  subjects: {q['subjects']}\n"
                  f"  evidence: {q['evidence']}\n")
        print(f"{len(open_qs)} open — answer by setting \"answer\": \"yes\"/\"no\" in {path}")
        return
    if args.audit:
        audit_pages(chat_dir, _config_from(args, ComposeConfig))
        return
    if args.fresh:
        shutil.rmtree(chat_dir / "wiki", ignore_errors=True)
    only = args.only.replace(",", " ").split() if args.only else None
    build_wiki(chat_dir, _config_from(args, ComposeConfig),
               stage=args.stage, limit_pages=args.pages, only=only)


def main(argv=None):
    p = argparse.ArgumentParser(prog="atlas", description="Build a cited wiki over a chat.")
    sub = p.add_subparsers(dest="cmd", required=True)

    e = sub.add_parser("extract", help="Layer 1: chat → cited observations")
    e.add_argument("slug", help="name of this workspace; everything lives in chats/<slug>/")
    e.add_argument("--chats", help="comma-separated chat row ids (see `imsg chats`); "
                                   "only needed on the first run")
    e.add_argument("--fresh", action="store_true", help="discard the existing run and redo it")
    e.add_argument("--redo", help="comma-separated chunk indexes to re-run (e.g. after inspecting)")
    e.add_argument("--limit", type=int, help="only run this many chunks (for trying things out)")
    _config_flags(e, ExtractConfig)
    e.set_defaults(fn=cmd_extract)

    w = sub.add_parser("wiki", help="Layer 2: observations → wiki pages")
    w.add_argument("slug", help="workspace under chats/<slug>/ (needs observations.json)")
    w.add_argument("--fresh", action="store_true", help="discard the existing wiki and redo it")
    w.add_argument("--stage", choices=["plan", "route", "all"], default="all",
                   help="stop after this sublayer (for inspection)")
    w.add_argument("--pages", type=int, help="only write this many pages (for trying things out)")
    w.add_argument("--only", help="comma-separated page ids to (re)write, e.g. person/alice")
    w.add_argument("--audit", action="store_true",
                   help="judge every page against its cited messages; flag pages for revision")
    w.add_argument("--questions", action="store_true",
                   help="show open questions for the owner (answer in wiki/questions.json)")
    _config_flags(w, ComposeConfig)
    w.set_defaults(fn=cmd_wiki)

    c = sub.add_parser("caption", help="caption image attachments so extraction sees pictures")
    c.add_argument("slug", help="workspace under chats/<slug>/ (chat ids from its manifest)")
    c.add_argument("--chats", help="comma-separated chat row ids (defaults to the manifest's)")
    c.add_argument("--model", default="gemini-flash")
    c.add_argument("--workers", type=int, default=32)
    c.add_argument("--limit", type=int, help="only caption this many (for trying things out)")
    c.set_defaults(fn=lambda a: build_captions(
        _chat_ids(a, Path("chats") / a.slug), model=a.model, workers=a.workers, limit=a.limit))

    r = sub.add_parser("render", help="render the wiki as a Wikipedia-style static site")
    r.add_argument("slug", help="workspace under chats/<slug>/ (needs a built wiki)")
    r.add_argument("--out", help="output directory (default: chats/<slug>/site)")
    r.set_defaults(fn=lambda a: render_site(Path("chats") / a.slug, out=a.out))

    args = p.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
