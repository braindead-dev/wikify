# Chat Wiki — Design

A continuously-updated, deeply-cited knowledge base built over an iMessage
conversation: typed pages (people, events, topics), a derived timeline, and
wiki-style cross-links — every claim traceable to a specific message.

This document is the spine: the principles and the clean architecture. It is
deliberately not a spec — it fixes the *shape* and the *invariants*, and leaves
implementation latitude inside them.

---

## 1. Core principles

These are load-bearing. Everything below follows from them; if a future change
violates one, the change is wrong, not the principle.

**P1 — The KB is a derivation of the transcript, never a parallel truth.**
The messages are the only source of truth. The wiki is a cache of *understanding*
over them: always rebuildable from the transcript, never authoritative on its own.
If the KB and the messages disagree, the messages win.

**P2 — One narrow write path.**
Every mutation flows through a single typed, deterministic edit-applier. The model
*proposes* edits; code *applies* them. Nothing touches a page any other way. This
buys provenance, atomicity, rollback, a free changelog, and a hard boundary
between model nondeterminism (proposing) and system determinism (applying).

**P3 — Cite or it didn't happen.**
No claim enters the KB without at least one resolvable `[#message_id]`. Citations
are mechanically verifiable against the source DB. This is both the correctness
contract and the evaluation ground truth — a signal most agent systems never get.

**P4 — Derive everything computable; maintain only the irreducible.**
The timeline, backlinks, entity index, and stats are *views* over pages +
citations — regenerated, never hand-edited. The only maintained artifacts are the
pages. One source of truth means no drift.

**P5 — Own the loop, the prompts, and the context; rent the model.**
Control flow is our code. Prompts and model choice are versioned config. The
context window is explicitly assembled, never left to a framework. The model sits
behind one `complete()` seam and is swappable without touching the system.

**P6 — A workflow, not an autonomous agent.**
The pipeline is fixed; the LLM decides *content*, not *control flow*. Determinism
where it's cheap, intelligence only where it's needed. This is what makes the
system testable, cheap, and debuggable.

**P7 — Edits scale with information, not invocations.**
Chunk by content, gate by significance, stay idempotent. Ten small syncs and one
large sync over the same messages must converge to the same wiki. Cadence of
updates never changes the result — only the information does.

**P8 — Per-chat isolation, shared machinery.**
One codebase, N self-contained knowledge bases. No chat shares mutable state with
another. Adding a chat is creating a directory, not editing the system.

---

## 2. Architecture: layers and dependency direction

Strict one-way dependencies. Each layer knows only the layers below it. This is
the entire maintainability story — you can replace any layer without disturbing
the ones above.

```
L0  Source data        the iMessage DB (read-only, external)
      ▲
L1  Data access        the `imessage` CLI/SDK  — faithful primitives, citations,
      ▲                chunked export, identity resolution. Never depends upward.
L2  KB store           per-chat pages + indexes + the edit-applier. Knows nothing
      ▲                about LLMs. Enforces the store's invariants.
L3  Reducer            the workflow: extract → resolve → plan → emit → apply →
      ▲                evaluate. Orchestrates L1 (read) and L2 (write) via L4.
L4  Model provider     the thin `complete()` seam → OpenRouter → any model.

  cross-cutting:  prompts (versioned)   ·   eval (golden sets + metrics)
                  config (per-model, per-chat)
```

- **L1 is the tool we already have.** Its `--ids` export, `show`, `export
  --after/--before`, and the `alias`/`rename`/`people` identity verbs are exactly
  the agent's read + citation + entity-resolution surface. The agent consumes L1;
  L1 never learns the agent exists.
- **L2 is pure and LLM-free.** It can be tested, rebuilt, and reasoned about with
  zero model calls. It owns the page schema and the indexes and is the *only*
  thing that writes to disk.
- **L3 is the only layer that "thinks."** It is a plain loop, not an agent runtime.
- **L4 is one function.** Swapping models is a config entry, never a code change.

---

## 3. Data model (the irreducible artifacts)

Only two things are truly maintained: **pages** and the **entity registry**.
Everything else is derived.

### Pages

Typed markdown with frontmatter. Types are a small open set (`person`, `event`,
`topic`, `place`, …) — extensible, not enumerated forever.

```yaml
---
id: person/alice           # stable, slug-based, never reused
type: person
title: Alice
aliases: [ali]
pinned: true               # people + index are pinned; the rest are agent-made
sources: [1024, 1031]      # every message id this page cites (derived from body,
updated: 2026-06-30        #   mirrored here for fast integrity checks)
---
Body prose. Every claim carries an inline [#1024] citation and links to other
pages via [[event/road-trip]].
```

Pinned pages always exist (one `person/*` per participant + a home `index`).
Everything else the agent creates, splits, merges, or retires.

### Entity registry

The one piece of maintained state that isn't a page: canonical entity → page +
its handles/aliases. This *is* an extension of L1's identity system — when the
agent merges two people, it calls the `alias`/`rename` verbs, and the registry
and `identities.json` stay one thing, not two.

### Derived indexes (never hand-edited, always rebuildable)

- **Backlinks:** page → pages that link to it. Powers "edit everything relevant."
- **Timeline:** every `[#id]` resolves to a message with a timestamp (via L1), so
  the timeline is a *query* over the citation graph, filterable by person / type /
  significance. There is no timeline *page* to keep in sync — it cannot drift.
- **Stats / coverage:** counts, gaps, uncited spans — all queries.

### The edit envelope (the narrow write path)

The model emits a small, typed set of operations; L2 applies them atomically.
The shape (extensible, not final):

`create_page` · `add_claim{page, text, citations[]}` · `revise_section{page,
anchor, text, citations[]}` · `link{from, to}` · `merge_entity{from, into}` ·
`retire_page{id, redirect?}`

Structural moves (create/merge/link/retire) are typed ops. Prose edits within a
page use a fuzzy-context diff (codex's `apply_patch` + `seek_sequence` cascade)
so model-authored patches land despite whitespace/typography drift. Hybrid by
design: typed where structure matters, diff where prose flows.

---

## 4. The reducer (L3)

The entire system is one function, applied repeatedly:

```
new_KB = reduce(KB, chunk)
```

**Initialization is not special — it is `reduce` folded over the backfill.** A
live update is `reduce` over the newest chunk. Same function, same code path. This
is why there is no separate "initializer" and "updater."

Per chunk, a fixed pipeline (a workflow, P6):

1. **Extract** — from the ID-tagged chunk, pull candidate facts/events/entities,
   each with `[#id]` citations. *(The one place the RLM pattern applies: keep the
   orchestrator's context tiny, hold raw text outside the window, fan parallel
   sub-calls over ID-tagged slices, stitch with code — never lossy-summarize.
   Add a second recursion level only if a single chunk is itself too large.)*
2. **Resolve & route** — map entities to existing pages via the registry; decide
   the affected set (the target page *and*, via backlinks, everything linking to it).
3. **Plan** — pull only the affected pages; decide create vs edit vs refactor.
4. **Emit** — structured, cited edit ops.
5. **Apply** — L2, atomic, diff-tracked.
6. **Evaluate** — validate: every citation resolves (mechanical), no
   contradictions, no dangling links. Failure → compact error → one retry.

### Two modes, not two agents

- **Incremental** (per chunk): append/update/cite. Cheap, frequent, `reasoning:
  high`.
- **Consolidation** (periodic, or when a page outgrows a threshold): merge
  duplicates, split overgrown pages, refactor for coherence, backfill links. A
  different job with a different prompt, `reasoning: xhigh`. Run on a cadence, not
  per message.

That is the only justified split — *incremental vs. consolidation*, not init vs.
update.

### Why edits track information, not calls (P7)

- **Content-sized chunks**, not "update events." A big backfill is many chunks and
  proportionally many edits; a trickle of new messages buffers until it crosses a
  content threshold.
- **Significance gating** at extraction: only above-threshold claims create pages
  or edits. Small updates can't over-index on trivia; large ones can't under-index.
- **Idempotency**: re-processing already-captured information is a no-op. Frequency
  cannot inflate the KB.

### The reducer's own context (compaction)

The reducer holds working context across a chunk. Manage it the way codex does:
truncate tool/read outputs on ingest, and when nearing ~90% of the window,
summarize-and-replace — re-pin the stable style guide + the last few raw
messages, collapse the rest into a handoff summary. Raw transcript always lives on
disk (L1); the model only ever sees a windowed view.

---

## 5. Model & provider (L4)

- **One seam:** `complete(messages, tools) → edits`, pointed at OpenRouter's
  OpenAI-compatible endpoint. Swapping models = changing one string
  (`deepseek/deepseek-v4-flash` → whatever ships next).
- **Model-agnostic by construction.** No vendor SDK leaks above L4. Per-model
  config (reasoning effort, cache headers) lives in a config map, so a new model is
  a config entry, not a code change.
- **Default model:** DeepSeek V4 Flash — 1M context, ~$0.10/$0.20 per Mtok in/out,
  60–80% cheaper with prompt caching on the repeated style guide + KB. A full
  multi-million-token backfill costs single-digit dollars. Cost is never the
  constraint; context assembly and edit consistency are.
- **Not the Claude Agent SDK** (hard-locked to Anthropic models). **Not a codex
  port** (Responses-API-only; can't reach OpenRouter, and it's a coding CLI whose
  value doesn't map here). Borrow codex's *patterns* (loop shape, single-write-path,
  context-fragments-as-history, compaction, backoff/retry), not its code.

---

## 6. Multi-chat & folder organization

**Code is shared and singular; data is per-chat and self-contained.** This is P8
made concrete. One repo, two packages with a strict one-way dependency, and a
`chats/` tree where each conversation is an isolated, portable unit.

```
imessage-analysis/
├── imessage/                 # L1 — data-access CLI/SDK (exists today)
├── wiki/                     # L2–L4 — the agent system
│   ├── store/                #   L2: page schema, indexes, the edit-applier
│   ├── reduce/               #   L3: the reducer pipeline + the two modes
│   ├── llm/                  #   L4: the complete() seam + model config
│   ├── prompts/              #   versioned prompt templates (own your prompts)
│   └── eval/                 #   golden sets + metrics + runner
├── chats/                    # PER-CHAT DATA — one self-contained unit each
│   └── <chat-slug>/
│       ├── transcript/       #   L1 exports (--ids), the ingest source of record
│       ├── kb/               #   the wiki: people/  events/  topics/  index.md
│       ├── index/            #   derived: backlinks, entity map, timeline cache
│       ├── identities.json   #   per-chat identity merges (shared with L1)
│       └── state.json        #   ingestion watermark + progress (resume anywhere)
├── docs/
│   └── wiki-agent-design.md  # this document
├── pyproject.toml
└── README.md
```

- **A chat is a directory.** Everything specific to a conversation lives under
  `chats/<slug>/`; nothing is shared between chats. Copy the directory and the KB
  moves with it. Delete it and the chat is gone cleanly.
- **`state.json` makes ingestion resumable** — a stateless-reducer fold checkpointed
  after each chunk. Interrupt and resume anywhere; re-run is idempotent.
- **`index/` is disposable** — always rebuildable from `kb/` + L1. Never a source
  of truth; safe to delete and regenerate.
- **Tracked vs. ignored:** code is versioned; `chats/` is git-ignored (it holds
  real people's data — same rule as the transcripts). Version a specific chat's
  `kb/` only in its own private repo if you want its edit history.

**One repo vs. two.** Keep both packages in one repo for now — the dependency is
one-way and premature splitting is a single-use abstraction. If `imessage` ever
ships as a standalone library, split then; nothing above depends on that choice.

---

## 7. Maintainability — the properties that fall out

Not aspirations; consequences of the principles.

- **Replayability.** The KB is a pure fold over the transcript (P1, P7). Any bug
  fix or prompt improvement is applied by re-running, not by hand-patching pages.
- **Fenced nondeterminism.** The LLM only *proposes*; application is deterministic
  code (P2). The unpredictable part has a hard, testable boundary.
- **Swap without fear.** Model and prompts are versioned config behind one seam
  (P5); the eval set (§8) gates every swap. "Change the model" is one line plus a
  benchmark run.
- **No drift.** Anything computable is derived (P4); there is no second copy of the
  timeline or the links to fall out of sync.
- **Isolated blast radius.** Per-chat data (P8) means a bad run on one chat can
  never corrupt another, and layer boundaries mean an L4 change can't break L2.
- **Small surface.** A workflow (P6) with one write path (P2) is a few hundred
  lines of orchestration, not an agent framework to maintain.

---

## 8. Evaluation

The domain hands us hard ground truth: **citations either resolve to a real
message or they don't.** Lean on it.

- **Golden set:** a handful of hand-labeled chunks → expected entities, claims,
  citations.
- **Mechanical metrics (no judge needed):** citation validity (100% checkable via
  L1's `show`), citation precision/recall vs. golden, entity-resolution accuracy,
  **idempotency** (re-run a chunk → no-op), link/consistency integrity.
- **Judged metrics:** grounding / no-hallucination via a cheap LLM-judge on top.

The eval is what makes P5 safe: no model or prompt change ships without passing it.

---

## 9. Non-goals (recorded decisions, so we don't relitigate)

- **No full RLM.** Its ephemeral REPL, brittle finalization, and unbounded cost are
  wrong for a persistent, incrementally-maintained store. We borrow only its
  chunked map-reduce *pattern* for extraction (§4).
- **No autonomous agent.** The control flow is ours (P6). The model does not decide
  when to stop or what to run next.
- **No maintained timeline page.** Derived only (§3, P4).
- **No direct page writes.** Ever. One write path (P2).
- **No vendor lock-in.** No SDK or model leaks above L4 (P5).

---

## 10. Open decisions

Deliberately left open — to settle when we build, not before:

1. **Significance threshold & chunk size** — tune empirically against the eval set;
   these are the two main knobs for P7.
2. **Consolidation cadence** — time-based, page-size-triggered, or both.
3. **Index storage** — flat files vs. SQLite for the derived indexes. Start flat;
   promote to SQLite only if lookups get slow. (Not a source of truth either way.)

---

## 11. Build sequence

Order chosen so each step is independently testable and nothing is built before
it's needed.

1. **L1 gaps** the reducer depends on: `export --after/--before`, `people
   --unresolved`. Small, already scoped.
2. **L2 store**: page schema, entity registry, derived indexes, and the
   edit-applier — with zero LLM calls. Test the invariants in isolation.
3. **L4 seam**: `complete()` → OpenRouter/DeepSeek + model config.
4. **L3 reducer**: the six-step pipeline over *one* chunk.
5. **Eval harness** on a golden chunk before scaling.
6. **Fold** over the backfill (init), then wire the live delta path (same reducer).
7. **Consolidation mode** once there are pages worth refactoring.
