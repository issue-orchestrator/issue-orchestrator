"""Protocol for worktree setup policy decisions.

This module defines the interface for worktree preparation policy.
Implementations decide:
- Whether to reuse an existing worktree
- How to validate a worktree is in good state
- When to delete and recreate vs proceed

Principle: Reuse is an optimization, not a requirement.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass
class ValidationResult:
    """Result of worktree validation check."""

    can_reuse: bool
    reason: str


@dataclass
class SyncResult:
    """Result of remote ref sync."""

    success: bool
    reason: str = ""


class WorktreePolicy(Protocol):
    """Protocol for worktree setup policy decisions.

    Implementations of this protocol decide whether a worktree can be
    reused and handle cleanup when it cannot.

    The worktree creation code calls into the policy to make decisions,
    then acts on those decisions.
    """

    def validate_for_reuse(
        self,
        worktree_path: Path,
        expected_branch: str | None,
        repo_root: Path,
    ) -> ValidationResult:
        """Check if a worktree can be reused.

        Validates:
        1. Worktree path exists and is a valid git worktree
        2. Not in a broken git state (mid-rebase, mid-merge, conflicts)
        3. If expected_branch provided, current branch matches

        Args:
            worktree_path: Path to the worktree
            expected_branch: Branch we expect (or None to skip check)
            repo_root: Path to the main repository

        Returns:
            ValidationResult indicating whether worktree can be reused
        """
        ...

    def sync_remote_refs(
        self,
        worktree_path: Path,
        branch_name: str,
    ) -> SyncResult:
        """Sync local tracking refs with remote.

        Ensures --force-with-lease will work by updating local refs.
        If remote branch doesn't exist (first push), that's fine.

        Args:
            worktree_path: Path to the worktree
            branch_name: Branch to sync refs for

        Returns:
            SyncResult indicating success or failure
        """
        ...

    def delete_worktree(
        self,
        worktree_path: Path,
        repo_root: Path,
    ) -> bool:
        """Delete a worktree completely.

        Used when validation or preparation fails.

        Args:
            worktree_path: Path to the worktree to delete
            repo_root: Path to the main repository

        Returns:
            True if deletion succeeded
        """
        ...
