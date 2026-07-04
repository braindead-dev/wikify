"""Deploy a chat's rendered wiki into a git repository.

    python3 -m atlas sync my-chat --to /path/to/repo   # first time: remembers target
    python3 -m atlas sync my-chat                      # thereafter

The target repo becomes the hosting artifact (GitHub Pages, Cloudflare Pages,
any static host watching the repo). Sync renders the site fresh, mirrors it
into the repo root (removing files the wiki no longer produces, leaving the
repo's own files — .git, README, CNAME, dotfiles — alone), commits, and pushes
when a remote is configured. The target persists in `<chat_dir>/sync.json`.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from .render import render_site

KEEP = {".git", ".gitattributes", ".gitignore", "README.md", "CNAME", "LICENSE"}


def _run(args, cwd) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True)


def sync_site(chat_dir, to=None, render=True, verbose=True) -> Path:
    """Mirror the rendered site into the target repo and commit. Returns the
    target path."""
    chat_dir = Path(chat_dir)
    cfg_path = chat_dir / "sync.json"
    cfg = json.loads(cfg_path.read_text()) if cfg_path.exists() else {}
    target = Path(to).expanduser() if to else Path(cfg.get("repo", ""))
    if not str(target) or not (target / ".git").exists():
        raise SystemExit(f"no sync target — pass --to <git-repo-path> once "
                         f"(got: {target or 'nothing'})")
    if to and cfg.get("repo") != str(target):
        cfg["repo"] = str(target)
        cfg_path.write_text(json.dumps(cfg, indent=1))

    if render:
        render_site(chat_dir, verbose=verbose)
    site = chat_dir / "site"

    # mirror: copy everything from site/, delete repo files the site no longer
    # has — but never the repo's own files
    for item in target.iterdir():
        if item.name in KEEP or item.name.startswith("."):
            continue
        rel = item.name
        if not (site / rel).exists():
            shutil.rmtree(item) if item.is_dir() else item.unlink()
    shutil.copytree(site, target, dirs_exist_ok=True)
    (target / ".nojekyll").touch()

    if not _run(["git", "status", "--porcelain"], target).stdout.strip():
        if verbose:
            print(f"[sync] {target} already up to date")
        return target
    _run(["git", "add", "-A"], target)
    pages = len(list(site.rglob("*.html")))
    r = _run(["git", "commit", "-q", "-m", f"wiki sync: {pages} pages"], target)
    if r.returncode != 0:
        raise SystemExit(f"[sync] commit failed: {r.stderr.strip()}")
    pushed = ""
    if _run(["git", "remote"], target).stdout.strip():
        p = _run(["git", "push", "-q"], target)
        pushed = " · pushed" if p.returncode == 0 else f" · PUSH FAILED: {p.stderr.strip()[:80]}"
    if verbose:
        print(f"[sync] {pages} pages → {target}{pushed}", flush=True)
    return target
