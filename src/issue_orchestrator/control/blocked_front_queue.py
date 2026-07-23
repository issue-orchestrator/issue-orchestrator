"""Front-queue issues that become schedulable after being blocked (#6873).

A blocked issue — whether by a blocking label or a closed dependency gate — was
already deemed important enough to be in flight, so when it becomes AVAILABLE
again it jumps to the front of the work queue instead of sorting to the back
with fresh backlog. The policy keys on the scheduler's own **typed** availability
predicates (`IssueAvailabilityDecision.is_blocked` / `.available`), so it covers
both routes out of blocked — a removed blocking label and a re-opened dependency
gate — and cannot drift from `Scheduler._evaluate_issue` (there are no loose
string literals here to fall out of sync).

Writes route through an **owned, ledgered** lane on `RetryHistoryState`
(`prioritize_blocked_front` / `release_blocked_front`). Unlike the tech-lead
expedite lane (#6870) it is deliberately *unbounded* — restoring known work is
neither gated nor capped — but it is ledgered so its entries have a real
lifecycle: released on successful launch (via the always-run launch handler
`OrchestratorSupport._handle_launch_session`, NOT the tech-lead-specific
`ExpediteLane`, so cleanup is an invariant of every composition root) and on
re-block / out-of-band unavailability (the reconciliation below). Operator-owned
priorities are never disturbed — the owner only tracks and releases entries this
lane itself placed.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..domain.models import OrchestratorState
    from .scheduler import IssueAvailabilityDecision
    from .session_manager import SessionType

logger = logging.getLogger(__name__)


def front_queue_newly_unblocked(
    state: "OrchestratorState",
    decisions: "list[IssueAvailabilityDecision]",
) -> None:
    """Reconcile the blocked->front restore lane against this scan's decisions."""
    from .retry_history_state import RetryHistoryState

    blocked_now = {d.issue.number for d in decisions if d.is_blocked}
    available_now = {d.issue.number for d in decisions if d.available}

    retry = RetryHistoryState(state)

    # (1) Newly schedulable: blocked at the last scan, available now -> jump the
    #     front. prioritize_blocked_front skips an already-queued issue, so an
    #     operator/tech-lead priority is neither duplicated nor claimed.
    newly_schedulable = state.previously_blocked_issue_numbers & available_now
    added = [n for n in sorted(newly_schedulable) if retry.prioritize_blocked_front(n)]

    # (2) Reconcile ownership: a lane-owned entry that is no longer available —
    #     re-blocked, picked up, or closed — leaves the lane. The launch hook
    #     also releases on successful pickup; this is the backstop for re-block
    #     and out-of-band unavailability, so the lane never leaks stale priority.
    released = [
        n
        for n in list(state.blocked_front_prioritized)
        if n not in available_now and retry.release_blocked_front(n)
    ]

    if added or released:
        logger.info(
            "[BLOCKED->FRONT] +%d to front %s / -%d released %s",
            len(added),
            added,
            len(released),
            released,
        )
    state.previously_blocked_issue_numbers = blocked_now


def release_blocked_front_on_launch(
    state: "OrchestratorState",
    session_type: "SessionType",
    issue_number: int | None,
) -> None:
    """Free a blocked->front entry once its issue launches (#6873 R4/R5/N4).

    Hooked into the ALWAYS-RUN successful-launch state handler
    (``OrchestratorSupport._handle_launch_session``), not the tech-lead-specific
    ``ExpediteLane`` composition seam — so launch cleanup is an invariant of every
    composition root rather than accidentally coupled to tech-lead wiring.
    Success-only by contract: the only caller runs solely on a successful launch.

    Cleanup is scoped to ISSUE launches. A blocked->front entry is an issue
    awaiting pickup, so only its own coding launch retires it here; a REVIEW
    launch — whose ``LaunchSessionAction.number`` is a PR number — must never
    touch the lane (that was the R5 collision), and any other pickup is caught by
    the reconciliation backstop in ``front_queue_newly_unblocked`` on the next
    scan. Non-issue launches are therefore an explicit no-op, not a missing-field
    silent no-op.

    For an ISSUE launch the CANONICAL ``issue_number`` (the ``ActionResult``
    identity, never the polymorphic action number) is REQUIRED: the wired
    ``ActionApplier`` always returns it, so its absence is a producer/contract
    bug that would silently leak the priority entry — surfaced (fail-fast) rather
    than swallowed. Releasing an entry this lane does not own is itself a no-op.
    """
    from .session_manager import SessionType

    if session_type is not SessionType.ISSUE:
        return
    if issue_number is None:
        raise AssertionError(
            "a successful ISSUE launch carried no canonical issue_number "
            "(ActionApplier contract violation)"
        )
    from .retry_history_state import RetryHistoryState

    RetryHistoryState(state).release_blocked_front(issue_number)
