"""Canonical event classifiers shared by lifecycle and journey projections.

A small, narrow owner for the frozensets that both
``view_models.lifecycle_projection`` (typed lifecycle state) and
``view_models.journey_projection`` (drawer-facing journey overlay) consume.
Keeping the classifiers here breaks an import cycle between the two
projection modules and ensures neither copies a set — the AC-4 drift rail
from issue #6310.

Component sets for coding terminal classification (completed / blocked /
failed / publish-failed) are also public here so the lifecycle coder
projection (which dispatches between ``CompletedCodingAttempt`` /
``BlockedCodingAttempt`` / ``FailedCodingAttempt`` / etc.) and the
journey validation-badge derivation (which only cares about the union)
share a single definition.  Adding a new event name in one bucket
automatically propagates to both call sites — no parallel private set in
any consumer.
"""

from __future__ import annotations

from typing import Literal

CodingTerminalKind = Literal["completed", "blocked", "failed", "publish_failed"]


CODING_COMPLETED_EVENTS: frozenset[str] = frozenset(
    {
        "agent.coding_completed",
        "observation.completion_detected",
        "session.completed",
    }
)
CODING_BLOCKED_EVENTS: frozenset[str] = frozenset(
    {"agent.blocked", "session.blocked", "issue.blocked"}
)
CODING_FAILED_EVENTS: frozenset[str] = frozenset(
    {
        "agent.failed",
        "agent.timed_out",
        "session.failed",
        "session.timeout",
    }
)
CODING_PUBLISH_FAILED_EVENTS: frozenset[str] = frozenset({"publish.failed"})

CODING_TERMINAL_EVENTS: frozenset[str] = (
    CODING_COMPLETED_EVENTS
    | CODING_BLOCKED_EVENTS
    | CODING_FAILED_EVENTS
    | CODING_PUBLISH_FAILED_EVENTS
)


def classify_coding_terminal_event(
    event_name: str,
) -> CodingTerminalKind | None:
    """Classify an event name into one of four coding-terminal buckets.

    Single owner for the "which bucket does this terminal event belong to"
    decision.  Used by ``lifecycle_projection`` for typed coder-attempt
    dispatch, and indirectly by the journey badge derivation (via the
    union ``CODING_TERMINAL_EVENTS``).  Returns ``None`` for non-terminal
    events.
    """
    if event_name in CODING_COMPLETED_EVENTS:
        return "completed"
    if event_name in CODING_BLOCKED_EVENTS:
        return "blocked"
    if event_name in CODING_FAILED_EVENTS:
        return "failed"
    if event_name in CODING_PUBLISH_FAILED_EVENTS:
        return "publish_failed"
    return None

VALIDATION_PASSED_EVENTS: frozenset[str] = frozenset(
    {"validation.passed", "session.validation_passed"}
)
VALIDATION_FAILED_EVENTS: frozenset[str] = frozenset(
    {
        "validation.failed",
        "session.validation_failed",
        "session.validation_retry_needed",
    }
)

# Outcome-relevant events used by the journey projection to derive a
# cycle's outcome label.  Content preserved verbatim from the historical
# ``view_models.issue_detail._OUTCOME_EVENTS`` so the projection
# consolidation introduces no behavior drift.
OUTCOME_EVENTS: frozenset[str] = frozenset(
    {
        "session.failed",
        "session.timeout",
        "session.blocked",
        "session.completed",
        "review_exchange.round_completed",
        "review.changes_requested",
        "review.approved",
        "review.escalated",
        "review.merged",
        "issue.blocked",
        "issue.needs_human",
        "publish.failed",
        "issue.completed",
    }
)

# Blocked-relevant events used by issue-detail to derive a blocked-status
# explanation.  Content preserved verbatim from the historical
# ``view_models.issue_detail._BLOCKED_EVENT_NAMES``.
BLOCKED_EVENT_NAMES: frozenset[str] = frozenset(
    {
        "session.timeout",
        "session.failed",
        "session.blocked",
        "session.validation_failed",
        "issue.blocked",
        "issue.needs_human",
        "publish.failed",
        "review.changes_requested",
        "review.escalated",
    }
)


__all__ = [
    "BLOCKED_EVENT_NAMES",
    "CODING_BLOCKED_EVENTS",
    "CODING_COMPLETED_EVENTS",
    "CODING_FAILED_EVENTS",
    "CODING_PUBLISH_FAILED_EVENTS",
    "CODING_TERMINAL_EVENTS",
    "CodingTerminalKind",
    "OUTCOME_EVENTS",
    "VALIDATION_FAILED_EVENTS",
    "VALIDATION_PASSED_EVENTS",
    "classify_coding_terminal_event",
]
