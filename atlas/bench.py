"""Retrieval benchmark — measure the knowledge base instead of vibing it.

    python3 -m atlas bench my-chat [--n 40]

For a deterministic sample of written pages, an LLM writes a natural question
whose answer lives on that page (questions cache in `<chat_dir>/bench.json`,
keyed by page content hash, so re-runs are free until pages change). Then the
question is run through the SAME BM25 `find` the MCP serves, scoring whether
the source page comes back: hit@1, hit@5, and MRR. Run after any retrieval or
structure change — improvements should move numbers, not vibes.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .compose import _load_state
from .config import ComposeConfig
from .llm import LLMClient
from .retrieval import bm25_find, bm25_index
from .store import _atomic_write


def run_bench(chat_dir, n=40, config: ComposeConfig = None, verbose=True) -> dict:
    cfg = config or ComposeConfig()
    chat_dir = Path(chat_dir)
    wiki_dir = chat_dir / "wiki"
    state = _load_state(wiki_dir)
    written = sorted(pid for pid, p in state["pages"].items()
                     if p["status"] == "written" and (wiki_dir / (pid + ".md")).exists())
    # deterministic, spread sample: order by hash of pid
    sample = sorted(written, key=lambda pid: hashlib.sha1(pid.encode()).hexdigest())[:n]

    bench_path = chat_dir / "bench.json"
    cached = json.loads(bench_path.read_text()) if bench_path.exists() else {}
    llm = LLMClient(cfg.model)
    probes = {}
    for pid in sample:
        body = (wiki_dir / (pid + ".md")).read_text()
        key = f"{pid}:{hashlib.sha1(body.encode()).hexdigest()[:10]}"
        if key in cached:
            probes[pid] = cached[key]
            continue
        out = llm.complete_json(
            "You write ONE retrieval-benchmark question for a knowledge-base page. "
            "The question must be answerable from this page, phrased the way a "
            "person would naturally ask from memory — NO title words copied "
            "verbatim, no page ids. JSON only.",
            body[:6000] + '\n\nReturn JSON: {"question": "..."}',
            effort="low", temperature=0.6, max_tokens=1500)
        q = str(out.get("question", "")).strip()
        if q:
            probes[pid] = cached[key] = q
    _atomic_write(bench_path, cached)

    index = bm25_index(state, wiki_dir)
    hit1 = hit5 = 0
    mrr = 0.0
    misses = []
    for pid, q in probes.items():
        ranked = [p for _, p, _ in bm25_find(index, q, k=10)]
        # a family counts: retrieving parent or sibling of the target is a hit
        top = pid.split("/")[0] + "/" + pid.split("/")[1] if pid.count("/") >= 1 else pid
        rank = next((i for i, r in enumerate(ranked)
                     if r == pid or r.startswith(top)), None)
        if rank == 0:
            hit1 += 1
        if rank is not None and rank < 5:
            hit5 += 1
        if rank is not None:
            mrr += 1 / (rank + 1)
        else:
            misses.append((pid, q))
    total = len(probes) or 1
    result = {"n": len(probes), "hit@1": round(hit1 / total, 3),
              "hit@5": round(hit5 / total, 3), "mrr": round(mrr / total, 3)}
    if verbose:
        print(f"[bench] {result['n']} probes · hit@1 {result['hit@1']:.0%} · "
              f"hit@5 {result['hit@5']:.0%} · MRR {result['mrr']:.2f}", flush=True)
        for pid, q in misses[:5]:
            print(f"  miss: {pid} ← \"{q[:90]}\"", flush=True)
    return result
