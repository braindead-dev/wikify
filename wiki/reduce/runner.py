"""The runner — folds the reducer over a conversation and owns per-chat state.

`ingest` is the same operation whether it's the first backfill or a live delta
(P1/P7): it processes only messages newer than the watermark, in day-aligned
chunks, checkpointing after each so a run is resumable and idempotent. A failing
chunk is logged and skipped — one bad slice never sinks the run (resilience).

Message loading and the model client are lazy, so read-only views (status, show,
pages) need neither the source DB scan nor an API key.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from ..llm import DEFAULT_MODEL, LLMClient
from ..store import Page, Store, slugify, timeline, verify
from .chunk import chunk_messages
from .reducer import Reducer


class Runner:
    def __init__(self, chat_dir, chat_ids, title, model=DEFAULT_MODEL,
                 chunk_size=300, db=None):
        self.dir = Path(chat_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.store = Store(self.dir / "kb")
        self.chat_ids = list(chat_ids)
        self.title = title
        self.chunk_size = chunk_size
        self.model = model
        self._db = db
        self._msgs = None

    # -- lazy source access --------------------------------------------------
    @property
    def db(self):
        if self._db is None:
            from imessage import MessagesDB
            ident = self.dir / "identities.json"
            self._db = MessagesDB(identities=str(ident) if ident.exists() else None)
        return self._db

    def _messages(self):
        if self._msgs is None:
            self._msgs = self.db.messages(self.chat_ids)
            self._valid = {m.rowid for m in self._msgs}
            self._ts = {m.rowid: m.ts for m in self._msgs}
        return self._msgs

    def resolves(self, mid) -> bool:
        self._messages()
        return mid in self._valid

    # -- state ---------------------------------------------------------------
    @property
    def _state_file(self):
        return self.dir / "state.json"

    def load_state(self) -> dict:
        if self._state_file.exists():
            return json.loads(self._state_file.read_text())
        return {"chat_ids": self.chat_ids, "title": self.title, "model": self.model,
                "watermark": 0, "chunks_done": 0, "failures": []}

    def _save_state(self, st: dict):
        self._state_file.write_text(json.dumps(st, indent=2))

    # -- seeding (deterministic, no LLM) ------------------------------------
    def seed(self):
        if not self.store.exists("index"):
            self.store.write(Page(
                id="index", type="index", title=self.title, pinned=True,
                body=f"Wiki for **{self.title}** — people, events, and topics, "
                     f"each claim cited to a message.", updated=_today()))
        for sender in sorted({m.sender for m in self._messages() if not m.system}):
            pid = f"person/{slugify(sender)}"
            if not self.store.exists(pid):
                self.store.write(Page(id=pid, type="person", title=sender, pinned=True))

    # -- the fold ------------------------------------------------------------
    def ingest(self, after=None, before=None, max_chunks=None, verbose=True):
        self._messages()
        self.seed()
        reducer = Reducer(self.store, LLMClient(self.model), self.title, self.resolves)
        st = self.load_state()
        window = self.db.messages(self.chat_ids, since=after, until=before)
        pending = [m for m in window if m.rowid > st["watermark"]]
        chunks = chunk_messages(pending, self.chunk_size)
        if max_chunks:
            chunks = chunks[:max_chunks]
        if verbose:
            print(f"  {len(pending)} new messages → {len(chunks)} chunks "
                  f"(model {reducer.llm.name})")

        results = []
        for i, chunk in enumerate(chunks, 1):
            span = f"{chunk[0].ts:%Y-%m-%d}..{chunk[-1].ts:%Y-%m-%d}"
            try:
                cs = reducer.reduce_chunk(chunk)
            except Exception as e:                       # never let one chunk kill the run
                st["failures"].append({"span": span, "error": str(e)[:200]})
                self._save_state(st)
                if verbose:
                    print(f"  chunk {i}/{len(chunks)} [{span}]: FAILED — {str(e)[:120]}")
                continue
            for pid in cs.touched:                       # stamp freshness
                page = self.store.read(pid)
                if page:
                    page.updated = _today()
                    self.store.write(page)
            st["watermark"] = max(st["watermark"], max(m.rowid for m in chunk))
            st["chunks_done"] += 1
            self._save_state(st)
            results.append(cs)
            if verbose:
                print(f"  chunk {i}/{len(chunks)} [{span}]: {cs.summary()}  ·  {reducer.llm.usage}")
        return results

    # -- consolidation (mode 2) ---------------------------------------------
    def consolidate(self, page_ids=None, min_cites=8, verbose=True):
        from .consolidate import Consolidator
        self._messages()
        con = Consolidator(self.store, LLMClient(self.model), self.resolves)
        if page_ids is None:
            page_ids = [p.id for p in self.store.all_pages()
                        if p.type != "index" and len(p.sources) >= min_cites]
        done = []
        for pid in page_ids:
            try:
                cs, dropped = con.consolidate_page(pid)
            except Exception as e:
                if verbose:
                    print(f"  {pid}: FAILED — {str(e)[:100]}")
                continue
            if dropped:
                if verbose:
                    print(f"  {pid}: reverted (would drop cites {sorted(dropped)})")
                continue
            if cs:
                page = self.store.read(pid)
                page.updated = _today()
                self.store.write(page)
                done.append(pid)
                if verbose:
                    print(f"  {pid}: consolidated  ·  {con.llm.usage}")
        return done

    # -- derived views -------------------------------------------------------
    def timeline(self):
        self._messages()
        return timeline(self.store, self._ts.get)

    def verify(self):
        return verify(self.store, self.resolves)


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")
