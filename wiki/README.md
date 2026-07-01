# wiki — a cited knowledge base over a chat

Reads a conversation (via the `imessage` tool) and builds a living, Wikipedia-style
wiki about it: typed pages for people, events, and topics; a derived timeline; and
**every claim cited to a specific message**. Continuously updatable — re-running
folds in new messages. See [`docs/wiki-agent-design.md`](../docs/wiki-agent-design.md)
for the principles.

## Setup

```bash
pip install -e '.[wiki]'          # or: pip install openai python-dotenv
echo 'OPENROUTER_API_KEY=sk-or-...' >> .env
```

The default model is DeepSeek V4 Flash via OpenRouter (cheap, 1M context). Swap it
with `--model` (keys in `wiki/llm/config.py`); a new model is a config entry.

## Use

```bash
# build a wiki from source chats (first run needs a selector + title)
python3 -m wiki build --chats 12,15,18 --title "Book Club"
python3 -m wiki build --match "book club"        # or find chats by name
python3 -m wiki build --group "Book Club"        # or an identities.json group

# later: fold in new messages (delta) — same command, just the slug
python3 -m wiki build book-club
python3 -m wiki build book-club --chunks 3       # cap this run

# read it
python3 -m wiki list
python3 -m wiki status  book-club
python3 -m wiki pages   book-club [--type person]
python3 -m wiki show    book-club person/alice
python3 -m wiki timeline book-club [--limit 40] [--page person/alice]

# quality
python3 -m wiki verify  book-club                # citation + link integrity
python3 -m wiki eval    book-club --sample 25    # + judged grounding
python3 -m wiki consolidate book-club            # refactor grown pages
```

Everything about a wiki lives in `chats/<slug>/` (git-ignored — real people):
`kb/` the pages, `state.json` the resumable ingest watermark, `identities.json`
per-chat name merges.

## How it works (the short version)

`build` folds a **reducer** over day-aligned chunks. For each chunk the model
proposes typed **edit ops** carrying `[#id]` citations; a deterministic applier
validates the whole batch (every citation must resolve) and commits atomically —
so nothing half-lands and no uncited claim is ever written. Pages are markdown;
the timeline and backlinks are **derived**, never maintained, so they can't drift.

Two modes: **incremental** (per chunk, append/update) and **consolidate**
(periodic refactor of grown pages — reverts if it would drop a citation).

## Architecture

```
wiki/
  store/    L2  pages, the single write path, derived views     (no LLM)
  llm/      L4  the model seam — one swappable backend
  reduce/   L3  the reducer + runner (the fold) + consolidator
  prompts/      versioned prompts (own your prompts)
  eval/         mechanical integrity + judged grounding
```

Strict one-way dependencies; `imessage` (L1) is consumed here, never the reverse.
