"""Small read-only wrappers around the ``git`` command line."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Iterable


class GitError(RuntimeError):
    """Raised when a git command fails (not a repository, unknown ref, git missing)."""


def _git(repo_root: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), *args],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        raise GitError(f"could not run git: {exc}") from exc
    if result.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def head_commit(repo_root: Path) -> str:
    """Full hash of the commit currently checked out."""
    return _git(repo_root, "rev-parse", "--verify", "HEAD^{commit}").strip()


def top_level(repo_root: Path) -> Path:
    """Top-level directory of the repository containing ``repo_root``."""
    return Path(_git(repo_root, "rev-parse", "--show-toplevel").strip())


def tag_commit(repo_root: Path, tag: str) -> str:
    """Full hash of the commit a tag points to (annotated or lightweight)."""
    return _git(repo_root, "rev-parse", "--verify", f"refs/tags/{tag}^{{commit}}").strip()


def dirty_paths(repo_root: Path, *, ignore: Iterable[str] = ()) -> list[str]:
    """Paths with uncommitted changes or untracked files, minus ``ignore``.

    ``ignore`` holds repository-relative POSIX paths. An empty result means the
    working tree is clean.
    """
    ignored = set(ignore)
    # -z: NUL-separated, paths unquoted. Each entry is "XY <path>"; a rename is
    # followed by an extra entry holding the original path.
    entries = _git(repo_root, "status", "--porcelain", "-z", "--untracked-files=all").split("\0")
    paths: list[str] = []
    skip_next = False
    for entry in entries:
        if skip_next or not entry:
            skip_next = False
            continue
        status, path = entry[:2], entry[3:]
        skip_next = "R" in status or "C" in status
        if path not in ignored:
            paths.append(path)
    return paths
