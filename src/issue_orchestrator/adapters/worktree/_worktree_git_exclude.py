"""Owner of io's writes to a repository's shared git ``info/exclude``.

Every worktree of a repository shares one ``info/exclude`` in the common git
dir. Worktree setup appends runtime entries to it, and legacy-drop retirement
removes io's old ``cli_tools`` lines from it, possibly for two worktrees at
once. Every read-modify-write therefore happens under one repository-scoped
lock, and rewrites are atomic, so concurrent setups never lose each other's
entries.
"""

from __future__ import annotations

import fcntl
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from ...infra.atomic_io import atomic_write_bytes

__all__ = [
    "EXCLUDE_LOCK_PATH",
    "append_worktree_exclude_entries",
    "common_dir_of",
    "exclude_lock",
    "remove_exclude_lines",
    "worktree_git_common_dir",
    "worktree_git_dir",
]

#: Under the git COMMON dir, so every worktree of a repository takes one lock.
EXCLUDE_LOCK_PATH = Path("issue-orchestrator") / "info-exclude.lock"


def worktree_git_dir(worktree_path: Path) -> Path | None:
    """Return the git dir a linked worktree's ``.git`` file points at."""
    git_file = worktree_path / ".git"
    if not git_file.is_file():
        return None
    content = git_file.read_text().strip()
    if not content.startswith("gitdir:"):
        return None
    git_dir = Path(content.split(":", 1)[1].strip())
    if not git_dir.is_absolute():
        # ``worktree.useRelativePaths`` links are relative to the worktree.
        git_dir = (worktree_path / git_dir).resolve()
    return git_dir


def worktree_git_common_dir(worktree_path: Path) -> Path | None:
    """Return the repository's common git dir for a linked worktree."""
    git_dir = worktree_git_dir(worktree_path)
    if git_dir is None:
        return None
    return common_dir_of(git_dir)


def common_dir_of(git_dir: Path) -> Path:
    """Return the common git dir a (possibly linked) git dir shares."""
    commondir_file = git_dir / "commondir"
    if not commondir_file.exists():
        return git_dir
    common_dir = Path(commondir_file.read_text().strip())
    if not common_dir.is_absolute():
        common_dir = (git_dir / common_dir).resolve()
    return common_dir


@contextmanager
def exclude_lock(common_dir: Path) -> Iterator[None]:
    """Hold the repository-scoped lock for ``info/exclude`` read-modify-writes.

    ``flock`` is per open file description, so two threads of one process
    serialise exactly like two processes. Callers must not nest it.
    """
    lock_path = common_dir / EXCLUDE_LOCK_PATH
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _append_exclude_entries(exclude_path: Path, paths: list[Path]) -> None:
    exclude_path.parent.mkdir(parents=True, exist_ok=True)
    existing_lines: list[str] = []
    existing_text = ""
    if exclude_path.exists():
        existing_text = exclude_path.read_text()
        existing_lines = existing_text.splitlines()
    existing = {line.strip() for line in existing_lines if line.strip()}
    missing = [
        str(path).replace("\\", "/")
        for path in paths
        if str(path).replace("\\", "/") not in existing
    ]
    if not missing:
        return
    suffix = "\n" if existing_lines and not existing_text.endswith("\n") else ""
    with exclude_path.open("a", encoding="utf-8") as handle:
        if suffix:
            handle.write(suffix)
        for entry in missing:
            handle.write(f"{entry}\n")


def append_worktree_exclude_entries(worktree_path: Path, paths: list[Path]) -> None:
    """Add ``paths`` to the worktree's and the repository's ``info/exclude``."""
    git_dir = worktree_git_dir(worktree_path)
    if git_dir is None:
        return
    common_dir = common_dir_of(git_dir)
    exclude_paths = [git_dir / "info" / "exclude"]
    if common_dir != git_dir:
        exclude_paths.append(common_dir / "info" / "exclude")
    with exclude_lock(common_dir):
        for exclude_path in exclude_paths:
            _append_exclude_entries(exclude_path, paths)


def remove_exclude_lines(exclude_path: Path, matches: Callable[[str], bool]) -> None:
    """Atomically drop every line ``matches`` selects. Caller holds the lock."""
    if not exclude_path.exists():
        return
    lines = exclude_path.read_text(encoding="utf-8").splitlines(keepends=True)
    kept = [line for line in lines if not matches(line.strip())]
    if len(kept) != len(lines):
        atomic_write_bytes(exclude_path, "".join(kept).encode("utf-8"))
