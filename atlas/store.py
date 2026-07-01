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

    def __init__(self, chat_dir, meta, n_chunks):
        self.dir = Path(chat_dir)
        self.chunks_dir = self.dir / "chunks"
        self.manifest_path = self.dir / "manifest.json"
        self.obs_path = self.dir / "observations.json"
        self.meta = meta
        self.n_chunks = n_chunks
        self.manifest = self._load_or_init()

    def _config(self):
        return {k: self.meta[k] for k in _CONFIG_KEYS}

    def _load_or_init(self):
        # resume only if the on-disk run matches this config and chunk count;
        # otherwise the chunking differs and old chunk files are meaningless.
        if self.manifest_path.exists():
            m = json.loads(self.manifest_path.read_text())
            if m.get("config") == self._config() and len(m.get("chunks", [])) == self.n_chunks:
                return m
        return {"config": self._config(),
                "chunks": [{"index": i, "status": "pending", "count": 0,
                            "first_id": None, "last_id": None, "n_messages": None, "error": None}
                           for i in range(self.n_chunks)]}

    def set_spans(self, chunks):
        """Record the row-id span each chunk covers (for status/inspection)."""
        for i, c in enumerate(chunks):
            e = self.manifest["chunks"][i]
            e["first_id"], e["last_id"], e["n_messages"] = c["first_id"], c["last_id"], c["n_messages"]
        self._save()

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
        self.manifest_path.unlink(missing_ok=True)
        self.obs_path.unlink(missing_ok=True)
        self.manifest = self._load_or_init()

    def _save(self):
        self.dir.mkdir(parents=True, exist_ok=True)
        _atomic_write(self.manifest_path, self.manifest)
