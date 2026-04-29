"""WorktreeManager port for worktree lifecycle operations.

This module defines the protocol (interface) for worktree management:
- Create worktrees for issues
- Remove worktrees after completion
- Extract issue numbers from branch names

Distinct from WorkingCopy which handles operations *inside* a worktree.
WorktreeManager handles the worktree lifecycle itself.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass
class WorktreeInfo:
    """Information about a created worktree."""
    path: Path
    branch_name: str
    reuse_status: str = "created"  # created | reused | recreated
    reuse_reason: str | None = None
    rebase_failed: bool = False  # True if rebase onto main failed (work was discarded)
    uncommitted_discarded: int = 0  # Count of uncommitted changes discarded during reset
    commits_discarded: int = 0  # Count of commits discarded during reset (rebase failure)


@dataclass
class WorktreeReuseOptions:
    """Options controlling worktree reuse behavior."""
    reuse_push_preflight: bool = True
    worktree_branch_on_recreate: str = "delete"
    allow_no_verify_dry_run_preflight: bool = True
    allow_remote_branch_delete: bool = True
    disable_reuse: bool = False


class WorktreeManager(Protocol):
    """Protocol for worktree lifecycle management.

    This protocol defines the interface for creating and removing
    git worktrees. Implementations handle the actual git operations.
    """

    def create(
        self,
        repo_root: Path,
        issue_number: int,
        issue_title: str,
        worktree_base: Path | None = None,
        enforce_hooks: bool = True,
        pre_push_hook: Path | None = None,
        branch_name: str | None = None,
        base_branch: str | None = None,
        seed_ref: str | None = None,
        reuse_options: WorktreeReuseOptions | None = None,
    ) -> WorktreeInfo:
        """Create a new git worktree for an issue.

        Args:
            repo_root: Path to the main git repository
            issue_number: GitHub issue number
            issue_title: GitHub issue title
            worktree_base: Base directory for worktrees
            enforce_hooks: Whether to install pre-push hooks
            pre_push_hook: Custom pre-push hook path
            branch_name: Specific branch to checkout (for reviews)
            base_branch: Branch to use for new worktree bases (defaults to default branch)
            seed_ref: Optional local ref used to seed fresh worktrees
            reuse_options: Options controlling reuse behavior

        Returns:
            WorktreeInfo with path and branch name

        Raises:
            WorktreeError: If creation fails
        """
        ...

    def remove(self, worktree_path: Path, *, force: bool = False) -> None:
        """Remove a git worktree.

        Args:
            worktree_path: Path to the worktree to remove
            force: If true, use forced worktree removal and fallback directory
                cleanup. This is reserved for hard lifecycle boundaries where
                the orchestrator intentionally discards local state.

        Raises:
            WorktreeError: If removal fails
        """
        ...

    def extract_issue_number(self, branch_name: str) -> int | None:
        """Extract issue number from a branch name.

        Args:
            branch_name: Branch name (e.g., "328-add-feature")

        Returns:
            Issue number if found, None otherwise
        """
        ...
