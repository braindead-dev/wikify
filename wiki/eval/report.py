"""Evaluation — the quality signals that make model/prompt swaps safe (§8).

Two kinds:
  * mechanical (free, no judge): citation/link integrity, idempotency, coverage.
    Citations either resolve to a real message or they don't — hard ground truth.
  * judged (a cheap LLM): grounding — does the cited evidence actually support the
    claim? Catches faithful-looking hallucination that integrity can't.
"""
from __future__ import annotations

import random
import re

from ..store.page import CITE_RE

_SENTENCE = re.compile(r"(?<=[.!?])\s+")

JUDGE_SYS = ("You audit a wiki. You are given a claim and chat messages: lines "
             "marked » are the messages the claim cites; unmarked lines are "
             "surrounding context for reference only. Decide whether the cited (») "
             "messages — read in context — support the claim. Reasonable synthesis "
             "and paraphrase are fine; mark false only if the claim is unsupported, "
             "overstated, or misattributed to the wrong speaker. JSON only.")

JUDGE_TMPL = """Claim: "{claim}"

Messages (» = cited, others = context):
{messages}

Do the cited (») messages support the claim? Reply JSON:
{"supported": true|false, "reason": "<one short phrase>"}"""


def claims(store) -> list:
    """Every cited *sentence* as {page, text, cites}. Sentence-level (not
    paragraph-level) so each claim is judged against exactly its own citations."""
    out = []
    for page in store.all_pages():
        for line in page.body.split("\n"):
            if not line.strip() or line.startswith("#"):
                continue
            para = line.lstrip("-*").strip()
            for sent in _SENTENCE.split(para):
                cites = [int(m.group(1)) for m in CITE_RE.finditer(sent)]
                if cites:
                    text = CITE_RE.sub("", sent).strip(" -*\t").strip()
                    out.append({"page": page.id, "text": text, "cites": cites})
    return out


def stats(store, total_messages=None) -> dict:
    pages = list(store.all_pages())
    cl = claims(store)
    cited = {mid for c in cl for mid in c["cites"]}
    by_type = {}
    for p in pages:
        by_type[p.type] = by_type.get(p.type, 0) + 1
    return {
        "pages": len(pages),
        "by_type": by_type,
        "claims": len(cl),
        "citations": sum(len(c["cites"]) for c in cl),
        "distinct_messages_cited": len(cited),
        "coverage": round(len(cited) / total_messages, 4) if total_messages else None,
    }


def grounding(runner, judge, n=15, seed=0) -> dict:
    """Sample cited claims and ask `judge` whether the evidence supports each."""
    cl = claims(runner.store)
    sample = random.Random(seed).sample(cl, min(n, len(cl))) if cl else []
    results = []
    for c in sample:
        seen, lines = set(), []
        for mid in c["cites"]:
            try:
                for m in runner.db.message(mid, context=1):     # ±1 for conversational context
                    if m.rowid not in seen:
                        seen.add(m.rowid)
                        mark = "»" if m.rowid in c["cites"] else " "
                        lines.append(f"{mark}#{m.rowid} {m.ts:%Y-%m-%d %H:%M} {m.sender}: {m.text}")
            except KeyError:
                lines.append(f"»#{mid} <unresolved>")
        user = JUDGE_TMPL.replace("{claim}", c["text"]).replace("{messages}", "\n".join(lines))
        try:
            v = judge.complete_json(JUDGE_SYS, user)
            supported = bool(v.get("supported"))
            reason = v.get("reason", "")
        except Exception as e:
            supported, reason = None, str(e)[:80]
        results.append({**c, "supported": supported, "reason": reason})
    ok = sum(1 for r in results if r["supported"])
    return {"sampled": len(results), "supported": ok,
            "rate": round(ok / len(results), 3) if results else None,
            "failures": [r for r in results if r["supported"] is False]}
