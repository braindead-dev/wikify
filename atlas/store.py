"""On-disk layout for a Layer 1 run — streamed, resumable, order-preserving.

    chats/<slug>/
      manifest.json        run config + per-chunk status (pending | done | failed)
      chunks/NNN.json      one file per chunk, written the instant it finishes
      observations.json    every observation, assembled in chunk order

Each chunk is persisted the moment it completes, so an interrupted run loses
nothing: re-running resumes the pending/failed chunks and leaves the done ones
untouched. All writes are atomic (temp file + rename).
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

_CONFIG_KEYS = ("chat_ids", "model", "chunk_tokens", "overlap_tokens")


def _atomic_write(path: Path, data):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    tmp.replace(path)


class RunStore:
    """Tracks and persists one extraction run under `chat_dir`."""

    def __init__(self, chat_dir, meta, chunks):
        self.dir = Path(chat_dir)
        self.chunks_dir = self.dir / "chunks"
        self.traces_dir = self.dir / "traces"
        self.manifest_path = self.dir / "manifest.json"
        self.obs_path = self.dir / "observations.json"
        self.meta = meta
        self.spans = [{"first_id": c["first_id"], "last_id": c["last_id"],
                       "n_messages": c["n_messages"], "text_hash": c.get("text_hash")}
                      for c in chunks]
        self.n_chunks = len(chunks)
        self.restarted = False               # True when a prior run's config didn't match
        self.carried = 0                     # done chunks carried over from the prior run
        self.manifest = self._load_or_init()

    def _config(self):
        return {k: self.meta[k] for k in _CONFIG_KEYS}

    def _fresh_manifest(self):
        return {"config": self._config(),
                "chunks": [{"index": i, "status": "pending", "count": 0,
                            **self.spans[i], "error": None}
                           for i in range(self.n_chunks)]}

    def _load_or_init(self):
        # One mechanism covers both resume and grown data: a done chunk from the
        # prior run is carried over iff it covers exactly the same message span at
        # the same index. When new messages arrive, the chunking's prefix is
        # unchanged, so old chunks carry and only the tail (re)runs. A config
        # change invalidates everything (surfaced via `self.restarted`).
        if self.manifest_path.exists():
            m = json.loads(self.manifest_path.read_text())
            if m.get("config") == self._config():
                fresh = self._fresh_manifest()
                old = m.get("chunks", [])
                for i, e in enumerate(fresh["chunks"]):
                    if (i < len(old) and old[i].get("status") == "done"
                            and all(old[i].get(k) == e[k]
                                    for k in ("first_id", "last_id", "n_messages", "text_hash"))):
                        fresh["chunks"][i] = old[i]
                        self.carried += 1
                return fresh
            self.restarted = True
            shutil.rmtree(self.chunks_dir, ignore_errors=True)
            shutil.rmtree(self.traces_dir, ignore_errors=True)
        return self._fresh_manifest()

    def done_count(self):
        return sum(e["status"] == "done" for e in self.manifest["chunks"])

    def pending(self):
        """Indices still to run (anything not yet done — pending or failed)."""
        return [e["index"] for e in self.manifest["chunks"] if e["status"] != "done"]

    def failed(self):
        return [e["index"] for e in self.manifest["chunks"] if e["status"] == "failed"]

    def write_chunk(self, index, observations):
        self.chunks_dir.mkdir(parents=True, exist_ok=True)
        _atomic_write(self.chunks_dir / f"{index:03d}.json",
                      {"index": index, "count": len(observations),
                       "observations": [o.to_dict() for o in observations]})
        e = self.manifest["chunks"][index]
        e["status"], e["count"], e["error"] = "done", len(observations), None
        self._save()

    def mark_failed(self, index, error):
        e = self.manifest["chunks"][index]
        e["status"], e["error"] = "failed", str(error)[:300]
        self._save()

    def write_trace(self, index, record):
        """Persist the full request/response of a chunk's call for observability
        (`traces/NNN.json`). Written from the worker thread; the path is unique
        per chunk, so no locking is needed."""
        self.traces_dir.mkdir(parents=True, exist_ok=True)
        _atomic_write(self.traces_dir / f"{index:03d}.json", record)

    def assemble(self):
        """Concatenate all done chunk files, in index order, into observations.json."""
        obs = []
        for e in self.manifest["chunks"]:
            path = self.chunks_dir / f"{e['index']:03d}.json"
            if e["status"] == "done" and path.exists():
                obs.extend(json.loads(path.read_text())["observations"])
        _atomic_write(self.obs_path,
                      {**self.meta, "chunks_done": self.done_count(), "chunks_total": self.n_chunks,
                       "count": len(obs), "observations": obs})
        return obs

    def reset(self):
        shutil.rmtree(self.chunks_dir, ignore_errors=True)
        shutil.rmtree(self.traces_dir, ignore_errors=True)
        self.manifest_path.unlink(missing_ok=True)
        self.obs_path.unlink(missing_ok=True)
        self.manifest = self._fresh_manifest()

    def _save(self):
        self.dir.mkdir(parents=True, exist_ok=True)
        _atomic_write(self.manifest_path, self.manifest)
