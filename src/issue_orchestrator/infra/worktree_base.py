"""Worktree base branch resolution."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ResolvedBaseBranch:
    """Resolved base branch and its resolution source."""

    branch: str
    source: str


def resolve_base_branch(
    repo_root: Path,
    *,
    config_override: str | None,
    default_branch_resolver: Callable[[Path], str],
    env_override: str | None = None,
    log: logging.Logger | None = None,
) -> ResolvedBaseBranch:
    """Resolve the base branch for worktree creation/reset.

    Resolution order:
        1) Config override (worktrees.base_branch_override)
        2) Env override (ORCHESTRATOR_WORKTREE_BASE_BRANCH)
        3) Auto-detected default branch
    """
    log = log or logger

    if config_override:
        branch = config_override.strip()
        if branch:
            log.info("Resolved worktree base branch: %s (source=config_override)", branch)
            return ResolvedBaseBranch(branch=branch, source="config_override")

    if env_override is None:
        env_override = os.environ.get("ORCHESTRATOR_WORKTREE_BASE_BRANCH")
    if env_override:
        branch = env_override.strip()
        if branch:
            log.info("Resolved worktree base branch: %s (source=env_override)", branch)
            return ResolvedBaseBranch(branch=branch, source="env_override")

    branch = default_branch_resolver(repo_root)
    log.info("Resolved worktree base branch: %s (source=auto_detect)", branch)
    return ResolvedBaseBranch(branch=branch, source="auto_detect")
