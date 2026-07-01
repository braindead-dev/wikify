"""Reducer (L3) integration tests with the deterministic MockClient — no API key.

Proves the propose→validate→apply→self-correct loop without a live model, so the
core control flow is covered in CI.
"""
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wiki.llm import MockClient
from wiki.reduce import Reducer
from wiki.store import Page, Store


def _msg(rowid, sender, text, ts=None):
    return SimpleNamespace(rowid=rowid, sender=sender, text=text,
                           ts=ts or datetime(2025, 1, 1, 12, 0), system=None,
                           attachments=[], reply_to=None, edited=False, reactions={})


def _seeded_store():
    s = Store(tempfile.mkdtemp())
    s.write(Page(id="person/alice", type="person", title="Alice", pinned=True))
    return s


def test_reducer_applies_edits():
    s = _seeded_store()
    llm = MockClient({"edits": [{"op": "append", "page": "person/alice", "text": "Likes tea [#1]."}]})
    r = Reducer(s, llm, "Test", {1}.__contains__)
    cs = r.reduce_chunk([_msg(1, "Alice", "i like tea")])
    assert "tea" in s.read("person/alice").body
    assert s.read("person/alice").sources == [1]
    assert cs.summary() != "no changes"


def test_reducer_retries_on_bad_citation():
    s = _seeded_store()
    calls = {"n": 0}

    def responder(system, user):
        calls["n"] += 1
        bad = calls["n"] == 1
        mid = 999 if bad else 1
        return {"edits": [{"op": "append", "page": "person/alice", "text": f"Fact [#{mid}]."}]}

    r = Reducer(s, MockClient(responder), "Test", {1}.__contains__)
    r.reduce_chunk([_msg(1, "Alice", "hi")])
    assert calls["n"] == 2, "should retry once after the bad-citation rejection"
    assert "Fact" in s.read("person/alice").body
    assert s.read("person/alice").sources == [1]


def test_reducer_empty_edits_is_noop():
    s = _seeded_store()
    before = s.read("person/alice").body
    r = Reducer(s, MockClient({"edits": []}), "Test", {1}.__contains__)
    r.reduce_chunk([_msg(1, "Alice", "lol")])
    assert s.read("person/alice").body == before


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  ✓ {t.__name__}")
    print(f"\n{len(tests)} tests passed")


if __name__ == "__main__":
    main()
