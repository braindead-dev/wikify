# wiki — a cited knowledge base over a chat

Reads a conversation (via the `imessage` tool) and writes a living, Wikipedia-style
wiki about it: deep biographies of each person, articles on the group's inside
jokes and running bits (with their origins and how they evolved), and pages for
real events — **every claim cited to a specific message**.

It's a **writer agent**, not a mechanical pipeline. See
[`docs/wiki-agent-design.md`](../docs/wiki-agent-design.md) for the principles.

## Setup

```bash
pip install -e '.[wiki]'          # openai + python-dotenv
echo 'OPENROUTER_API_KEY=sk-or-...' >> .env
```

Default model is DeepSeek V4 Flash via OpenRouter (cheap, 1M context). Swap it with
`--model` (keys in `wiki/llm/config.py`); a new model is a config entry.

## Use

```bash
# build a wiki from source chats (first run needs a selector + title)
python3 -m wiki build --match "book club"
python3 -m wiki build --chats 12,15,18 --title "Book Club"

# later: fold in new messages (delta) — same command, just the slug
python3 -m wiki build book-club
python3 -m wiki build book-club --limit 5     # only a few windows this run

# read it
python3 -m wiki list
python3 -m wiki status book-club
python3 -m wiki pages  book-club [--type person]
python3 -m wiki show   book-club person/alice

# quality
python3 -m wiki verify book-club              # citation + link integrity
python3 -m wiki eval   book-club --sample 25  # + judged grounding
```

Everything about a wiki lives in `chats/<slug>/` (git-ignored — real people):
`kb/` the articles, `limbo/` the captured evidence, `identities.json` the resolved
names, `state.json` the resumable progress.

## How it works

A **map → reduce over a durable limbo store**, fully parallelized:

1. **Scouts** (one per window, in parallel) read the chat and dump *cited evidence*
   into `limbo/` — people's traits, inside-joke origins, incidents. High-recall
   capture, no judgment.
2. A **planner** decides which subjects have *matured* enough (recurred, accumulated
   material) to deserve an article; the rest stay in limbo until they do.
3. **Curators** (one per subject, in parallel) synthesize all of a subject's limbo
   evidence into a deep article — a biography of who someone *is*, or a joke's full
   arc from origin to evolution. Not a timeline.

Re-running is cheap and non-over-indexing: only new windows are scouted, and on an
update only subjects with genuinely new evidence are re-written. Identity is
resolved up front (the literal "Me" and nicknames → real names) via the CLI.

Both agent passes are reliable completions (no tool over-investigation); citations
are verified on write, so an unresolvable id never lands.

## Architecture

```
wiki/
  agent/    scouts + planner + curators (the writer agent) — wiki/agent/run.py
  store/    the page model + derived views (backlinks, timeline, integrity)
  llm/      the model provider seam — swap models via config
  prompts/  scout.md, writer.md (own your prompts)
  eval/     mechanical integrity + judged grounding
```

Strict one-way dependency on `imessage` (L1). Needs Full Disk Access for
`~/Library/Messages`. Everything runs locally.
