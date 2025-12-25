"""WorktreeManager adapter implementation.

Implements the WorktreeManager port using the git worktree implementation.
"""

from pathlib import Path

from ..ports.worktree_manager import WorktreeManager, WorktreeInfo
from .._worktree_impl import (
    create_worktree,
    remove_worktree,
    extract_issue_number_from_branch,
)


class GitWorktreeManager:
    """Git-based implementation of WorktreeManager.

    Wraps the _worktree_impl functions to implement the port protocol.
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
    ) -> WorktreeInfo:
        """Create a new git worktree for an issue."""
        path, branch = create_worktree(
            repo_root=repo_root,
            issue_number=issue_number,
            issue_title=issue_title,
            worktree_base=worktree_base,
            enforce_hooks=enforce_hooks,
            pre_push_hook=pre_push_hook,
            branch_name=branch_name,
        )
        return WorktreeInfo(path=path, branch_name=branch)

    def remove(self, worktree_path: Path) -> None:
        """Remove a git worktree."""
        remove_worktree(worktree_path)

    def extract_issue_number(self, branch_name: str) -> int | None:
        """Extract issue number from a branch name."""
        return extract_issue_number_from_branch(branch_name)
