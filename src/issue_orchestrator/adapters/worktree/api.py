"""Public worktree API for tests and adapters."""

from ._worktree import (
    WorktreeError,
    create_worktree,
    remove_worktree,
    list_worktrees,
    worktree_exists,
    has_uncommitted_changes,
    install_hooks,
    slugify,
    generate_branch_name,
    get_worktree_branch,
    next_branch_name,
)


__all__ = [
    "WorktreeError",
    "create_worktree",
    "remove_worktree",
    "list_worktrees",
    "worktree_exists",
    "has_uncommitted_changes",
    "install_hooks",
    "slugify",
    "generate_branch_name",
    "get_worktree_branch",
    "next_branch_name",
]
