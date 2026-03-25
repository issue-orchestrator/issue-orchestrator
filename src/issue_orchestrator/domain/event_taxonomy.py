"""Shared event classification helpers used by projections and view models."""

from __future__ import annotations

from enum import Enum

from ..events import EventName

_REVIEW_EVENTS = frozenset({
    EventName.REVIEW_STARTED,
    EventName.REVIEW_QUEUED,
    EventName.REVIEW_ESCALATED,
    EventName.REVIEW_SKIPPED,
    EventName.REVIEW_LAUNCHING,
    EventName.REVIEW_APPROVED,
    EventName.REVIEW_CHANGES_REQUESTED,
    EventName.REVIEW_REWORK_STARTED,
    EventName.REVIEW_REWORK_COMPLETED,
    EventName.REVIEW_TRIAGE_STARTED,
    EventName.REVIEW_TRIAGE_APPROVED,
    EventName.REVIEW_MERGED,
    EventName.REVIEW_CLOSED,
    EventName.REVIEW_COMMENT_ADDED,
    EventName.REVIEW_EXCHANGE_STARTED,
    EventName.REVIEW_EXCHANGE_ROUND_STARTED,
    EventName.REVIEW_EXCHANGE_ROUND_COMPLETED,
    EventName.REVIEW_EXCHANGE_COMPLETED,
    EventName.REVIEW_EXCHANGE_FAILED,
})

_REVIEW_EXCHANGE_EVENTS = frozenset({
    EventName.REVIEW_EXCHANGE_STARTED,
    EventName.REVIEW_EXCHANGE_ROUND_STARTED,
    EventName.REVIEW_EXCHANGE_ROUND_COMPLETED,
    EventName.REVIEW_EXCHANGE_COMPLETED,
    EventName.REVIEW_EXCHANGE_FAILED,
})


class EventIntent(str, Enum):
    """Typed semantic intent carried with timeline events."""

    REVIEW = "review"
    REWORK = "rework"
    CODING = "coding"
    ORCHESTRATOR = "orchestrator"
    SYSTEM = "system"


def _to_event_name(event_name: str) -> EventName | None:
    try:
        return EventName(event_name)
    except ValueError:
        return None


def is_review_event_name(event_name: str) -> bool:
    """Return True when an event belongs to the review family."""
    event = _to_event_name(event_name)
    if event is not None:
        return event in _REVIEW_EVENTS
    return event_name.startswith("review.") or event_name.startswith("review_exchange.")


def is_review_exchange_event_name(event_name: str) -> bool:
    """Return True when an event belongs to the review-exchange subfamily."""
    event = _to_event_name(event_name)
    if event is not None:
        return event in _REVIEW_EXCHANGE_EVENTS
    return event_name.startswith("review_exchange.")


def is_review_oriented_event(*, event_name: str, task: str | None = None) -> bool:
    """Return True when event semantics should be treated as review-oriented."""
    if is_review_event_name(event_name):
        return True
    return (task or "").strip().lower() == "review"


def is_session_event_name(event_name: str) -> bool:
    event = _to_event_name(event_name)
    if event is not None:
        return event.name.startswith("SESSION_")
    return event_name.startswith("session.")


def is_issue_event_name(event_name: str) -> bool:
    event = _to_event_name(event_name)
    if event is not None:
        return event.name.startswith("ISSUE_")
    return event_name.startswith("issue.")


def is_rework_event_name(event_name: str) -> bool:
    event = _to_event_name(event_name)
    if event is not None:
        return event.name.startswith("REWORK_")
    return event_name.startswith("rework.")


def is_validation_event_name(event_name: str) -> bool:
    event = _to_event_name(event_name)
    if event is not None:
        return event.name.startswith("VALIDATION_") or event.name.startswith("SESSION_VALIDATION_")
    return event_name.startswith("validation.") or event_name.startswith("session.validation_")


def is_completion_event_name(event_name: str) -> bool:
    event = _to_event_name(event_name)
    if event is not None:
        return event.name.startswith("COMPLETION_")
    return event_name.startswith("completion.")


def is_observation_event_name(event_name: str) -> bool:
    event = _to_event_name(event_name)
    if event is not None:
        return event.name.startswith("OBSERVATION_")
    return event_name.startswith("observation.")


def is_e2e_event_name(event_name: str) -> bool:
    return event_name.startswith("e2e.")


def infer_event_intent(*, event_name: str, task: str | None = None) -> EventIntent:
    """Classify event into a typed semantic intent."""
    normalized_task = (task or "").strip().lower()
    if is_review_oriented_event(event_name=event_name, task=normalized_task):
        return EventIntent.REVIEW
    if is_rework_event_name(event_name) or normalized_task == "rework":
        return EventIntent.REWORK
    if is_validation_event_name(event_name) or is_issue_event_name(event_name):
        return EventIntent.ORCHESTRATOR
    if is_session_event_name(event_name) or normalized_task in {"code", "coding"}:
        return EventIntent.CODING
    return EventIntent.SYSTEM
