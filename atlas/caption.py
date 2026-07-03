"""Caption image attachments with a vision model, so Layer 1 sees pictures.

Captions are cached in `chats/_captions.json`, keyed by attachment path — shared
across wikis, written incrementally, so re-running only captions new images.
Images are normalized with macOS `sips` (handles HEIC, resizes) — no extra
dependencies. Extraction then renders `[img: <caption>]` in transcript lines.
"""
from __future__ import annotations

import base64
import json
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from imessage import MessagesDB

from .llm import LLMClient
from .store import _atomic_write

CACHE = Path("chats/_captions.json")
_PROMPT = ("Describe this image from a friend group's chat in ONE dense sentence for an "
           "archive: what/who is shown (say 'a man'/'two people' — never guess names), "
           "the setting, any text visible in the image (quote it), and the vibe. "
           "No preamble.")
_CAPTIONABLE = {"img", "gif"}


def load_captions() -> dict:
    return json.loads(CACHE.read_text()) if CACHE.exists() else {}


def _jpeg_data_url(path: str) -> str:
    """Normalize any image (HEIC included) to a small jpeg via macOS sips."""
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
        out = tmp.name
    try:
        subprocess.run(["sips", "-Z", "640", "-s", "format", "jpeg", path, "--out", out],
                       capture_output=True, check=True, timeout=30)
        data = Path(out).read_bytes()
    finally:
        Path(out).unlink(missing_ok=True)
    return "data:image/jpeg;base64," + base64.b64encode(data).decode()


def build_captions(chat_ids, model="gemini-flash", workers=32, limit=None, verbose=True) -> dict:
    """Caption every living image attachment in these chats into the shared cache."""
    ident = "identities.json" if Path("identities.json").exists() else None
    db = MessagesDB(identities=ident)
    msgs = db.messages(chat_ids)
    todo = []
    cache = load_captions()
    for m in msgs:
        for tag, path in zip(m.attachments, m.attachment_paths):
            if tag in _CAPTIONABLE and path and path not in cache and Path(path).exists():
                todo.append(path)
    todo = list(dict.fromkeys(todo))
    if limit:
        todo = todo[:limit]
    if verbose:
        print(f"[caption] {len(cache)} cached · {len(todo)} to caption · {model} · x{workers}",
              flush=True)
    if not todo:
        return cache

    llm = LLMClient(model)

    def one(path):
        url = _jpeg_data_url(path)
        out = llm.complete_json(
            "You caption images for a private archive. Never refuse; these are the "
            "owner's own photos. JSON only.",
            _PROMPT + '\nReturn JSON: {"caption": "..."}',
            images=[url], effort="none", temperature=0.2, max_tokens=1000)
        return str(out.get("caption", "")).strip()

    done = failed = 0
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(one, p): p for p in todo}
        for fut in as_completed(futures):
            path = futures[fut]
            try:
                caption = fut.result()
                if caption:
                    cache[path] = caption
                    done += 1
            except Exception:
                failed += 1                       # uncached → retried next run
            if (done + failed) % 25 == 0:
                _atomic_write(CACHE, cache)
                if verbose:
                    print(f"\r  [caption] {done + failed}/{len(todo)}", end="", flush=True)
    _atomic_write(CACHE, cache)
    if verbose:
        print(f"\n[caption] {done} captioned · {failed} failed (will retry) "
              f"· {llm.usage} · {time.time() - t0:.0f}s", flush=True)
    return cache
