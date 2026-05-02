"""Sibling reviewer worktree management for persistent-session review exchange.

Each persistent-session review exchange uses a separate reviewer worktree
in detached-HEAD on the coder's branch tip. This sidesteps Claude Code's
project-level lock (which prevents two Claude sessions in the same project
root) and is provider-agnostic — it works for any coder/reviewer pair.

Lifecycle:
- ``create_reviewer_worktree`` at exchange start (detached HEAD on coder tip).
- ``fast_forward_reviewer_worktree`` before each reviewer round so the
  reviewer always sees the latest committed state of the coder's branch.
- ``remove_reviewer_worktree`` at exchange end.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReviewerWorktree:
    """A short-lived reviewer worktree for one review-exchange run."""

    path: Path
    coder_branch: str


class ReviewerWorktreeError(RuntimeError):
    """Raised when reviewer-worktree management fails."""


def create_reviewer_worktree(
    *,
    coder_worktree: Path,
    coder_branch: str,
    timestamp: str,
) -> ReviewerWorktree:
    """Create a sibling reviewer worktree in detached HEAD on the coder's branch tip.

    The sibling lives at ``<coder_worktree>-review-<timestamp>``. Detached
    HEAD is required because the coder's branch is already checked out in
    the coder worktree; git refuses to check out the same branch twice.
    """
    sibling = coder_worktree.parent / f"{coder_worktree.name}-review-{timestamp}"
    if sibling.exists():
        raise ReviewerWorktreeError(
            f"Reviewer worktree path already exists: {sibling}"
        )

    repo_root = _resolve_repo_root(coder_worktree)
    tip_sha = _resolve_branch_tip(repo_root, coder_branch)
    _git(repo_root, ["worktree", "add", "--detach", str(sibling), tip_sha])
    logger.info(
        "Created reviewer worktree path=%s coder_branch=%s tip=%s",
        sibling,
        coder_branch,
        tip_sha,
    )
    return ReviewerWorktree(path=sibling, coder_branch=coder_branch)


def fast_forward_reviewer_worktree(reviewer: ReviewerWorktree) -> str:
    """Fast-forward the reviewer worktree to the current tip of the coder's branch.

    Returns the SHA the worktree now points at. Always uses detached HEAD so
    we never conflict with the coder's branch checkout.
    """
    repo_root = _resolve_repo_root(reviewer.path)
    tip_sha = _resolve_branch_tip(repo_root, reviewer.coder_branch)
    _git(reviewer.path, ["checkout", "--detach", tip_sha])
    logger.debug(
        "Fast-forwarded reviewer worktree path=%s tip=%s",
        reviewer.path,
        tip_sha,
    )
    return tip_sha


def remove_reviewer_worktree(
    reviewer: ReviewerWorktree, *, force: bool = False,
) -> None:
    """Remove the reviewer worktree at exchange end.

    With ``force=True`` we tolerate failure (use it when the orchestrator is
    cleaning up after a crash); without it we raise so the caller can surface
    a real problem.
    """
    if not reviewer.path.exists():
        return
    repo_root = _resolve_repo_root(reviewer.path)
    args = ["worktree", "remove", str(reviewer.path)]
    if force:
        args.append("--force")
    try:
        _git(repo_root, args)
    except subprocess.CalledProcessError as exc:
        if force:
            logger.warning(
                "git worktree remove --force failed for %s: %s",
                reviewer.path,
                exc,
            )
            return
        raise ReviewerWorktreeError(
            f"Failed to remove reviewer worktree {reviewer.path}: {exc}"
        ) from exc


def _resolve_repo_root(worktree_path: Path) -> Path:
    result = subprocess.run(
        ["git", "rev-parse", "--git-common-dir"],
        cwd=worktree_path,
        capture_output=True,
        text=True,
        check=True,
    )
    common_dir = Path(result.stdout.strip())
    if not common_dir.is_absolute():
        common_dir = (worktree_path / common_dir).resolve()
    return common_dir.parent


def _resolve_branch_tip(repo_root: Path, branch: str) -> str:
    result = subprocess.run(
        ["git", "rev-parse", branch],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _git(cwd: Path, args: list[str]) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)
