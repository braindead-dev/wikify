"""Command line interface for the imessage package.

    python3 -m imessage                       # interactive: pick chat(s), export
    python3 -m imessage chats [--match TEXT]  # list chat rows (faithful)
    python3 -m imessage people                # list handles (faithful)
    python3 -m imessage export 512 638        # merge these chats -> data/
    python3 -m imessage export --group "Label"
    python3 -m imessage update                # refresh every export, show new counts
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

from .db import MessagesDB

STATE = Path("data") / ".state.json"


def _open(args) -> MessagesDB:
    identities = args.identities or ("identities.json" if Path("identities.json").exists() else None)
    try:
        return MessagesDB(path=args.db, contacts=not args.no_contacts, identities=identities)
    except (FileNotFoundError, PermissionError) as exc:
        sys.exit(f"  {exc}")


def _slug(text: str) -> str:
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", text.lower())).strip("-") or "chat"


def _date(d) -> str:
    return d.strftime("%Y-%m-%d") if d else "?"


def _state() -> dict:
    return json.loads(STATE.read_text()) if STATE.exists() else {}


def _save_state(state: dict):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, indent=2))


def _print_chats(chats):
    print(f"\n  {'id':>5}  {'msgs':>7}  {'kind':<5}  {'span':<23}  name")
    print("  " + "-" * 72)
    for c in chats:
        span = f"{_date(c.first)} -> {_date(c.last)}"
        print(f"  {c.rowid:>5}  {c.message_count:>7}  {c.kind:<5}  {span:<23}  {c.title[:40]}")
    print()


def _export(db, chat_ids, fmt, out=None, title=None):
    """Render to disk, record an exact watermark, and report the diff vs last write."""
    chat_ids = [chat_ids] if isinstance(chat_ids, int) else list(chat_ids)
    payload, meta, title = db.render(chat_ids, fmt, title)
    text = json.dumps(payload, indent=2, ensure_ascii=False) if fmt == "json" else payload
    out = Path(out) if out else Path("data") / f"{_slug(title)}.{fmt}"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text)

    state = _state()
    prev = state.get(str(out), {})
    watermark = db.max_message_id(chat_ids)
    state[str(out)] = {"chats": chat_ids, "format": fmt, "title": title,
                       "messages": meta["message_count"], "watermark": watermark}
    _save_state(state)

    _report(db, chat_ids, title, out, meta, prev)


def _report(db, chat_ids, title, out, meta, prev):
    total = meta["message_count"]
    size = f"{out.stat().st_size / 1024 / 1024:.1f} MB"
    if "messages" not in prev:
        print(f"\n  ✓ {title}  (indexed {total} messages)")
    elif "watermark" not in prev:
        print(f"\n  ✓ {title}  ({total} messages — baseline set)")
    else:
        new = [m for m in db.messages(chat_ids, after_id=prev["watermark"]) if not m.system]
        print(f"\n  ✓ {title}  {len(new):+d} new  ({prev['messages']} → {total})")
        if new:
            span = f"{new[0].ts:%m-%d %H:%M} → {new[-1].ts:%m-%d %H:%M}"
            senders = ", ".join(f"{s} ({n})" for s, n in Counter(m.sender for m in new).most_common())
            last = new[-1].text[:50] or ("[" + ", ".join(new[-1].attachments) + "]")
            print(f"     span: {span}")
            print(f"     from: {senders}")
            print(f"     last: {new[-1].sender}: {last}")
    print(f"  → {out}  ({size})\n")


# -- commands --------------------------------------------------------------
def cmd_chats(db, args):
    chats = db.chats()
    if args.match:
        needle = args.match.lower()
        chats = [c for c in chats if needle in c.title.lower()
                 or any(needle in n.lower() for n in c.participant_names)]
    if not args.all:
        chats = chats[:args.limit]
    if args.json:
        print(json.dumps([{"id": c.rowid, "title": c.title, "kind": c.kind,
                           "service": c.service, "messages": c.message_count,
                           "participants": c.participant_names,
                           "first": _date(c.first), "last": _date(c.last)}
                          for c in chats], indent=2))
    else:
        _print_chats(chats)


def cmd_people(db, args):
    people = db.handles()
    if not args.all:
        people = people[:args.limit]
    if args.json:
        print(json.dumps([{"id": h.rowid, "handle": h.value, "name": h.name,
                           "messages": h.message_count, "chats": h.chat_ids}
                          for h in people], indent=2))
        return
    print(f"\n  {'msgs':>7}  {'name':<22}  handle")
    print("  " + "-" * 60)
    for h in people:
        print(f"  {h.message_count:>7}  {(h.name if h.name != h.value else '—')[:22]:<22}  {h.value}")
    print()


def cmd_export(db, args):
    if args.group:
        _export(db, db.group(args.group), args.format, args.out, args.title or args.group)
    elif args.chats:
        _export(db, args.chats, args.format, args.out, args.title)
    else:
        sys.exit("  give chat ids (e.g. `export 512 638`) or --group NAME")


def cmd_update(db, args):
    state = _state()
    targets = args.files or list(state)
    if not targets:
        sys.exit("  nothing to update — run `export` first.")
    for path in targets:
        entry = state.get(path)
        if not entry:
            print(f"  ? {path}: not a known export, skipping")
            continue
        _export(db, entry["chats"], entry["format"], path, entry["title"])


def cmd_pick(db, args):
    chats = db.chats()
    _print_chats(chats[:args.limit])
    valid = {c.rowid for c in chats}
    try:
        ids = [int(x) for x in re.split(r"[,\s]+", input("  chat id(s), comma to merge: ").strip()) if x]
        fmt = "json" if input("  format [txt/json] (txt): ").strip().lower() == "json" else "txt"
    except (EOFError, KeyboardInterrupt, ValueError):
        sys.exit("\n  cancelled.")
    if not ids or any(i not in valid for i in ids):
        sys.exit("  invalid selection.")
    _export(db, ids, fmt, args.out)


# -- wiring ----------------------------------------------------------------
def main(argv=None):
    parser = argparse.ArgumentParser(prog="imessage", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", help="path to chat.db (default: ~/Library/Messages/chat.db)")
    parser.add_argument("--identities", help="path to identities.json")
    parser.add_argument("--no-contacts", action="store_true", help="don't resolve names from Contacts")
    sub = parser.add_subparsers(dest="cmd")

    p = sub.add_parser("chats", help="list chat rows (faithful, no merging)")
    p.add_argument("--match", help="filter by title/participant substring")
    p.add_argument("--limit", type=int, default=25)
    p.add_argument("--all", action="store_true", help="show every chat")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_chats)

    p = sub.add_parser("people", help="list handles (faithful, no merging)")
    p.add_argument("--limit", type=int, default=40)
    p.add_argument("--all", action="store_true")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_people)

    p = sub.add_parser("export", help="export chat(s) to data/")
    p.add_argument("chats", nargs="*", type=int, help="chat rowid(s) to merge")
    p.add_argument("--group", help="named group from identities.json")
    p.add_argument("--format", choices=("txt", "json"), default="txt")
    p.add_argument("--out", help="output path (default: data/<slug>.<fmt>)")
    p.add_argument("--title", help="override the conversation title")
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("update", help="refresh previous exports to current state")
    p.add_argument("files", nargs="*", help="specific export paths (default: all)")
    p.set_defaults(func=cmd_update)

    p = sub.add_parser("pick", help="interactive picker (default command)")
    p.add_argument("--limit", type=int, default=25)
    p.add_argument("--out")
    p.set_defaults(func=cmd_pick)

    args = parser.parse_args(argv)
    db = _open(args)
    try:
        if args.cmd is None:
            args.limit, args.out = 25, None
            cmd_pick(db, args)
        else:
            args.func(db, args)
    finally:
        db.close()


if __name__ == "__main__":
    main()
