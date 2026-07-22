"""Front-queue issues that become schedulable after being blocked (#6873).

A blocked issue — whether by a blocking label or a closed dependency gate — was
already deemed important enough to be in flight, so when it becomes AVAILABLE
again it jumps to the front of the work queue instead of sorting to the back
with fresh backlog. The policy is keyed on the scheduler's own availability
verdict (:class:`IssueAvailabilityDecision.reason`), so it covers BOTH routes out
of blocked — a removed blocking label and a re-opened dependency gate — and can
never drift from ``Scheduler._evaluate_issue``.

The write routes through the ``priority_queue`` owner (:class:`RetryHistoryState`)
and uses the unbounded operator/retry lane, NOT the capped tech-lead expedite
lane (#6870): restoring known work is neither gated nor bounded. The blocked
baseline is in-memory like ``priority_queue`` itself, so a restart forgets it and
re-establishes it on the next scan (never a spurious front-queue).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..domain.models import OrchestratorState
    from .scheduler import IssueAvailabilityDecision

logger = logging.getLogger(__name__)

# Scheduler availability reasons that mean "blocked, but could later unblock".
_BLOCKED_REASONS = ("blocked_label", "dependency_blocked")


def front_queue_newly_unblocked(
    state: "OrchestratorState",
    decisions: "list[IssueAvailabilityDecision]",
) -> None:
    """Move issues that just became schedulable after being blocked to the front."""
    from .retry_history_state import RetryHistoryState

    by_reason: dict[str, set[int]] = {}
    for decision in decisions:
        by_reason.setdefault(decision.reason, set()).add(decision.issue.number)

    blocked_now: set[int] = set()
    for reason in _BLOCKED_REASONS:
        blocked_now |= by_reason.get(reason, set())
    available_now = by_reason.get("available", set())

    newly_schedulable = state.previously_blocked_issue_numbers & available_now
    retry = RetryHistoryState(state)
    for number in sorted(newly_schedulable):
        retry.prioritize_issue_front(number)
    if newly_schedulable:
        logger.info(
            "[BLOCKED->FRONT] %d issue(s) left a blocked state and moved to the "
            "front of the queue: %s",
            len(newly_schedulable),
            sorted(newly_schedulable),
        )
    state.previously_blocked_issue_numbers = blocked_now
