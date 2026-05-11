"""Canonical event classifiers shared by lifecycle and journey projections.

A small, narrow owner for the frozensets that both
``view_models.lifecycle_projection`` (typed lifecycle state) and
``view_models.journey_projection`` (drawer-facing journey overlay) consume.
Keeping the classifiers here breaks an import cycle between the two
projection modules and ensures neither copies a set — the AC-4 drift rail
from issue #6310.
"""

from __future__ import annotations

# Coding-side terminal events — the union of completion and failure-mode
# events emitted at the end of a coding attempt.  Public so the per-cycle
# validation badge and the lifecycle coder projection share a single
# definition of "coding is over".
_CODING_COMPLETED_EVENTS: frozenset[str] = frozenset(
    {
        "agent.coding_completed",
        "observation.completion_detected",
        "session.completed",
    }
)
_CODING_BLOCKED_EVENTS: frozenset[str] = frozenset(
    {"agent.blocked", "session.blocked", "issue.blocked"}
)
_CODING_FAILED_EVENTS: frozenset[str] = frozenset(
    {
        "agent.failed",
        "agent.timed_out",
        "session.failed",
        "session.timeout",
    }
)
_CODING_PUBLISH_FAILED_EVENTS: frozenset[str] = frozenset({"publish.failed"})

CODING_TERMINAL_EVENTS: frozenset[str] = (
    _CODING_COMPLETED_EVENTS
    | _CODING_BLOCKED_EVENTS
    | _CODING_FAILED_EVENTS
    | _CODING_PUBLISH_FAILED_EVENTS
)

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
    "CODING_TERMINAL_EVENTS",
    "OUTCOME_EVENTS",
    "VALIDATION_FAILED_EVENTS",
    "VALIDATION_PASSED_EVENTS",
]
