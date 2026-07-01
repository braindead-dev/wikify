"""Layer 1 — extract granular observations from a chat, in parallel.

Each chunk is a plain string of ID-tagged messages; each extraction is one
structured-output completion (genuine json_schema, no tools). Chunks overlap so
nothing falls in a seam. Every observation is then validated against the real
message ids before it is written to `<chat_dir>/observations.json`.
"""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from pathlib import Path

from imessage import MessagesDB
from imessage.render import format_message

from .config import ExtractConfig
from .llm import LLMClient
from .observation import TYPES, Observation, observations_schema

_PROMPTS = Path(__file__).resolve().parent / "prompts"


@lru_cache(maxsize=None)
def _prompt(name):
    return (_PROMPTS / name).read_text()


def _toks(s: str) -> int:
    return max(1, len(s) // 4)               # ~4 chars/token estimate


def _participants(messages) -> list:
    return sorted({m.sender for m in messages if not m.system and m.sender})


def chunk_by_tokens(messages, chunk_tokens, overlap_tokens):
    """Split into ~chunk_tokens slices of ID-tagged lines, each overlapping the
    previous by ~overlap_tokens so a thread spanning a seam is seen whole."""
    lines = [format_message(m, ids=True) for m in messages]
    tks = [_toks(ln) for ln in lines]
    chunks, i, n = [], 0, len(lines)
    while i < n:
        j, t = i, 0
        while j < n and t < chunk_tokens:
            t += tks[j]
            j += 1
        chunks.append("\n".join(lines[i:j]))
        if j >= n:
            break
        back, ov = j, 0
        while back > i + 1 and ov < overlap_tokens:
            back -= 1
            ov += tks[back]
        i = back
    return chunks


def contact_directory(db, messages) -> str:
    """`raw handle -> resolved name` lines for the system prompt (env metadata)."""
    lines = [f"{h.value} -> {h.name}" for h in db.handles() if h.name and h.name != h.value]
    return "\n".join(lines) + "\n\nparticipants: " + ", ".join(_participants(messages))


def extract_chunk(chunk_text, contacts, schema, llm, effort) -> list:
    system = (_prompt("extract.md")
              .replace("{contacts}", contacts)
              .replace("{types}", ", ".join(TYPES)))
    user = ("Transcript chunk:\n" + chunk_text +
            "\n\nExtract every wiki-worthy observation from this chunk.")
    try:
        out = llm.complete_json(system, user, effort=effort,
                                schema=schema, schema_name="observations")
    except Exception as e:
        print(f"  chunk FAILED — {str(e)[:100]}", flush=True)
        return []
    raw = out.get("observations", []) if isinstance(out, dict) else []
    return [Observation.from_dict(o) for o in raw if isinstance(o, dict)]


def _dedup(observations) -> list:
    """Drop exact duplicates the chunk overlaps produce (same title + same sources).
    Fuzzy near-duplicates are left for a later layer to merge."""
    seen, out = set(), []
    for o in observations:
        key = (o.title.lower(), tuple(sorted(o.sources)))
        if key not in seen:
            seen.add(key)
            out.append(o)
    return out


def extract_all(chat_ids, config: ExtractConfig = None, limit_chunks=None, verbose=True):
    """Extract → validate → dedup. Returns a list of clean Observations."""
    config = config or ExtractConfig()
    # respect the user's merges/renames (same auto-detect as the imessage CLI)
    ident = "identities.json" if Path("identities.json").exists() else None
    db = MessagesDB(identities=ident)
    msgs = db.messages(chat_ids)
    valid_ids = {m.rowid for m in msgs}
    participants = _participants(msgs)
    contacts = contact_directory(db, msgs)
    schema = observations_schema(participants)

    chunks = chunk_by_tokens(msgs, config.chunk_tokens, config.overlap_tokens)
    if limit_chunks:
        chunks = chunks[:limit_chunks]
    if verbose:
        print(f"[extract] {len(msgs)} msgs → {len(chunks)} chunks "
              f"(~{config.chunk_tokens // 1000}k tok, {config.overlap_tokens // 1000}k overlap) "
              f"· {len(participants)} participants · model {config.model} · x{config.workers}",
              flush=True)

    llm = LLMClient(config.model)
    raw = []
    with ThreadPoolExecutor(max_workers=config.workers) as pool:
        for i, obs in enumerate(pool.map(
                lambda c: extract_chunk(c, contacts, schema, llm, config.effort), chunks), 1):
            raw.extend(obs)
            if verbose:
                print(f"  chunk {i}/{len(chunks)}: {len(obs)} observations", flush=True)

    clean = _dedup([c for o in raw if (c := o.cleaned(valid_ids, participants))])
    if verbose:
        dropped = len(raw) - len(clean)
        print(f"[extract] {len(raw)} raw → {len(clean)} valid "
              f"({dropped} dropped as invalid/duplicate) · {llm.usage}", flush=True)
    return clean


def save_observations(observations, path, meta=None):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {**(meta or {}), "count": len(observations),
               "observations": [o.to_dict() for o in observations]}
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False))


def build_observations(chat_dir, chat_ids, config: ExtractConfig = None,
                       limit_chunks=None, verbose=True):
    """Layer 1 end to end: extract → validate → write `<chat_dir>/observations.json`."""
    config = config or ExtractConfig()
    observations = extract_all(chat_ids, config, limit_chunks, verbose)
    path = Path(chat_dir) / "observations.json"
    save_observations(observations, path,
                      meta={"chat_ids": list(chat_ids), "model": config.model})
    if verbose:
        print(f"[extract] wrote {len(observations)} observations → {path}", flush=True)
    return observations
