# imessage analysis

Two tools over your local iMessage history:

- **`imessage`** — read and export your chats faithfully, as a **CLI** or **SDK**
  (this README). Stdlib only — nothing to install.
- **`wiki`** — build a cited, Wikipedia-style knowledge base over a conversation
  (people, events, topics, a derived timeline), every claim traced to a message.
  See [`wiki/README.md`](wiki/README.md) and [`docs/wiki-agent-design.md`](docs/wiki-agent-design.md).

## imessage

Read and export your local iMessage history — as a **CLI** or an importable
**SDK**. It shows the real entities faithfully (every chat row, every handle)
and lets you merge them *explicitly* when you want. Nothing is auto-grouped.

## Quick start

```bash
# list your chats (faithful — one row per chat, no merging)
python3 -m imessage chats

# list the people/handles in your messages
python3 -m imessage people

# export one chat, or merge several by id, into data/
python3 -m imessage export 512 638 --format txt

# later, pull new messages into every export you've made
python3 -m imessage update

# or just run it and pick interactively
python3 -m imessage
```

Optionally `pip install -e .` to get an `imsg` command instead of
`python3 -m imessage`.

## Why it's built this way

The local database splits a single human "conversation" across multiple rows —
a group re-created under a new id, or an iMessage thread with an SMS fallback
copy. Rather than guess which rows belong together, this tool **shows them as
they are** and gives you simple ways to combine them:

- **Merge chats:** pass several ids to `export` (or `messages` in the SDK).
- **Merge handles into one person:** list them under a name in `identities.json`.
- **Name a group of chats:** define it under `groups` in `identities.json`,
  then `export --group "Label"`.

Names are resolved from your macOS Contacts for readability, but two handles are
never treated as the same person unless you say so.

## CLI

| command | what it does |
| --- | --- |
| `chats [--match TEXT] [--limit N] [--all] [--json]` | list chat rows |
| `people [--limit N] [--all] [--json]` | list handles with message counts |
| `export <ids...> \| --group NAME [--format txt\|json] [--ids] [--header] [--out PATH]` | export to `data/` |
| `update [paths...]` | re-render past exports; reports an exact diff (`+N new`, span, senders, last message) |
| `show <rowid> [--context N]` | resolve a `#rowid` citation back to the message + surrounding context |
| `pick` (default) | interactive picker; comma-separate ids to merge |
| `alias <name> <handle...>` | merge handles under one person |
| `rename <old> <new>` | rename a person, a contact name, or yourself |
| `group <label> <chatid...>` | name a set of chats (use it via `export --group`) |
| `identities` | show current merges, aliases, and groups |

`--ids` prefixes each line with its stable `#rowid` at line-start (the message's
permanent key — works for system events too) so agents can cite unambiguously as
`[#rowid]`; resolve any citation with `show`. `--header` prepends a
self-describing format/meta block — **off by default**, since the data file is
meant to be chunked/prefixed by your own pipeline, not pasted whole. Both flags
are remembered, so `update` reproduces them.

The `alias` / `rename` / `group` verbs just read-modify-write `identities.json`
for you, so merging and renaming are first-class CLI actions — no hand-editing:

```bash
imsg people                                     # find the handles/ids you need
imsg alias "Alice" +15551230001 +15551230002    # merge handles → one person
imsg rename "Bob Example" "Bob"                  # rename a person (or `rename Me Alex`)
imsg group "My Group" 101 102 103               # name a set of chats
imsg export --group "My Group" --ids            # use the group
```

Global: `--db PATH`, `--identities PATH`, `--no-contacts`.

## SDK

```python
from imessage import MessagesDB

with MessagesDB() as db:
    for c in db.chats():                 # faithful list of chat rows
        print(c.rowid, c.title, c.message_count)

    msgs = db.messages([512, 638])        # merge chats explicitly, by id
    new = db.messages(638, since=cutoff)  # only messages after a datetime
    wm = db.max_message_id(638)           # exact watermark (a ROWID)
    delta = db.messages(638, after_id=wm) # only messages newer than a ROWID
    cite = db.message(83412, context=2)   # resolve a citation + surrounding msgs
    text = db.export([512, 638], "txt", ids=True)   # transcript with #rowid tags
    data = db.export([512, 638], "json")            # structured dict (id always present)
```

`MessagesDB(path=None, contacts=True, identities=None)` — `identities` accepts a
path or a dict. `Chat`, `Handle`, and `Message` are plain dataclasses.

## identities.json (optional)

Build it with the `alias` / `rename` / `group` commands above, or copy
`identities.example.json` to `identities.json` and edit by hand. It's git-ignored.

```json
{
  "me": "Me",
  "people": { "Alice": ["+15551230001", "alice@example.com"] },
  "groups": { "My Group": [101, 102, 103] }
}
```

## txt format

```
== 2025-06-23 ==
22:00 Me: hey
22:13 Alice: wyd  {Loved: Bob, Cara; Laughed: Dee}
22:42 Bob: (re "movie tonight…") down
17:06 Cara: [img] look at this
```

Day headers keep timestamps cheap; reactions fold onto their target message. With
`--ids`, every line (events included) is prefixed with its stable id for citing:

```
#83410 22:13 Alice: wyd  {Loved: Bob}
#83411 22:14 * Alice named the group "trip"
```

## Requirements

- macOS, Python 3.9+ (stdlib only).
- Your terminal needs **Full Disk Access** to read `~/Library/Messages`
  (System Settings → Privacy & Security → Full Disk Access).

Everything runs locally; nothing leaves your machine.
