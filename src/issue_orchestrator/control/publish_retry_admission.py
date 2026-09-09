"""May this issue be retried, and are its stored inputs still usable?

The recovery owner owns reservations and tombstones. These read-only checks
admit board state and require exact receipt locators; the intake owner validates
that receipt and its run in the worker. Agent completion files are not authority.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Iterable, Sequence

if TYPE_CHECKING:
    from ..domain.models import OrchestratorState
    from ..domain.publish_retry import PublishRetryLocators


def board_block_reason(
    *,
    issue_number: int,
    state: "OrchestratorState",
    labels: Sequence[str] | Iterable[str],
    publish_failed_label: str,
) -> str | None:
    """None when the BOARD admits a retry for *issue_number*, else the reason.

    ``labels`` must be a freshly observed set: an unreadable issue is decided
    before this is ever called, because "could not read" is not "not blocked"
    (#6957 round-2 review F4).
    """
    if publish_failed_label not in tuple(labels):
        return "Issue is not blocked by a publish failure"
    if any(session.issue.number == issue_number for session in state.active_sessions):
        return "Issue has an active session"
    return None


def locator_block_reason(locators: "PublishRetryLocators") -> str | None:
    """None when the stored retry inputs are still usable, else the reason."""
    worktree = Path(locators.worktree_path)
    if not worktree.exists():
        return "Retry worktree no longer exists"
    if locators.intake_receipt is None:
        return "Exact processed intake receipt for retry is missing"

    return None
