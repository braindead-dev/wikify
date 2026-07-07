"""Layer 1 — extract granular observations from a chat, in parallel.

Each chunk is a plain string of ID-tagged messages; each extraction is one
structured-output completion (genuine json_schema, no tools). Chunks overlap so
nothing falls in a seam. Observations are validated against the real message ids,
then streamed to disk chunk by chunk via `RunStore` — resumable and order-preserving.
"""
from __future__ import annotations

import hashlib
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

from sources.fetch import fetch_streams
from sources.imessage.render import format_message

from .caption import load_captions
from .transcribe import load_transcripts
from .config import ExtractConfig
from .llm import LLMClient, LLMError
from .observation import Observation, observations_schema
from .store_db import import_items
from .store import RunStore

_PROMPT = Path(__file__).resolve().parent / "prompts" / "extract.md"
_DOC_PROMPT = Path(__file__).resolve().parent / "prompts" / "extract_document.md"


def _toks(s: str) -> int:
    return max(1, len(s) // 4)               # ~4 chars/token estimate


def _participants(messages) -> list:
    return sorted({m.sender for m in messages if not m.system and m.sender})


def _render_line(m, captions) -> str:
    """One transcript line; image placeholders become `[img: <caption>]` when the
    attachment has been captioned."""
    line = format_message(m, ids=True)
    for tag, path in zip(m.attachments, m.attachment_paths):
        caption = captions.get(path or "")
        if caption:
            line = line.replace(f"[{tag}]", f"[{tag}: {caption}]", 1)
    return line


def chunk_messages(messages, chunk_tokens, overlap_tokens, captions=None, stream="") -> list:
    """Split one conversation stream into ~chunk_tokens slices of ID-tagged
    lines, each overlapping the previous by ~overlap_tokens so a thread spanning
    a seam is seen whole. Each chunk carries its text (headed by its channel),
    the id span it covers, and a content hash (so a chunk whose rendering
    changed — e.g. newly captioned images — re-extracts)."""
    captions = captions or {}
    head = f"(channel: {stream})\n" if stream else ""
    lines = [_render_line(m, captions) for m in messages]
    tks = [_toks(ln) for ln in lines]
    chunks, i, n = [], 0, len(lines)
    while i < n:
        j, t = i, 0
        while j < n and t < chunk_tokens:
            t += tks[j]
            j += 1
        text = head + "\n".join(lines[i:j])
        chunks.append({"text": text, "first_id": messages[i].rowid,
                       "last_id": messages[j - 1].rowid, "n_messages": j - i,
                       "text_hash": hashlib.sha1(text.encode()).hexdigest()[:10]})
        if j >= n:
            break
        back, ov = j, 0
        while back > i + 1 and ov < overlap_tokens:
            back -= 1
            ov += tks[back]
        i = back
    return chunks


def chunk_streams(streams, chunk_tokens, overlap_tokens, captions=None) -> list:
    """Chunk every stream independently (parallel channels never interleave in a
    transcript) and concatenate in stream order — one flat chunk list. Each chunk
    carries its stream's kind (chat/document) so extraction can shape its prompt
    and schema per kind."""
    out = []
    for s in streams:
        chunks = chunk_messages(s["messages"], chunk_tokens, overlap_tokens,
                                captions, stream=s["label"] if len(streams) > 1 else "")
        for c in chunks:
            c["kind"] = s.get("kind", "chat")
        out += chunks
    return out


def system_prompt(db, messages) -> str:
    """The extraction system prompt: role + behavior, with the contact directory
    (raw handle -> resolved name, plus the participant roster) substituted in.
    Identical for every chunk, so it is built once per run. Sources without a
    contact database (senders already resolved) contribute the roster only."""
    directory = [f"{h.value} -> {h.name}" for h in db.handles()
                 if h.name and h.name != h.value] if db else []
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
    Path(chat_dir).mkdir(parents=True, exist_ok=True)
    until = datetime.fromisoformat(config.until) if config.until else None
    streams, db = fetch_streams(chat_ids, until=until)
    msgs = [m for s in streams for m in s["messages"]]
    import_items(sorted(msgs, key=lambda m: m.ts))
    valid_ids = {m.rowid for m in msgs}
    chat_msgs = [m for st in streams if st.get("kind", "chat") == "chat"
                 for m in st["messages"]]
    participants = _participants(chat_msgs or msgs)
    system = system_prompt(db, chat_msgs or msgs)
    if len(streams) > 1:
        system += ("\n\nThis record spans several channels (" +
                   "; ".join(s["label"] for s in streams) +
                   ") — the same people throughout. Each slice you receive is "
                   "from one channel, named at the top.")
    doc_system = _DOC_PROMPT.read_text().replace(
        "{contacts}", "known people: " + ", ".join(participants)) if any(
        st.get("kind") == "document" for st in streams) else None
    schema = observations_schema(participants)
    doc_schema = observations_schema(None)
    chunks = chunk_streams(streams, config.chunk_tokens, config.overlap_tokens,
                           {**load_captions(), **load_transcripts()})

    specs = [int(s) if str(s).isdigit() else str(s) for s in chat_ids]
    meta = {"chat_ids": specs, "model": config.model,
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
        doc = chunks[i].get("kind") == "document"
        sys_i, schema_i = (doc_system, doc_schema) if doc else (system, schema)
        kw = dict(effort=config.effort, max_tokens=config.max_tokens or None,
                  trace=trace_sink(i), temperature=config.temperature)
        try:
            obs = extract_chunk(llm, sys_i, chunks[i]["text"], schema_i, **kw)
        except LLMError as e:
            if "truncated" not in str(e):
                raise
            kw["effort"] = "low"    # truncation = reasoning burn — rerun as a scan
            obs = extract_chunk(llm, sys_i, chunks[i]["text"], schema_i, **kw)
        if len(obs) < config.min_density * chunks[i]["n_messages"]:
            retry = extract_chunk(llm, sys_i, chunks[i]["text"], schema_i, **kw)
            if len(retry) > len(obs):
                obs = retry
        return obs

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(extract_dense, i): i for i in todo}
        for fut in as_completed(futures):
            i = futures[fut]
            try:
                allowed = None if chunks[i].get("kind") == "document" else participants
                store.write_chunk(i, [c for o in fut.result()
                                      if (c := o.cleaned(valid_ids, allowed))])
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
