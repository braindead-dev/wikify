"""Unit tests for the KB store (L2) — the pure, LLM-free core.

Run: python3 tests/test_store.py   (or under pytest)
Every test uses a temp dir and a fixed set of "resolvable" message ids.
"""
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wiki.store import Page, Store, apply_edits, EditError, backlinks, verify

REAL = {1, 2, 3, 4}
resolves = REAL.__contains__


def _store():
    return Store(tempfile.mkdtemp())


def test_page_roundtrip():
    p = Page(id="person/alice", type="person", title="Alice", aliases=["ali"],
             pinned=True, body="Drives a car [#1]. Links [[event/x]].")
    p2 = Page.from_markdown(p.to_markdown())
    assert p2.id == "person/alice" and p2.title == "Alice"
    assert p2.aliases == ["ali"] and p2.pinned is True
    assert p2.sources == [1] and p2.links == ["event/x"]


def test_create_and_append():
    s = _store()
    apply_edits(s, [
        {"op": "create_page", "id": "person/alice", "type": "person", "title": "Alice"},
        {"op": "append", "page": "person/alice", "text": "Likes tea [#1]."},
    ], resolves)
    assert s.read("person/alice").sources == [1]


def test_atomic_reject_on_bad_citation():
    s = _store()
    apply_edits(s, [{"op": "create_page", "id": "person/bob", "type": "person", "title": "Bob"}], resolves)
    before = s.read("person/bob").body
    try:
        apply_edits(s, [
            {"op": "append", "page": "person/bob", "text": "Good [#2]."},
            {"op": "append", "page": "person/bob", "text": "Bad [#999]."},
        ], resolves)
        assert False, "should have raised EditError"
    except EditError:
        pass
    assert s.read("person/bob").body == before, "partial write — atomicity broken"


def test_append_idempotent():
    s = _store()
    apply_edits(s, [{"op": "create_page", "id": "person/c", "type": "person", "title": "C"}], resolves)
    for _ in range(3):
        apply_edits(s, [{"op": "append", "page": "person/c", "text": "Fact [#1]."}], resolves)
    assert s.read("person/c").body.count("Fact") == 1


def test_create_duplicate_rejected():
    s = _store()
    apply_edits(s, [{"op": "create_page", "id": "person/d", "type": "person", "title": "D"}], resolves)
    try:
        apply_edits(s, [{"op": "create_page", "id": "person/d", "type": "person", "title": "D"}], resolves)
        assert False
    except EditError:
        pass


def test_dangling_link_rejected():
    s = _store()
    apply_edits(s, [{"op": "create_page", "id": "person/e", "type": "person", "title": "E"}], resolves)
    try:
        apply_edits(s, [{"op": "link", "from": "person/e", "to": "event/nope"}], resolves)
        assert False
    except EditError:
        pass


def test_merge_tombstone_and_backlinks():
    s = _store()
    apply_edits(s, [
        {"op": "create_page", "id": "person/a", "type": "person", "title": "A"},
        {"op": "create_page", "id": "person/b", "type": "person", "title": "B"},
        {"op": "append", "page": "person/b", "text": "Coffee [#3]."},
    ], resolves)
    apply_edits(s, [{"op": "merge", "from": "person/b", "into": "person/a"}], resolves)
    assert "Coffee" in s.read("person/a").body
    assert "Merged into" in s.read("person/b").body
    assert backlinks(s).get("person/a") == ["person/b"]


def test_related_pinned_to_bottom():
    s = _store()
    apply_edits(s, [
        {"op": "create_page", "id": "event/x", "type": "event", "title": "X"},
        {"op": "create_page", "id": "person/a", "type": "person", "title": "A", "body": "Lead [#1]."},
        {"op": "link", "from": "person/a", "to": "event/x"},
        {"op": "append", "page": "person/a", "text": "Later fact [#2]."},
    ], resolves)
    body = s.read("person/a").body
    assert body.index("Lead") < body.index("Later fact") < body.index("## Related")


def test_verify_clean_and_dirty():
    s = _store()
    apply_edits(s, [
        {"op": "create_page", "id": "person/a", "type": "person", "title": "A", "body": "Fact [#1]."},
    ], resolves)
    assert verify(s, resolves) == []
    # a citation that no longer resolves is caught
    assert verify(s, {2, 3}.__contains__), "should report the now-unresolvable #1"


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  ✓ {t.__name__}")
    print(f"\n{len(tests)} tests passed")


if __name__ == "__main__":
    main()
