"""The KB store — the only thing that reads/writes pages on disk (L2).

A page's `id` *is* its path under the kb dir: `person/alice` -> `person/alice.md`,
so folders are the page types (`person/`, `event/`, `topic/`, …) and the home
page is `index`. The store knows nothing about LLMs; it enforces the on-disk
shape and nothing more.
"""
from __future__ import annotations

from pathlib import Path

from .page import Page

_ID_OK = set("abcdefghijklmnopqrstuvwxyz0123456789-/")


def slugify(text: str) -> str:
    import re
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s or "untitled"


def valid_id(page_id: str) -> bool:
    """A page id is `type/slug` (or the bare `index`), lowercase, slug-safe."""
    if page_id == "index":
        return True
    if page_id.count("/") != 1:
        return False
    return bool(page_id) and set(page_id) <= _ID_OK and "//" not in page_id


class Store:
    def __init__(self, kb_dir):
        self.root = Path(kb_dir)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, page_id: str) -> Path:
        return self.root / f"{page_id}.md"

    def exists(self, page_id: str) -> bool:
        return self._path(page_id).exists()

    def read(self, page_id: str):
        p = self._path(page_id)
        return Page.from_markdown(p.read_text()) if p.exists() else None

    def write(self, page: Page):
        if not valid_id(page.id):
            raise ValueError(f"invalid page id: {page.id!r}")
        p = self._path(page.id)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(page.to_markdown())

    def delete(self, page_id: str):
        self._path(page_id).unlink(missing_ok=True)

    def all_ids(self) -> list:
        return sorted(
            str(p.relative_to(self.root)).removesuffix(".md")
            for p in self.root.rglob("*.md")
        )

    def all_pages(self):
        for pid in self.all_ids():
            page = self.read(pid)
            if page is not None:
                yield page

    def by_type(self, type_: str) -> list:
        return [p for p in self.all_pages() if p.type == type_]
