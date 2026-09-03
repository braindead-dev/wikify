import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from atlas.render import _inline, _Page
from atlas.sync import MANIFEST, _mirror_site, sync_site


class RenderSafetyTests(unittest.TestCase):
    def render(self, text):
        return _inline(text, _Page("", {}), {})

    def test_dangerous_link_schemes_are_not_linked(self):
        for value in ("javascript:alert(1)", "javascript&#58;alert(1)",
                      "data:text/html,hello", "//example.com/path"):
            with self.subTest(value=value):
                rendered = self.render(f"[label]({value})")
                self.assertNotIn("<a ", rendered)
                self.assertNotIn("javascript", rendered.lower())

    def test_link_attribute_is_quote_escaped(self):
        rendered = self.render('[hover](path" onmouseover="alert(1))')
        self.assertIn("&quot;", rendered)
        self.assertNotIn('" onmouseover="', rendered)

    def test_safe_links_still_render(self):
        rendered = self.render("[docs](https://example.com/a.md?x=1&y=2)")
        self.assertIn('href="https://example.com/a.html?x=1&amp;y=2"', rendered)


class SyncSafetyTests(unittest.TestCase):
    def run_git(self, target, *args):
        return subprocess.run(
            ["git", *args], cwd=target, capture_output=True, text=True, check=True
        )

    def test_legacy_sync_removes_nested_stale_pages_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            site, target = root / "site", root / "target"
            (site / "person").mkdir(parents=True)
            (target / "person").mkdir(parents=True)
            (target / "private-notes").mkdir()
            (site / "index.html").write_text("new index")
            (site / "person" / "current.html").write_text("new page")
            (target / "person" / "current.html").write_text("old page")
            (target / "person" / "stale.html").write_text("stale page")
            (target / "private-notes" / "keep.txt").write_text("local data")
            (target / "README.md").write_text("repo docs")

            copied, removed = _mirror_site(site, target)

            self.assertEqual((copied, removed), (2, 1))
            self.assertEqual((target / "person" / "current.html").read_text(), "new page")
            self.assertFalse((target / "person" / "stale.html").exists())
            self.assertEqual((target / "private-notes" / "keep.txt").read_text(), "local data")
            self.assertEqual((target / "README.md").read_text(), "repo docs")

    def test_manifest_removes_an_entire_retired_generated_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            site, target = root / "site", root / "target"
            (site / "event").mkdir(parents=True)
            target.mkdir()
            (site / "event" / "old.html").write_text("old")
            _mirror_site(site, target)

            (site / "event" / "old.html").unlink()
            (site / "event").rmdir()
            (site / "index.html").write_text("new")
            _, removed = _mirror_site(site, target)

            self.assertEqual(removed, 1)
            self.assertFalse((target / "event").exists())
            self.assertTrue((target / "index.html").exists())

    def test_untrusted_manifest_cannot_escape_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            site, target = root / "site", root / "target"
            site.mkdir()
            target.mkdir()
            outside = root / "outside.txt"
            outside.write_text("keep")
            (site / "index.html").write_text("index")
            (target / MANIFEST).write_text(json.dumps(
                {"files": ["../outside.txt", ".git/config", "README.md"]}))

            _mirror_site(site, target)

            self.assertEqual(outside.read_text(), "keep")

    def test_sync_refuses_a_dirty_target_before_changing_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            chat, target = root / "chat", root / "target"
            (chat / "site").mkdir(parents=True)
            (chat / "site" / "index.html").write_text("new site")
            target.mkdir()
            self.run_git(target, "init", "-q")
            (target / "private-notes.txt").write_text("do not publish")

            with self.assertRaisesRegex(SystemExit, "uncommitted files"):
                sync_site(chat, to=target, render=False, verbose=False)

            self.assertEqual((target / "private-notes.txt").read_text(), "do not publish")
            self.assertFalse((target / "index.html").exists())
            self.assertFalse((target / MANIFEST).exists())


if __name__ == "__main__":
    unittest.main()
