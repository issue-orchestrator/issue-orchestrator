"""The launch boundary never starts a coder over a PR of published validated work (#7293).

The scheduler gates on the ``pr-pending`` label, but a label is not custody: it
may never have been applied (review discovery drops a review while the issue
is blocked), or a human may remove the blocking label on GitHub directly. An
issue that looks schedulable then launches a coder whose worktree recreate
deletes the remote branch of the PR carrying its published validated work -
closing that PR. So the last check before any claim or worktree is made asks
the custody owner itself, and repairs the missing gate so the scheduler stops
offering the issue.

Tech-lead sessions are exempt: they read their subject from a scratch worktree
(or their own anchor) and never touch the subject issue's branch.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ..infra.logging_config import issue_log
from .actions import AddLabelAction
from .session_launch_types import LaunchResult

if TYPE_CHECKING:
    from .action_applier import ActionApplier
    from .label_manager import LabelManager

logger = logging.getLogger(__name__)


def refuse_launch_over_published_review(
    action_applier: "ActionApplier",
    labels: "LabelManager",
    issue_number: int,
    *,
    tech_lead: bool,
) -> LaunchResult | None:
    """A non-launch while an open PR holds the issue's published work, else None."""
    if tech_lead:
        return None
    holds = action_applier.runtime_lifecycle.published_review.holds(issue_number)
    if not holds:
        return None
    described = "; ".join(hold.describe() for hold in holds)
    gate = action_applier.apply(
        AddLabelAction(
            issue_number=issue_number,
            label=labels.pr_pending,
            fresh_presence=True,
            reason=f"published validated work is under review: {described}",
        )
    )
    logger.warning(
        issue_log(
            issue_number,
            "Launch refused: %s; its review owns the issue (pr-pending %s) (#7293)",
        ),
        described,
        "restored" if gate.success else f"NOT restored: {gate.error}",
    )
    return LaunchResult(
        None,
        False,
        f"Published validated work is under review: {described}",
    )
