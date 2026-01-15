"""Worktree configuration validator."""

from typing import TYPE_CHECKING

from .base import ConfigValidator

if TYPE_CHECKING:
    from ..config import Config


class WorktreeValidator(ConfigValidator):
    """Validates worktree-related configuration.

    Checks:
    - worktrees.base is an absolute path
    - worktrees.base exists and is a directory
    - worktree_branch_on_recreate is valid
    """

    VALID_RECREATE_MODES = {"delete", "create_new_branch"}

    def validate(self, config: "Config") -> list[str]:
        errors = []

        if not config.worktree_base.is_absolute():
            errors.append(
                f"worktrees.base must be absolute path, got: {config.worktree_base}"
            )
        elif not config.worktree_base.exists():
            errors.append(f"worktrees.base does not exist: {config.worktree_base}")
        elif not config.worktree_base.is_dir():
            errors.append(
                f"worktrees.base is not a directory: {config.worktree_base}"
            )

        if config.worktree_branch_on_recreate not in self.VALID_RECREATE_MODES:
            errors.append(
                "worktree_branch_on_recreate must be one of "
                f"{sorted(self.VALID_RECREATE_MODES)}, got: '{config.worktree_branch_on_recreate}'"
            )

        return errors
