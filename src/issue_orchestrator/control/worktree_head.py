"""Typed worktree HEAD facts for completion control."""

from __future__ import annotations

import logging
from pathlib import Path

from .completion_ports import GitAdapter

logger = logging.getLogger(__name__)


def current_worktree_head_sha(
    *,
    git_adapter: GitAdapter,
    worktree: Path,
) -> str | None:
    """Return the actual commit currently checked out in ``worktree``."""
    try:
        head_sha = git_adapter.get_head_sha(worktree)
    except (OSError, RuntimeError, ValueError) as exc:
        logger.warning(
            "[REVIEW_EXCHANGE] could not resolve current worktree HEAD "
            "for %s: %s",
            worktree,
            exc,
        )
        return None
    if not isinstance(head_sha, str):
        return None
    normalized = head_sha.strip()
    return normalized or None
