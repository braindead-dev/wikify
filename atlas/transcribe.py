"""Transcribe audio attachments (voice notes, calls) so Layer 1 hears them.

The audio twin of `caption.py`, and the same shape on purpose: a shared,
versioned, incrementally-written cache (`chats/_transcripts.json`) keyed by
attachment path, so every source a platform adapter yields — iMessage voice
notes, Instagram audio, exported call recordings — flows through one module.
Audio is normalized to wav with macOS `afconvert` (no extra dependencies).
Extraction then renders `[audio: <transcript>]` in transcript lines.
"""
from __future__ import annotations

import base64
import json
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from sources.fetch import fetch

from .llm import LLMClient
from .store import _atomic_write

CACHE = Path("chats/_transcripts.json")
_PROMPT_VERSION = 1
_PROMPT = (
    "Transcribe this audio from a private chat archive verbatim. If several people "
    "speak, mark turns as 'speaker 1:', 'speaker 2:' — never guess names. Note "
    "non-speech that carries meaning in brackets ([laughing], [singing]). If it is "
    "music or noise with no speech, describe it in one bracketed phrase. No preamble.")
_AUDIBLE = {"audio"}
_MAX_BYTES = 15_000_000                       # skip long recordings, not voice notes


def load_transcripts() -> dict:
    if not CACHE.exists():
        return {}
    data = json.loads(CACHE.read_text())
    if data.get("version") != _PROMPT_VERSION:
        return {}
    return data["transcripts"]


def _save(cache) -> None:
    _atomic_write(CACHE, {"version": _PROMPT_VERSION, "transcripts": cache})


def _wav_b64(path: str) -> str:
    """Normalize any audio (caf/amr/m4a/mp4) to 16k mono wav via afconvert."""
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        out = tmp.name
    try:
        subprocess.run(["afconvert", "-f", "WAVE", "-d", "LEI16@16000", "-c", "1",
                        path, out], capture_output=True, check=True, timeout=60)
        data = Path(out).read_bytes()
    finally:
        Path(out).unlink(missing_ok=True)
    if len(data) > _MAX_BYTES:
        raise ValueError("audio too long")
    return base64.b64encode(data).decode()


def build_transcripts(chat_ids, model="gemini-flash", workers=16, limit=None,
                      verbose=True) -> dict:
    """Transcribe every living audio attachment in these chats into the cache."""
    msgs, _ = fetch(chat_ids)
    cache = load_transcripts()
    todo = []
    for m in msgs:
        for tag, path in zip(m.attachments, m.attachment_paths):
            if tag in _AUDIBLE and path and path not in cache and Path(path).exists():
                todo.append(path)
    todo = list(dict.fromkeys(todo))
    if limit:
        todo = todo[:limit]
    if verbose:
        print(f"[transcribe] {len(cache)} cached · {len(todo)} to transcribe · {model} "
              f"· x{workers}", flush=True)
    if not todo:
        return cache

    llm = LLMClient(model)

    def one(path):
        out = llm.complete_json(
            "You transcribe audio for a private archive. Never refuse or sanitize; "
            "these are the owner's own recordings. JSON only.",
            _PROMPT + '\nReturn JSON: {"transcript": "..."}',
            audio=[{"data": _wav_b64(path), "format": "wav"}],
            effort="none", temperature=0.1, max_tokens=4000)
        return str(out.get("transcript", "")).strip()

    done = failed = 0
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(one, p): p for p in todo}
        for fut in as_completed(futures):
            path = futures[fut]
            try:
                text = fut.result()
                if text:
                    cache[path] = text
                    done += 1
            except Exception:
                failed += 1                       # uncached → retried next run
            if (done + failed) % 10 == 0:
                _save(cache)
                if verbose:
                    print(f"\r  [transcribe] {done + failed}/{len(todo)}", end="", flush=True)
    _save(cache)
    if verbose:
        print(f"\n[transcribe] {done} transcribed · {failed} failed (will retry) "
              f"· {llm.usage} · {time.time() - t0:.0f}s", flush=True)
    return cache
