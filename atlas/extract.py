"""Layer 1 — extract granular observations from a chat, in parallel.

Each chunk is a plain string of ID-tagged messages; each extraction is one
structured-output completion (genuine json_schema, no tools). Chunks overlap so
nothing falls in a seam. Observations are validated against the real message ids,
then streamed to disk chunk by chunk via `RunStore` — resumable and order-preserving.
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

from imessage import MessagesDB
from imessage.render import format_message

from .config import ExtractConfig
from .llm import LLMClient
from .observation import Observation, observations_schema
from .store import RunStore

_PROMPT = Path(__file__).resolve().parent / "prompts" / "extract.md"


def _toks(s: str) -> int:
    return max(1, len(s) // 4)               # ~4 chars/token estimate


def _participants(messages) -> list:
    return sorted({m.sender for m in messages if not m.system and m.sender})


def chunk_messages(messages, chunk_tokens, overlap_tokens) -> list:
    """Split into ~chunk_tokens slices of ID-tagged lines, each overlapping the
    previous by ~overlap_tokens so a thread spanning a seam is seen whole. Each
    chunk carries its text and the row-id span it covers."""
    lines = [format_message(m, ids=True) for m in messages]
    tks = [_toks(ln) for ln in lines]
    chunks, i, n = [], 0, len(lines)
    while i < n:
        j, t = i, 0
        while j < n and t < chunk_tokens:
            t += tks[j]
            j += 1
        chunks.append({"text": "\n".join(lines[i:j]), "first_id": messages[i].rowid,
                       "last_id": messages[j - 1].rowid, "n_messages": j - i})
        if j >= n:
            break
        back, ov = j, 0
        while back > i + 1 and ov < overlap_tokens:
            back -= 1
            ov += tks[back]
        i = back
    return chunks


def system_prompt(db, messages) -> str:
    """The extraction system prompt: role + behavior, with the contact directory
    (raw handle -> resolved name, plus the participant roster) substituted in.
    Identical for every chunk, so it is built once per run."""
    directory = [f"{h.value} -> {h.name}" for h in db.handles()
                 if h.name and h.name != h.value]
    contacts = "\n".join(directory) + "\n\nparticipants: " + ", ".join(_participants(messages))
    return _PROMPT.read_text().replace("{contacts}", contacts)


def extract_chunk(llm, system, chunk_text, schema, *, effort, max_tokens, trace,
                  temperature=None) -> list:
    """Extract one chunk. Raises on API failure (after retries) so the caller can
    record it; a chunk with genuinely nothing to say returns an empty list."""
    user = ("Transcript chunk:\n" + chunk_text +
            "\n\nExtract every wiki-worthy observation from this chunk.")
    out = llm.complete_json(system, user, effort=effort, schema=schema,
                            schema_name="observations", trace=trace,
                            max_tokens=max_tokens, temperature=temperature)
    raw = out.get("observations", []) if isinstance(out, dict) else []
    return [Observation.from_dict(o) for o in raw if isinstance(o, dict)]


def _bar(done, total, width=26):
    fill = int(width * done / total) if total else width
    return "█" * fill + "░" * (width - fill)


def build_observations(chat_dir, chat_ids, config: ExtractConfig = None,
                       resume=True, limit_chunks=None, verbose=True) -> list:
    """Layer 1 end to end; returns the full list of `Observation`s.

    Extracts every chunk in parallel and streams each to disk the instant it
    finishes (`<chat_dir>/chunks/NNN.json`), tracking per-chunk status in
    `manifest.json` and assembling `observations.json` in chunk order. Resumable:
    re-running skips done chunks and retries the rest. Validation only drops
    sources that aren't real message ids (and the observation if none survive)
    and non-participant people — never merges or dedups, so Layer 1 stays a
    faithful capture."""
    config = config or ExtractConfig()
    # respect the user's merges/renames (same auto-detect as the imessage CLI)
    ident = "identities.json" if Path("identities.json").exists() else None
    db = MessagesDB(identities=ident)
    until = datetime.fromisoformat(config.until) if config.until else None
    msgs = db.messages(chat_ids, until=until)
    valid_ids = {m.rowid for m in msgs}
    participants = _participants(msgs)
    system = system_prompt(db, msgs)
    schema = observations_schema(participants)
    chunks = chunk_messages(msgs, config.chunk_tokens, config.overlap_tokens)

    meta = {"chat_ids": list(chat_ids), "model": config.model,
            "chunk_tokens": config.chunk_tokens, "overlap_tokens": config.overlap_tokens}
    store = RunStore(chat_dir, meta, chunks)
    if not resume:
        store.reset()

    todo = store.pending()
    if limit_chunks:
        todo = todo[:limit_chunks]
    total = len(chunks)
    done = total - len(store.pending())
    workers = config.workers or max(1, len(todo))     # 0 → all pending chunks at once
    if verbose:
        print(f"[extract] {len(msgs)} msgs → {total} chunks · {len(participants)} participants "
              f"· {config.model} · x{workers} workers", flush=True)
        if store.restarted:
            print("  config changed — prior run discarded, starting fresh", flush=True)
        elif store.carried and len(todo):
            print(f"  {store.carried} chunks carried over — {len(todo)} to run", flush=True)

    def trace_sink(index):
        # failures are always traced (so they stay diagnosable); successful calls
        # are traced only when config.trace is on.
        def sink(record):
            if config.trace or record.get("status") == "error":
                store.write_trace(index, record)
        return sink

    llm = LLMClient(config.model)

    def extract_dense(i):
        """One chunk, with the variance gate: a run that lands far under the
        expected observation density is a lazy sample, not a sparse chunk —
        take one more sample and keep the richer result."""
        kw = dict(effort=config.effort, max_tokens=config.max_tokens or None,
                  trace=trace_sink(i), temperature=config.temperature)
        obs = extract_chunk(llm, system, chunks[i]["text"], schema, **kw)
        if len(obs) < config.min_density * chunks[i]["n_messages"]:
            retry = extract_chunk(llm, system, chunks[i]["text"], schema, **kw)
            if len(retry) > len(obs):
                obs = retry
        return obs

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(extract_dense, i): i for i in todo}
        for fut in as_completed(futures):
            i = futures[fut]
            try:
                store.write_chunk(i, [c for o in fut.result()
                                      if (c := o.cleaned(valid_ids, participants))])
            except Exception as e:
                store.mark_failed(i, e)
                if verbose:
                    print(f"\n  chunk {i} failed — {str(e)[:80]}", flush=True)
            done += 1
            if verbose:
                print(f"\r  [{_bar(done, total)}] {done}/{total} chunks", end="", flush=True)
    if verbose:
        print(flush=True)

    observations = [Observation.from_dict(o) for o in store.assemble()]
    if verbose:
        failed = store.failed()
        msg = f"[extract] {len(observations)} observations → {store.obs_path}"
        if failed:
            msg += f" · {len(failed)} chunks failed, rerun to retry: {failed}"
        print(msg + f" · {llm.usage} · {time.time() - t0:.0f}s", flush=True)
    return observations
