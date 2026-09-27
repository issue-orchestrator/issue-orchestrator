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
(or their own anchor) and never touch the subject issue's branch. A rework
(including its validation retry, which relaunches as a rework since #7347) is
exempt from a hold on the PR it is fixing: pushing to that PR is its review
cycle, which the hold exists to let finish. A hold on any other PR - or a
rework that names no PR - still refuses it.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ..domain.session_kind import SessionKind
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
    kind: SessionKind,
    pr_number: int | None,
) -> LaunchResult | None:
    """A non-launch while an open PR holds the issue's published work, else None.

    ``kind`` and ``pr_number`` are the launching session's own: the kind it
    will run as and, for a rework, the PR it is fixing. Only a kind whose
    output IS the issue's deliverable (``capturable``) would start a second
    author on the held PR's branch; a tech-lead run reads the issue and
    publishes nothing as its work, so it is not refused. A kind that pushes to
    an already-open PR is not refused by a hold on that same PR (#7293, #7347).
    """
    if not kind.capabilities.capturable:
        return None
    holds = action_applier.runtime_lifecycle.published_review.holds(issue_number)
    if kind.capabilities.pushes_to_an_open_pr and pr_number is not None:
        holds = tuple(hold for hold in holds if hold.pr_number != pr_number)
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
    reason = f"Published validated work is under review: {described}"
    if gate.host_rate_limit is not None:
        # The gate could not be restored because GitHub refused it on a rate
        # limit (#7297): defer on the shared window instead of re-reading
        # custody every tick, and restore the gate once it reopens.
        return LaunchResult.host_rate_limited(reason, gate.host_rate_limit)
    return LaunchResult(None, False, reason)
