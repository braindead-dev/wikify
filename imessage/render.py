"""Render messages to a compact transcript (txt) or structured data (json)."""
from __future__ import annotations


def _reactions(reactions: dict) -> str:
    groups = sorted(reactions.items(), key=lambda kv: -len(kv[1]))
    return "{" + "; ".join(f"{label}: {', '.join(names)}" for label, names in groups) + "}"


def as_txt(title: str, meta: dict, messages: list) -> str:
    """Day-grouped transcript. Timestamps stay cheap; reactions fold onto targets."""
    header = (
        f"# {title} — iMessage transcript\n"
        f"# Participants: {', '.join(meta['participants'])}\n"
        f"# {meta['message_count']} messages | "
        f"{meta['first_message']} -> {meta['last_message']}\n"
        "# FORMAT: '== YYYY-MM-DD ==' day headers; 'HH:MM Name: message'.\n"
        "#   Reactions grouped by type, e.g. {Loved: Alice, Bob; Laughed: Cara}.\n"
        "#   Reply quotes parent: (re \"snippet…\"). Media: [img] [video] [gif] [audio].\n"
        "#   '(edited)' = edited.  '* ...' = system event (rename/add/remove/leave)."
    )
    lines = [header]
    day = None
    for m in messages:
        d = m.ts.strftime("%Y-%m-%d")
        if d != day:
            lines.append(f"\n== {d} ==")
            day = d
        hm = m.ts.strftime("%H:%M")
        if m.system:
            lines.append(f"{hm} * {m.sender} {m.system}")
            continue
        media = "".join(f"[{t}] " for t in m.attachments)
        reply = f'(re "{m.reply_to}…") ' if m.reply_to else ""
        body = m.text.replace("\n", " ⏎ ")
        line = f"{hm} {m.sender}: {media}{reply}{body}".rstrip()
        if m.edited:
            line += " (edited)"
        if m.reactions:
            line += "  " + _reactions(m.reactions)
        lines.append(line)
    return "\n".join(lines) + "\n"


def as_json(title: str, meta: dict, messages: list) -> dict:
    """One object per message, with only the fields that apply present."""
    items = []
    for m in messages:
        obj = {"t": m.ts.strftime("%Y-%m-%d %H:%M:%S"), "from": m.sender}
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
