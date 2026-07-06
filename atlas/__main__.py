"""atlas CLI — the layered pipeline.

    atlas extract <slug> --chats 101,102     # Layer 1: chat → observations
    atlas extract <slug>                     # resume / retry failures
    atlas wiki <slug>                        # Layer 2: observations → wiki pages
    atlas wiki <slug> --stage plan           # run/inspect one sublayer at a time

Everything for a run lives in wikis/<slug>/ (observations.json, manifest.json,
chunks/, traces/, wiki/). Any config knob is overridable via flags.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from collections import Counter
from dataclasses import fields
from pathlib import Path

from .caption import build_captions
from .transcribe import build_transcripts
from .bench import run_bench
from .store_db import read_log
from .compose import audit_pages, build_wiki, replan
from .grants import ALL_TOOLS, create_grant, list_grants, revoke_grant
from .corrections import add_correction
from .config import ComposeConfig, ExtractConfig, FaceConfig
from .faces import build_faces
from .extract import build_observations
from .render import render_site
from .sync import sync_site
from .mcp_server import serve


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
        return [int(x) if x.isdigit() else x
                for x in args.chats.replace(",", " ").split()]
    manifest = chat_dir / "manifest.json"
    if manifest.exists():                # resume: reuse the ids the run was started with
        ids = json.loads(manifest.read_text()).get("config", {}).get("chat_ids")
        if ids:
            return ids
    sys.exit("no --chats given and no prior run to resume — start with --chats <ids> "
             "(iMessage row ids and/or ig:<thread> keys)")


def cmd_extract(args):
    chat_dir = Path("wikis") / args.slug
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
    chat_dir = Path("wikis") / args.slug
    if args.questions:
        path = chat_dir / "wiki" / "questions.json"
        open_qs = [q for q in (json.loads(path.read_text()) if path.exists() else [])
                   if not q.get("applied") and not str(q.get("answer", "")).strip()]
        for q in open_qs:
            print(f"[{q['kind']}] {q['question']}\n"
                  f"  subjects: {q.get('subjects') or q.get('face_id', '-')}\n"
                  f"  evidence: {q.get('evidence', '-')}\n")
        print(f"{len(open_qs)} open — answer by setting \"answer\": \"yes\"/\"no\" in {path}")
        return
    if args.audit:
        audit_pages(chat_dir, _config_from(args, ComposeConfig))
        return
    if args.replan:
        replan(chat_dir, _config_from(args, ComposeConfig))
        return
    if args.fresh:
        shutil.rmtree(chat_dir / "wiki", ignore_errors=True)
    only = args.only.replace(",", " ").split() if args.only else None
    build_wiki(chat_dir, _config_from(args, ComposeConfig),
               stage=args.stage, limit_pages=args.pages, only=only)


_INTERVALS = {"m": 60, "h": 3600, "d": 86400}


def cmd_update(args):
    """One incremental pass of the whole pipeline: caption new images, extract
    new/changed chunks, fold into the wiki, render, and sync when configured.
    Every stage is a no-op when nothing changed."""
    import sys as _sys
    chat_dir = Path("wikis") / args.slug
    root = Path(__file__).resolve().parents[1]
    plist = Path.home() / "Library" / "LaunchAgents" / f"com.atlas.update.{args.slug}.plist"
    if args.unschedule:
        subprocess.run(["launchctl", "unload", str(plist)], capture_output=True)
        plist.unlink(missing_ok=True)
        print(f"[update] schedule removed ({plist.name})")
        return
    if args.schedule:
        n, unit = int(args.schedule[:-1]), args.schedule[-1]
        secs = n * _INTERVALS[unit]
        log = (root / chat_dir / "update.log").resolve()
        plist.parent.mkdir(parents=True, exist_ok=True)
        plist.write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.atlas.update.{args.slug}</string>
  <key>ProgramArguments</key><array>
    <string>{_sys.executable}</string><string>-m</string><string>atlas</string>
    <string>update</string><string>{args.slug}</string>
  </array>
  <key>WorkingDirectory</key><string>{root}</string>
  <key>StartInterval</key><integer>{secs}</integer>
  <key>StandardOutPath</key><string>{log}</string>
  <key>StandardErrorPath</key><string>{log}</string>
</dict></plist>
""")
        subprocess.run(["launchctl", "unload", str(plist)], capture_output=True)
        r = subprocess.run(["launchctl", "load", str(plist)], capture_output=True, text=True)
        print(f"[update] scheduled every {args.schedule} via launchd ({plist.name})"
              + (f" — load warning: {r.stderr.strip()}" if r.stderr.strip() else ""))
        print(f"  log: {log}")
        print("  note: the scheduled python needs Full Disk Access to read chat.db "
              "(System Settings → Privacy & Security)")
        return
    ids = _chat_ids(args, chat_dir)
    build_captions(ids)
    build_transcripts(ids)
    build_observations(chat_dir, ids, ExtractConfig())
    build_wiki(chat_dir, ComposeConfig())
    if (chat_dir / "sync.json").exists() or args.to:
        sync_site(chat_dir, to=args.to)
    else:
        render_site(chat_dir)


def _print_grant(g):
    print(f"grant '{g['name']}' → wiki {g['wiki']} · tools: {', '.join(g['tools'])}"
          + (f" · expires {g['expires']}" if g['expires'] else ""))
    print(f"token: {g['token']}")
    print(f"serve: python3 -m atlas mcp {g['wiki']} --grant {g['token']}")


def main(argv=None):
    p = argparse.ArgumentParser(prog="atlas", description="Build a cited wiki over a chat.")
    sub = p.add_subparsers(dest="cmd", required=True)

    e = sub.add_parser("extract", help="Layer 1: chat → cited observations")
    e.add_argument("slug", help="name of this workspace; everything lives in wikis/<slug>/")
    e.add_argument("--chats", help="comma-separated chat row ids (see `imsg chats`); "
                                   "only needed on the first run")
    e.add_argument("--fresh", action="store_true", help="discard the existing run and redo it")
    e.add_argument("--redo", help="comma-separated chunk indexes to re-run (e.g. after inspecting)")
    e.add_argument("--limit", type=int, help="only run this many chunks (for trying things out)")
    _config_flags(e, ExtractConfig)
    e.set_defaults(fn=cmd_extract)

    w = sub.add_parser("wiki", help="Layer 2: observations → wiki pages")
    w.add_argument("slug", help="workspace under wikis/<slug>/ (needs observations.json)")
    w.add_argument("--fresh", action="store_true", help="discard the existing wiki and redo it")
    w.add_argument("--stage", choices=["plan", "route", "all"], default="all",
                   help="stop after this sublayer (for inspection)")
    w.add_argument("--pages", type=int, help="only write this many pages (for trying things out)")
    w.add_argument("--only", help="comma-separated page ids to (re)write, e.g. person/alice")
    w.add_argument("--replan", action="store_true",
                   help="restructure audit: merge/retitle/delete ops + inconsistency report")
    w.add_argument("--audit", action="store_true",
                   help="judge every page against its cited messages; flag pages for revision")
    w.add_argument("--questions", action="store_true",
                   help="show open questions for the owner (answer in wiki/questions.json)")
    _config_flags(w, ComposeConfig)
    w.set_defaults(fn=cmd_wiki)

    c = sub.add_parser("caption", help="caption image attachments so extraction sees pictures")
    c.add_argument("slug", help="workspace under wikis/<slug>/ (chat ids from its manifest)")
    c.add_argument("--chats", help="comma-separated chat row ids (defaults to the manifest's)")
    c.add_argument("--model", default="gemini-flash")
    c.add_argument("--workers", type=int, default=32)
    c.add_argument("--limit", type=int, help="only caption this many (for trying things out)")
    c.set_defaults(fn=lambda a: build_captions(
        _chat_ids(a, Path("wikis") / a.slug), model=a.model, workers=a.workers, limit=a.limit))

    tr = sub.add_parser("transcribe", help="transcribe audio attachments so extraction hears them")
    tr.add_argument("slug", help="workspace under wikis/<slug>/ (chat ids from its manifest)")
    tr.add_argument("--chats", help="comma-separated chat specs (defaults to the manifest's)")
    tr.add_argument("--model", default="gemini-flash")
    tr.add_argument("--workers", type=int, default=16)
    tr.add_argument("--limit", type=int)
    tr.set_defaults(fn=lambda a: build_transcripts(
        _chat_ids(a, Path("wikis") / a.slug), model=a.model, workers=a.workers, limit=a.limit))

    f = sub.add_parser("faces", help="cluster faces in photos; owner names them via questions")
    f.add_argument("slug", help="workspace under wikis/<slug>/")
    f.add_argument("--chats", help="comma-separated chat row ids (defaults to the manifest's)")
    _config_flags(f, FaceConfig)
    f.set_defaults(fn=lambda a: build_faces(_chat_ids(a, Path("wikis") / a.slug),
                                            Path("wikis") / a.slug / "wiki",
                                            _config_from(a, FaceConfig)))

    lg = sub.add_parser("log", help="view the access audit trail (who/what/when via every channel)")
    lg.add_argument("slug")
    lg.add_argument("--tail", type=int, default=30)
    lg.set_defaults(fn=lambda a: [print(f"{r[0]}  [{r[1]}] {r[2]}/{r[3]}  {r[4][:52]}  → {r[5][:64]}  {r[6]}ms")
                                  for r in read_log(a.slug, a.tail)] and None)

    be = sub.add_parser("bench", help="retrieval benchmark: LLM probes → hit@1/hit@5/MRR")
    be.add_argument("slug")
    be.add_argument("--n", type=int, default=40)
    be.set_defaults(fn=lambda a: run_bench(Path("wikis") / a.slug, n=a.n))

    m = sub.add_parser("mcp", help="serve the wiki as an MCP server (stdio) for any AI client")
    m.add_argument("slug", help="workspace under wikis/<slug>/ (needs a built wiki)")
    m.add_argument("--grant", help="serve under a grant token (restricted tools, attributed audit)")
    m.set_defaults(fn=lambda a: serve(Path("wikis") / a.slug, grant_token=a.grant))

    gr = sub.add_parser("grant", help="mint provisioned access to a wiki (token + tool subset)")
    gr.add_argument("slug")
    gr.add_argument("--name", required=True, help="who/what this grant is for, e.g. slackbot")
    gr.add_argument("--tools", help=f"comma-separated (default: read-only). all: {','.join(ALL_TOOLS)}")
    gr.add_argument("--expires", help="e.g. 90d, 12h (default: never)")
    gr.add_argument("--note", default="")
    gr.set_defaults(fn=lambda a: _print_grant(create_grant(
        a.slug, a.name, a.tools.split(",") if a.tools else None, a.expires, a.note)))

    gl = sub.add_parser("grants", help="list or revoke grants")
    gl.add_argument("--revoke", help="grant name to revoke")
    gl.set_defaults(fn=lambda a: (
        print("revoked" if revoke_grant(a.revoke) else "no such grant") if a.revoke
        else [print(f"{g['name']:<14} wiki={g['wiki']:<10} tools={','.join(g['tools'])}"
                    f"{'  expires=' + g['expires'] if g['expires'] else ''}")
              for g in list_grants()] and None))

    u = sub.add_parser("update", help="one incremental pass: caption → extract → wiki → render → sync")
    u.add_argument("slug", help="workspace under wikis/<slug>/ (chat ids from its manifest)")
    u.add_argument("--chats", help="comma-separated chat specs (defaults to the manifest's)")
    u.add_argument("--to", help="sync target repo (remembered after first use)")
    u.add_argument("--schedule", metavar="N{m,h,d}",
                   help="run automatically at this interval via launchd, e.g. 6h")
    u.add_argument("--unschedule", action="store_true", help="remove the launchd schedule")
    u.set_defaults(fn=cmd_update)

    sy = sub.add_parser("sync", help="deploy the rendered site into a git repo (renders first)")
    sy.add_argument("slug", help="workspace under wikis/<slug>/ (needs a built wiki)")
    sy.add_argument("--to", help="git repo path (remembered in wikis/<slug>/sync.json)")
    sy.add_argument("--no-render", action="store_true", help="sync the site as-is")
    sy.set_defaults(fn=lambda a: sync_site(Path("wikis") / a.slug, to=a.to,
                                           render=not a.no_render))

    co = sub.add_parser("correct", help="fold a maintainer correction into the wiki")
    co.add_argument("slug", help="workspace under wikis/<slug>/")
    co.add_argument("text", help="the correction in plain words (may cite [#id] messages)")
    _config_flags(co, ComposeConfig)
    co.set_defaults(fn=lambda a: add_correction(Path("wikis") / a.slug, a.text,
                                                _config_from(a, ComposeConfig)))

    r = sub.add_parser("render", help="render the wiki as a Wikipedia-style static site")
    r.add_argument("slug", help="workspace under wikis/<slug>/ (needs a built wiki)")
    r.add_argument("--out", help="output directory (default: wikis/<slug>/site)")
    r.set_defaults(fn=lambda a: render_site(Path("wikis") / a.slug, out=a.out))

    args = p.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
