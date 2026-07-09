"""Public worktree API for tests and adapters."""

from ._worktree import (
    WorktreeError,
    create_worktree,
    remove_worktree,
    list_worktrees,
    worktree_exists,
    has_uncommitted_changes,
    can_remove_without_user_changes,
    install_hooks,
    slugify,
    generate_branch_name,
    get_worktree_branch,
    next_branch_name,
    find_worktree_for_branch,
    HOOKS_DIR,
    install_claude_settings,
    sync_cli_tools,
)


__all__ = [
    "WorktreeError",
    "create_worktree",
    "remove_worktree",
    "list_worktrees",
    "worktree_exists",
    "has_uncommitted_changes",
    "can_remove_without_user_changes",
    "install_hooks",
    "slugify",
    "generate_branch_name",
    "get_worktree_branch",
    "next_branch_name",
    "find_worktree_for_branch",
    "HOOKS_DIR",
    "install_claude_settings",
    "sync_cli_tools",
]
