"""Deploy a chat's rendered wiki into a git repository.

    python3 -m atlas sync my-chat --to /path/to/repo   # first time: remembers target
    python3 -m atlas sync my-chat                      # thereafter

The target repo becomes the hosting artifact (GitHub Pages, Cloudflare Pages,
Vercel, or any static host watching the repo). Sync renders the site fresh,
mirrors its files into the repo root, removes stale files from prior syncs, and
leaves unrelated repository files alone. It then commits and pushes when a
remote is configured. The target persists in `<chat_dir>/sync.json`.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from .render import render_site

KEEP = {".git", ".gitattributes", ".gitignore", "README.md", "CNAME", "LICENSE"}
MANIFEST = ".atlas-sync-manifest.json"


def _run(args, cwd) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=False)


def _files(root: Path) -> set[Path]:
    """Regular files below root, represented as safe relative paths."""
    paths = list(root.rglob("*"))
    if any(path.is_symlink() for path in paths):
        raise SystemExit("rendered site must not contain symlinks")
    return {path.relative_to(root) for path in paths if path.is_file()}


def _safe_rel(raw) -> Path | None:
    """Treat the on-disk manifest as untrusted; never let it escape target."""
    path = Path(str(raw))
    if (path.is_absolute() or not path.parts or ".." in path.parts
            or path.parts[0] in KEEP or path.parts[0].startswith(".")):
        return None
    return path


def _target_path(target: Path, rel: Path) -> Path:
    """Resolve parent symlinks and prove a managed path stays under target."""
    path = target / rel
    parent = path.parent.resolve()
    if parent != target and target not in parent.parents:
        raise SystemExit(f"sync path escapes target through a symlink: {rel}")
    return path


def _previous_files(target: Path, current: set[Path]) -> set[Path]:
    """Load files managed by the prior sync.

    Older deployments predate the manifest. For that one-time migration, files
    beneath roots produced by the current site are considered managed. This
    catches stale nested pages without claiming unrelated target directories.
    """
    manifest = target / MANIFEST
    if manifest.exists():
        try:
            payload = json.loads(manifest.read_text())
        except (OSError, ValueError, TypeError):
            payload = {}
        raw = payload.get("files", []) if isinstance(payload, dict) else []
        if not isinstance(raw, list):
            raw = []
        return {path for item in raw if (path := _safe_rel(item)) is not None}

    previous = set()
    for root in {path.parts[0] for path in current}:
        existing = target / root
        if existing.is_file() or existing.is_symlink():
            previous.add(Path(root))
        elif existing.is_dir():
            previous.update(path.relative_to(target) for path in existing.rglob("*")
                            if path.is_file() or path.is_symlink())
    return previous


def _mirror_site(site: Path, target: Path) -> tuple[int, int]:
    """Copy site files and remove only files owned by earlier syncs.

    Returns `(copied, removed)`. Directories are pruned only when empty; files
    outside the managed manifest are never removed.
    """
    site, target = site.resolve(), target.resolve()
    if not site.is_dir():
        raise SystemExit(f"rendered site not found: {site}")
    if site == target or site in target.parents or target in site.parents:
        raise SystemExit("site and sync target must be separate directories")

    current = _files(site)
    protected = [path for path in current if _safe_rel(path) is None]
    if protected:
        raise SystemExit(f"rendered site contains protected path: {protected[0]}")
    previous = _previous_files(target, current)
    removed = 0
    prune = set()
    for rel in sorted(previous - current, key=lambda p: len(p.parts), reverse=True):
        path = _target_path(target, rel)
        if path.is_file() or path.is_symlink():
            path.unlink()
            removed += 1
            parent = path.parent
            while parent != target:
                prune.add(parent)
                parent = parent.parent

    # Remove only empty directories left by stale managed pages, deepest first.
    for path in sorted(prune, key=lambda p: len(p.parts), reverse=True):
        rel = path.relative_to(target)
        if rel.parts[0] in KEEP or rel.parts[0].startswith("."):
            continue
        try:
            path.rmdir()
        except OSError:
            pass

    for rel in sorted(current):
        src, dest = site / rel, _target_path(target, rel)
        if dest.exists() and dest.is_dir():
            raise SystemExit(f"sync target has a directory where a file belongs: {dest}")
        if dest.is_symlink():
            dest.unlink()
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)

    (target / MANIFEST).write_text(json.dumps(
        {"version": 1, "files": [path.as_posix() for path in sorted(current)]},
        indent=2,
    ) + "\n")
    return len(current), removed


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
    status = _run(["git", "status", "--porcelain"], target)
    if status.returncode != 0:
        raise SystemExit(f"[sync] cannot inspect target: {status.stderr.strip()}")
    if status.stdout.strip():
        raise SystemExit("[sync] target has uncommitted files — commit or stash them "
                         "before syncing (nothing was changed)")
    if to and cfg.get("repo") != str(target):
        cfg["repo"] = str(target)
        cfg_path.write_text(json.dumps(cfg, indent=1))

    if render:
        render_site(chat_dir, verbose=verbose)
    site = chat_dir / "site"
    _, removed = _mirror_site(site, target)
    (target / ".nojekyll").touch()

    if not _run(["git", "status", "--porcelain"], target).stdout.strip():
        if verbose:
            print(f"[sync] {target} already up to date")
        return target
    added = _run(["git", "add", "-A"], target)
    if added.returncode != 0:
        raise SystemExit(f"[sync] git add failed: {added.stderr.strip()}")
    pages = len(list(site.rglob("*.html")))
    r = _run(["git", "commit", "-q", "-m", f"wiki sync: {pages} pages"], target)
    if r.returncode != 0:
        raise SystemExit(f"[sync] commit failed: {r.stderr.strip()}")
    pushed = ""
    if _run(["git", "remote"], target).stdout.strip():
        p = _run(["git", "push", "-q"], target)
        pushed = " · pushed" if p.returncode == 0 else f" · PUSH FAILED: {p.stderr.strip()[:80]}"
    if verbose:
        stale = f" · removed {removed} stale files" if removed else ""
        print(f"[sync] {pages} pages → {target}{stale}{pushed}", flush=True)
    return target
