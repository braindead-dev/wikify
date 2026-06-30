"""Render messages to a compact transcript (txt) or structured data (json).

`format_message` is the single source of truth for how one message becomes a
line; both the transcript and `imsg show` go through it.
"""
from __future__ import annotations


def _reactions(reactions: dict) -> str:
    groups = sorted(reactions.items(), key=lambda kv: -len(kv[1]))
    return "{" + "; ".join(f"{label}: {', '.join(names)}" for label, names in groups) + "}"


def format_message(m, *, ids: bool = False, with_date: bool = False) -> str:
    """One message as a transcript line. `ids` appends the citation #rowid;
    `with_date` includes the date (used outside day-grouped context)."""
    stamp = m.ts.strftime("%Y-%m-%d %H:%M" if with_date else "%H:%M")
    if m.system:
        return f"{stamp} * {m.sender} {m.system}"
    tag = f" #{m.rowid}" if ids else ""
    media = "".join(f"[{t}] " for t in m.attachments)
    reply = f'(re "{m.reply_to}…") ' if m.reply_to else ""
    line = f"{stamp} {m.sender}{tag}: {media}{reply}{m.text}".replace("\n", " ⏎ ").rstrip()
    if m.edited:
        line += " (edited)"
    if m.reactions:
        line += "  " + _reactions(m.reactions)
    return line


def _header(title: str, meta: dict, ids: bool) -> str:
    cite = "#NNNNN after a name is the stable message id — cite as [#NNNNN].\n#   " if ids else ""
    return (
        f"# {title} — iMessage transcript\n"
        f"# Participants: {', '.join(meta['participants'])}\n"
        f"# {meta['message_count']} messages | "
        f"{meta['first_message']} -> {meta['last_message']}\n"
        f"# FORMAT: '== YYYY-MM-DD ==' day headers; 'HH:MM Name: message'.\n"
        f"#   {cite}Reactions grouped by type, e.g. {{Loved: Alice, Bob; Laughed: Cara}}.\n"
        "#   Reply quotes parent: (re \"snippet…\"). Media: [img] [video] [gif] [audio].\n"
        "#   '(edited)' = edited.  '* ...' = system event (rename/add/remove/leave)."
    )


def as_txt(title: str, meta: dict, messages: list, ids: bool = False, header: bool = False) -> str:
    """Day-grouped transcript. Pure data by default; pass header=True for a
    self-describing standalone file. Timestamps stay cheap; reactions fold in."""
    lines = []
    day = None
    for m in messages:
        d = m.ts.strftime("%Y-%m-%d")
        if d != day:
            lines.append(f"== {d} ==" if day is None else f"\n== {d} ==")
            day = d
        lines.append(format_message(m, ids=ids))
    body = "\n".join(lines)
    return f"{_header(title, meta, ids)}\n\n{body}\n" if header else f"{body}\n"


def as_json(title: str, meta: dict, messages: list) -> dict:
    """One object per message. `id` (the rowid) is always present as the key."""
    items = []
    for m in messages:
        obj = {"id": m.rowid, "t": m.ts.strftime("%Y-%m-%d %H:%M:%S"), "from": m.sender}
        if m.system:
            obj["system"] = m.system
        else:
            obj["text"] = m.text
            if m.attachments:
                obj["attachments"] = m.attachments
            if m.reply_to:
                obj["reply_to"] = m.reply_to
            if m.edited:
                obj["edited"] = True
            if m.reactions:
                obj["reactions"] = m.reactions
        items.append(obj)
    return {"conversation": title, **meta, "messages": items}
