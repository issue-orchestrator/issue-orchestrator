"""Maintenance operations for issue cleanup and reset.

This module provides the IssueResetter class for performing "nuclear reset"
operations on issues - cleaning up all local and remote state to allow a
fresh retry.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..infra.config import Config
    from ..ports.worktree_manager import WorktreeManager
    from ..ports.working_copy import WorkingCopy
    from ..domain.models import SessionHistoryEntry
    from .action_applier import ActionApplier

from .actions import RemoveLabelAction
from .worktree_manager import get_worktree_path

logger = logging.getLogger(__name__)


def _find_issue_branch(
    working_copy: "WorkingCopy",
    repo_root: Path,
    issue_number: int,
) -> str | None:
    """Find the remote branch for an issue number.

    Looks for branches that start with the issue number (e.g., "3767-fix-something").

    Args:
        working_copy: Working copy adapter for git operations.
        repo_root: Path to the repository root.
        issue_number: The issue number to find a branch for.

    Returns:
        The branch name (without remote prefix) or None if not found.
    """
    branches = working_copy.list_remote_branches(repo_root)
    for raw in branches:
        branch = raw.strip()
        if branch.startswith("origin/"):
            branch = branch[len("origin/"):]
        if branch and branch[0].isdigit():
            parts = branch.split("-", 1)
            if parts[0].isdigit() and int(parts[0]) == issue_number:
                return branch
    return None


@dataclass
class ResetResult:
    """Result of an issue reset operation."""

    success: bool
    issue_number: int
    deleted_worktree: str | None = None
    deleted_branch: str | None = None
    labels_removed: list[str] | None = None
    error: str | None = None


def reset_issue(
    issue_number: int,
    config: "Config",
    worktree_manager: "WorktreeManager",
    working_copy: "WorkingCopy",
    action_applier: "ActionApplier",
    blocking_labels: list[str],
    session_history: list["SessionHistoryEntry"],
    completed_today: list[int],
) -> ResetResult:
    """Reset an issue to pristine state for fresh retry.

    This "nuclear option" cleans up all local and remote state:
    1. Deletes the local worktree
    2. Deletes the remote branch
    3. Removes blocking labels
    4. Removes from session history

    Args:
        issue_number: The issue to reset
        config: Orchestrator configuration
        worktree_manager: Manager for worktree lifecycle operations
        working_copy: Working copy adapter for git operations
        action_applier: For applying label changes
        blocking_labels: Blocking labels to remove from the issue
        session_history: Session history list (will be mutated)
        completed_today: Completed today list (will be mutated)

    Returns:
        ResetResult with details of what was cleaned up
    """
    deleted_worktree: str | None = None
    deleted_branch: str | None = None
    labels_removed: list[str] = []

    try:
        # 1. Delete local worktree
        worktree_path = get_worktree_path(config, issue_number)
        if worktree_path.exists():
            try:
                worktree_manager.remove(worktree_path)
                deleted_worktree = str(worktree_path)
                logger.info("[reset] Deleted worktree: %s", worktree_path)
            except Exception as e:
                logger.warning("[reset] Failed to delete worktree %s: %s", worktree_path, e)

        # 2. Delete remote branch
        # Find the branch for this issue from remote branches
        branch_name = _find_issue_branch(working_copy, config.repo_root, issue_number)
        if branch_name:
            try:
                working_copy.delete_remote_branch(config.repo_root, branch_name)
                deleted_branch = branch_name
                logger.info("[reset] Deleted remote branch: %s", branch_name)
            except Exception as e:
                logger.warning("[reset] Failed to delete remote branch %s: %s", branch_name, e)

        # 3. Remove blocking labels
        for label in blocking_labels:
            action = RemoveLabelAction(
                issue_number=issue_number,
                label=label,
                reason="reset via web",
            )
            result = action_applier.apply(action)
            if result.success:
                labels_removed.append(label)
                logger.info("[reset] Removed label '%s' from issue #%d", label, issue_number)
            else:
                logger.warning(
                    "[reset] Failed to remove label '%s' from #%d: %s",
                    label, issue_number, result.error or "unknown error"
                )

        # 4. Remove from session history
        # Note: We mutate the list in place since it's passed by reference
        session_history[:] = [
            entry for entry in session_history
            if entry.issue_number != issue_number
        ]
        if issue_number in completed_today:
            completed_today.remove(issue_number)

        logger.info(
            "[reset] Issue #%d reset complete: worktree=%s branch=%s labels=%s",
            issue_number,
            deleted_worktree or "(none)",
            deleted_branch or "(none)",
            labels_removed or "(none)",
        )

        return ResetResult(
            success=True,
            issue_number=issue_number,
            deleted_worktree=deleted_worktree,
            deleted_branch=deleted_branch,
            labels_removed=labels_removed,
        )

    except Exception as e:
        logger.error("[reset] Failed to reset issue #%d: %s", issue_number, e)
        return ResetResult(
            success=False,
            issue_number=issue_number,
            deleted_worktree=deleted_worktree,
            deleted_branch=deleted_branch,
            labels_removed=labels_removed,
            error=str(e),
        )
