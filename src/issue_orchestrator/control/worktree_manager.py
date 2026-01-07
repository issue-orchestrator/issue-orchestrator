"""WorktreeManagerControl - worktree-related control logic.

This module contains worktree-related methods extracted from the Orchestrator.
Note: This is a control-layer module that uses the WorktreeManager port.
"""

import logging
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..ports.working_copy import WorkingCopy

from ..infra.config import Config
from ..domain.models import AgentConfig

logger = logging.getLogger(__name__)


def get_worktree_path(
    config: Config,
    issue_number: int,
    agent_config: AgentConfig | None = None,  # Deprecated, kept for compatibility
) -> Path:
    """Compute the worktree path for an issue.

    Args:
        config: The orchestrator configuration.
        issue_number: The issue number.
        agent_config: Deprecated, no longer used. Worktree base is now config-level.

    Returns:
        The path where the worktree should be created.
    """
    repo_root = config.repo_root
    base = config.worktree_base
    return base / f"{repo_root.name}-{issue_number}"


def get_session_name(number: int, session_type: str = "issue") -> str:
    """Generate a session name for the given issue/PR number.

    Args:
        number: The issue or PR number.
        session_type: One of "issue", "review", or "rework".

    Returns:
        The session name (e.g., "issue-42", "review-123").

    Raises:
        ValueError: If session_type is invalid.
    """
    if session_type not in ("issue", "review", "rework"):
        raise ValueError(f"Invalid session_type: {session_type}")
    return f"{session_type}-{number}"


def extract_issue_branches(
    working_copy: "WorkingCopy",
    repo_root: Path,
) -> dict[int, str]:
    """Extract issue branches from the repository.

    Returns a mapping of issue number to branch name for all
    branches that match the issue branch pattern.
    """
    from ..infra.analysis import extract_issue_branches
    branches = working_copy.list_remote_branches(repo_root)
    return extract_issue_branches(branches)
