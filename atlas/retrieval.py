"""Shared page retrieval — one BM25 implementation for the MCP server and the
benchmark, so what we measure is exactly what agents get."""
from __future__ import annotations

import math
import re
from pathlib import Path

_WORD = re.compile(r"[a-z0-9']+")


def bm25_index(state, wiki_dir: Path):
    """(docs, idf, avg_len) over written pages; title/alias terms weighted 4x."""
    docs = {}
    for pid, pg in state["pages"].items():
        if pg["status"] != "written":
            continue
        path = wiki_dir / (pid + ".md")
        body = path.read_text().lower() if path.exists() else ""
        head = (pid.replace("/", " ") + " " + pg["title"] + " "
                + " ".join(pg.get("aliases", []))).lower()
        tf = {}
        for t in _WORD.findall(head):
            tf[t] = tf.get(t, 0) + 4
        for t in _WORD.findall(body):
            tf[t] = tf.get(t, 0) + 1
        docs[pid] = (tf, sum(tf.values()), pg["title"])
    n = len(docs) or 1
    avg = sum(l for _, l, _ in docs.values()) / n
    df = {}
    for tf, _, _ in docs.values():
        for t in tf:
            df[t] = df.get(t, 0) + 1
    idf = {t: math.log(1 + (n - d + 0.5) / (d + 0.5)) for t, d in df.items()}
    return docs, idf, avg


def bm25_find(index, query: str, k: int = 12) -> list:
    """Ranked [(score, pid, title)] for a natural-language query."""
    docs, idf, avg = index
    terms = [t for t in _WORD.findall(query.lower()) if len(t) > 2]
    k1, b = 1.4, 0.6
    scored = []
    for pid, (tf, length, title) in docs.items():
        sc = 0.0
        for t in terms:
            f = tf.get(t, 0)
            if f:
                sc += idf.get(t, 0) * f * (k1 + 1) / (f + k1 * (1 - b + b * length / avg))
        if sc > 0:
            scored.append((sc, pid, title))
    scored.sort(reverse=True)
    return scored[:k]
