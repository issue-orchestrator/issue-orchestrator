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
    VALID_PR_COLLISION_MODES = {"fail", "reuse_open", "new_branch"}

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
        if config.worktree_remediation_pr_collision not in self.VALID_PR_COLLISION_MODES:
            errors.append(
                "worktrees.remediation.pr_collision must be one of "
                f"{sorted(self.VALID_PR_COLLISION_MODES)}, got: '{config.worktree_remediation_pr_collision}'"
            )
        if config.worktree_base_branch_override is not None:
            if not config.worktree_base_branch_override.strip():
                errors.append("worktrees.base_branch_override must be a non-empty string when set")
            if config.worktree_base_branch_override.startswith("origin/"):
                errors.append("worktrees.base_branch_override must not include 'origin/' prefix")

        return errors
