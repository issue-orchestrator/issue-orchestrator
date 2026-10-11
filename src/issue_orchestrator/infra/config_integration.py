"""Integration-branch mode config: parsing, implication and minimal YAML (#8144).

``integration.enabled`` implies ``worktrees.base_branch_override`` = the
integration branch: agents' worktrees and PRs use it as their base. A config
that names a DIFFERENT override, or that also enables the merge queue (a second
owner of the same merge decision), fails at load rather than picking one.
"""

from dataclasses import fields
from typing import TYPE_CHECKING

from .config_models import IntegrationConfig

if TYPE_CHECKING:
    from .config import Config


def parse_integration_config(data: dict) -> IntegrationConfig:
    """Parse the ``integration`` section; ``IntegrationConfig`` validates it."""
    if not isinstance(data, dict):
        raise ValueError("integration must be a mapping")
    known = {item.name for item in fields(IntegrationConfig)}
    unknown = sorted(set(data) - known)
    if unknown:
        raise ValueError(f"integration has unknown keys {unknown}; allowed: {sorted(known)}")
    return IntegrationConfig(**data)


def apply_integration_implication(config: "Config") -> None:
    """Make an enabled integration branch the worktree/PR base, or fail loudly."""
    integration = config.integration
    if not integration.enabled:
        return
    if config.merge_queue.enabled:
        raise ValueError(
            "integration.enabled and merge_queue.enabled cannot both be true: each owns"
            " the merge of an approved PR"
        )
    override = config.worktree_base_branch_override
    if override is not None and override != integration.branch:
        raise ValueError(
            f"worktrees.base_branch_override is {override!r} but integration.branch is"
            f" {integration.branch!r}; integration mode implies the override, so drop it"
            " or make them equal"
        )
    config.worktree_base_branch_override = integration.branch


def implied_base_branch_override(config: "Config") -> bool:
    """Whether the worktree base override is only the one integration implies."""
    return (
        config.integration.enabled
        and config.worktree_base_branch_override == config.integration.branch
    )


def integration_section(config: "Config") -> dict:
    """Only the values that differ from the defaults."""
    values, defaults = config.integration, IntegrationConfig()
    return {
        item.name: getattr(values, item.name)
        for item in fields(IntegrationConfig)
        if getattr(values, item.name) != getattr(defaults, item.name)
    }
