"""Shared validation timing artifact helpers."""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path


def resolve_git_dir(worktree: Path) -> Path | None:
    """Resolve the git dir for the current worktree without shelling out."""
    dot_git = worktree / ".git"
    if dot_git.is_dir():
        return dot_git
    if not dot_git.is_file():
        return None
    content = dot_git.read_text(encoding="utf-8").strip()
    prefix = "gitdir: "
    if not content.startswith(prefix):
        return None
    git_dir = Path(content[len(prefix) :].strip())
    if not git_dir.is_absolute():
        git_dir = (worktree / git_dir).resolve()
    return git_dir


def resolve_git_common_dir(worktree: Path) -> Path | None:
    """Resolve the repository's shared git dir for cross-worktree artifacts."""
    git_dir = resolve_git_dir(worktree)
    if git_dir is None:
        return None
    commondir_file = git_dir / "commondir"
    if not commondir_file.exists():
        return git_dir
    common_dir = Path(commondir_file.read_text(encoding="utf-8").strip())
    if not common_dir.is_absolute():
        common_dir = (git_dir / common_dir).resolve()
    return common_dir


def read_head_ref_name(git_dir: Path) -> str | None:
    """Read the current branch name from HEAD when it points at a ref."""
    head_file = git_dir / "HEAD"
    if not head_file.exists():
        return None
    head = head_file.read_text(encoding="utf-8").strip()
    prefix = "ref: refs/heads/"
    if not head.startswith(prefix):
        return None
    return head[len(prefix) :]


def read_branch_name(worktree: Path) -> str | None:
    """Best-effort branch name for diagnostics records without git subprocesses."""
    git_dir = resolve_git_dir(worktree)
    if git_dir is None:
        return None
    branch = read_head_ref_name(git_dir)
    if branch:
        return branch
    common_dir = resolve_git_common_dir(worktree)
    if common_dir is not None:
        return read_head_ref_name(common_dir)
    return None


def current_branch_name(worktree: Path) -> str | None:
    """Best-effort branch name for diagnostics records."""
    return read_branch_name(worktree)


def get_shared_timings_file(worktree: Path) -> Path | None:
    """Return the shared JSONL timing file path for this repository."""
    common_dir = resolve_git_common_dir(worktree)
    if common_dir is None:
        return None
    return common_dir / "issue-orchestrator" / "validate-timings.jsonl"


def append_jsonl(path: Path | None, record: dict[str, object]) -> None:
    """Append one JSON object to a JSONL file, creating parents as needed."""
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    line = (json.dumps(record, sort_keys=True) + "\n").encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        # Use one O_APPEND write so concurrent validation gates cannot interleave
        # JSONL fragments on local POSIX filesystems such as macOS APFS.
        written = os.write(fd, line)
        if written != len(line):
            raise OSError(f"short JSONL write to {path}: {written} of {len(line)}")
    finally:
        os.close(fd)


def build_timing_envelope(
    *,
    wall_started_at: datetime,
    monotonic_started_at: float,
) -> dict[str, object]:
    """Return common elapsed-time fields for validation timing records."""
    wall_ended_at = datetime.now(timezone.utc)
    return {
        "monotonic_elapsed_seconds": round(time.monotonic() - monotonic_started_at, 3),
        "wall_started_at": wall_started_at.isoformat(),
        "wall_ended_at": wall_ended_at.isoformat(),
        "wall_elapsed_seconds": round(
            (wall_ended_at - wall_started_at).total_seconds(), 3
        ),
    }


def append_validation_timing(worktree: Path, record: dict[str, object]) -> None:
    """Append one validation timing record with shared worktree context."""
    payload: dict[str, object] = {
        "worktree": str(worktree),
        "branch": current_branch_name(worktree),
        "recorded_at": datetime.now(timezone.utc).isoformat(),
    }
    payload.update(record)
    append_jsonl(get_shared_timings_file(worktree), payload)
