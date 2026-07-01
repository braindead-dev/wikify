# How the Chat Wiki works

An accurate, code-level walkthrough of the pipeline in `wiki/agent/run.py`. It
describes exactly what each stage runs, what it sees, what it emits, and where the
output lands.

## First, about "tools"

**There are no LLM tools in the current design.** An earlier version gave the model
a tool-using agent loop (read/write/grep/search/identity tools). It was reverted:
DeepSeek V4 Flash over-investigated compulsively — it would fire 30–60 `search`/
`show` calls and never write. So each "agent" here is a single, tool-free
**JSON completion** (`LLMClient.complete_json(system, user)` → a parsed dict). All
retrieval, chunking, and file I/O is done by ordinary Python around those
completions. This is the "structured note-taking + sub-agents" pattern (durable
`limbo/` store + role-scoped completions), not autonomous tool use.

Every completion goes through one seam, `wiki/llm/client.py`:
`complete_json(system, user, effort)` — posts to OpenRouter (default model
`deepseek/deepseek-v4-flash`), forces `response_format={"type":"json_object"}`,
retries transient errors / malformed JSON with backoff, and returns the parsed
object. Swapping models is a config entry in `wiki/llm/config.py`.

## The pipeline (one function: `build_wiki`)

```
resolve_identities   (once)          -> identities.json
        │
windows(msgs, 600)   (deterministic) -> ~112 day-aligned slices
        │
SCOUTS   (parallel, one completion per window)   -> limbo/<window>.md
        │
PLAN     (one completion over all limbo material) -> the list of subjects
        │
CURATORS (parallel, one completion per subject)   -> kb/<type>/<slug>.md
        │
rebuild_index (deterministic)        -> kb/index.md
```

Everything for one wiki lives under `chats/<slug>/`:
`limbo/` (captured evidence), `kb/` (finished articles), `identities.json`,
`state.json` (progress).

---

## Stage 0 — `resolve_identities` (runs once, first build only)

- **What runs:** one completion.
- **Model / effort:** default model, default effort.
- **System prompt (inline):** "You resolve chat participant identities. Output JSON
  only. Only include a mapping when the chat clearly reveals it; skip anything
  uncertain."
- **Context it sees (user):** the **first 600 rendered transcript lines** (the
  intro, where people name each other), plus the instruction to map chat labels to
  real names and `Return JSON: {"renames": [["chat label", "Real Name"]]}`.
- **Output → where it goes:** the returned renames are filtered (never rename the
  owner label `"Me"`; never map two different labels to the same name — that would
  merge distinct people), then applied by shelling out to `imsg rename …`, which
  writes `chats/<slug>/identities.json`. The context then `reload()`s so every later
  stage renders the transcript with the resolved names.
- Deliberately conservative: it would rather leave a nickname unresolved than risk
  merging two people. Real names still surface inside the articles regardless.

## Stage 1 — Scouts (parallel, one per window)

- **What runs:** `scout(ctx, label, msgs)` — one completion per window, dispatched
  across a `ThreadPoolExecutor` (default 10 workers).
- **Model / effort:** default model, **`effort="medium"`** (capture doesn't need
  max reasoning; this is the throughput bottleneck, so it's tuned for speed).
- **System prompt:** `wiki/prompts/scout.md` — the archival framing (this is the
  owner's own chat; document faithfully, don't refuse or sanitize) + the scout role:
  capture cited observations about people, inside-joke *origins*, incidents, and
  group dynamics; note real names.
- **Context it sees (user):** **only its own window's transcript** — the ~600
  ID-tagged messages of that slice — plus `Return JSON: {"notes": "<markdown>"}`.
  A scout sees nothing else: not the KB, not other windows, not the roster. It is a
  pure, isolated extraction.
- **Output → where it goes:** `{"notes": "..."}` → written verbatim to
  `chats/<slug>/limbo/<window>.md`. These notes are cited (`[#id]`), grouped by
  `## subject`, and high-recall (capture liberally; the planner decides later).
- **Failure handling:** a scout that errors logs and returns "" — one bad window
  never sinks the run.

## Stage 2 — Plan (one completion)

- **What runs:** `plan(ctx)` — a single completion.
- **Model / effort:** default model, default (high) effort.
- **System prompt (inline):** "You plan a wiki from captured field notes about a
  group chat. JSON only."
- **Context it sees (user):** the **roster** (every participant name), plus **every
  topic/event/dynamic `##` section pulled from all limbo files** (sections whose
  header matches joke/slang/bit/incident/event/dynamic/running/meme/lore/saga/
  nickname/tension/obsession), concatenated across all ~112 windows and capped at
  400k chars. It does **not** see the per-person sections (those are for curators)
  and does **not** see the KB. The instruction tells it to always include every
  person, and to be *generous* with topics and events (a rich 18-month friendship
  should yield dozens), skipping only threads that appear once and never return.
- **Output → where it goes:** `{"subjects": [{"type": "person|topic|event", "id":
  "type/slug", "title": "..."}]}` — held in memory as the work-list for Stage 3.
  Subjects with an empty id/type are dropped. This is where limbo *graduates*:
  a recurring bit becomes a subject only once it's accumulated enough material.

## Stage 3 — Curators (parallel, one per subject)

- **What runs:** `curate(ctx, subject)` — one completion per subject, across the
  same `ThreadPoolExecutor` (10 workers). Different subjects write different files,
  so there are no write conflicts.
- **Model / effort:** default model, default (high) effort — this is the deep
  writing, so it gets the most reasoning.
- **System prompt:** `wiki/prompts/writer.md` — the archival framing + the writer
  role: an article is *understanding, not a timeline*; open with a lead on who the
  subject *is*; organize by theme not date; for a joke/topic trace its origin →
  evolution → meaning; cite every claim; Wikipedia voice.
- **Context it sees (user):** three things, assembled by `curate()`:
  1. the subject's identity (`type`, `title`, page `id`);
  2. **its relevant evidence** — `_gather(ctx, subject)` greps *all* limbo files and
     returns every `##` section that mentions the subject's keywords (their own
     section plus any joke/event/dynamic involving them), capped at 150k chars. This
     keeps each curator focused and bounded even when total limbo is ~900k chars;
  3. the **current article body** if the page already exists (so an update improves
     the whole, rather than starting over).
  A curator does **not** see the raw transcript or any other subject.
- **Output → where it goes:** `{"article": "<markdown body>"}`. Then, deterministically:
  `_strip_bad_cites` removes any `[#id]` that doesn't resolve to a real message;
  the body is wrapped in a `Page` (frontmatter: `id, type, title, sources` (derived
  from the cited ids), `updated`); and written to `chats/<slug>/kb/<id>.md`.
- **Failure handling:** a curator that errors or returns empty logs and skips —
  the rest still finish.

## Stage 4 — Index + state (deterministic, no LLM)

- `rebuild_index(ctx)` scans `kb/` and writes `kb/index.md`, a hub page linking
  every article grouped by People / Topics / Events.
- `state.json` records `chat_ids`, `title`, `model`, `identities_resolved`, the list
  of `scouted` window labels, and `curated_once`.

---

## Citation integrity

`[#id]` is a real message ROWID. It's carried from scout notes into articles.
`_strip_bad_cites` drops any id that doesn't resolve before a page is written, and
`wiki verify` re-checks every citation and cross-link against the source DB. So an
unresolvable citation never lands, and `wiki show <chat> <page>`'s ids round-trip
to `imsg show <id>`.

## Parallelism

Both fan-out stages (scouts over windows, curators over subjects) run on a shared
`ThreadPoolExecutor` (default 10). Each unit is an independent completion, so
wall-clock is roughly *slowest single unit*, not the sum. The plan and index steps
are single-threaded between them.

## Initialization vs. updates — the SAME process

There is one entry point, `build_wiki`, and one code path. The difference is purely
which work is new:

| | First build (initialization) | Update (re-run) |
|---|---|---|
| identity resolution | runs once | skipped (flag in state) |
| scouts | every window | **only windows not yet scouted** (watermark) |
| plan | over all limbo | over all limbo |
| curators | every subject | **only subjects whose *new* limbo mentions them** |
| nothing new? | n/a | prints "up to date", does nothing |

So an update is just `build_wiki` again: it scouts only the new time-slices, and
re-curates only the subjects those new slices actually touch — a small update maps
to small work, not a full rewrite, and re-running when nothing changed is a no-op.
`python3 -m wiki build <slug>` is both "build it" and "catch it up".
