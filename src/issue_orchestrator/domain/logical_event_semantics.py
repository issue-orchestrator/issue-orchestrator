"""Backend-owned logical timeline semantics.

Assigns canonical logical run/cycle/phase fields once at event-write time.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .event_taxonomy import EventIntent, infer_event_intent

_TERMINAL_EVENTS = frozenset({
    "issue.blocked",
    "issue.needs_human",
    "issue.completed",
    "session.failed",
    "session.timeout",
    "session.blocked",
})
_RUN_RESTART_EVENTS = frozenset({"issue.unblocked"})
_ITERATION_START_EVENTS = frozenset({
    "session.started",
    "rework.started",
    "rework.launching",
    "review.started",
    "review_exchange.started",
})


@dataclass(frozen=True)
class LogicalSemantics:
    logical_run: int
    logical_cycle: int
    logical_phase: str
    event_intent: str
    review_oriented: bool
    restart_pending: bool


def enrich_logical_semantics(
    *,
    event_name: str,
    event_data: dict[str, Any],
    previous_event_name: str | None,
    previous_data: dict[str, Any] | None,
) -> LogicalSemantics:
    """Compute canonical logical fields for one timeline event."""
    task = event_data.get("task")
    task_value = task if isinstance(task, str) else None
    intent = infer_event_intent(event_name=event_name, task=task_value)

    prev_run = _as_positive_int(previous_data.get("logical_run") if previous_data else None)
    prev_cycle = _as_positive_int(previous_data.get("logical_cycle") if previous_data else None)
    prev_restart_pending = bool(previous_data.get("_logical_restart_pending")) if previous_data else False
    logical_run = prev_run or 1

    restart_due_to_label_change = _is_pr_pending_removed_event(event_name, event_data)
    restart_due_to_transition = (
        (previous_event_name in _TERMINAL_EVENTS or prev_restart_pending)
        and event_name in _ITERATION_START_EVENTS
    )
    restart_due_to_event = event_name in _RUN_RESTART_EVENTS
    if restart_due_to_label_change or restart_due_to_transition or restart_due_to_event:
        logical_run = logical_run + 1

    signal_cycle = _cycle_from_signal(event_data.get("rework_cycle"))
    if logical_run != (prev_run or 1):
        logical_cycle = 1
    elif signal_cycle is not None:
        logical_cycle = signal_cycle
    else:
        logical_cycle = prev_cycle or 1

    logical_phase = _phase_for_intent(intent)
    if logical_cycle > 1 and logical_phase == "coding":
        logical_phase = "rework"

    restart_pending = _compute_restart_pending(
        event_name=event_name,
        previous_event_name=previous_event_name,
        previous_restart_pending=prev_restart_pending,
    )

    return LogicalSemantics(
        logical_run=logical_run,
        logical_cycle=logical_cycle,
        logical_phase=logical_phase,
        event_intent=intent.value,
        review_oriented=(intent == EventIntent.REVIEW),
        restart_pending=restart_pending,
    )


def _phase_for_intent(intent: EventIntent) -> str:
    if intent == EventIntent.REVIEW:
        return "review"
    if intent == EventIntent.REWORK:
        return "rework"
    if intent == EventIntent.CODING:
        return "coding"
    if intent == EventIntent.ORCHESTRATOR:
        return "orchestrator"
    return "system"


def _as_positive_int(value: Any) -> int | None:
    if isinstance(value, int) and value > 0:
        return value
    return None


def _cycle_from_signal(value: Any) -> int | None:
    if isinstance(value, int) and value >= 0:
        return value + 1
    return None


def _is_pr_pending_removed_event(event_name: str, event_data: dict[str, Any]) -> bool:
    if event_name != "issue.labels_changed":
        return False
    removed = event_data.get("removed")
    if not isinstance(removed, list):
        return False
    return any(isinstance(label, str) and label.split(":", 1)[0] == "pr-pending" for label in removed)


def _compute_restart_pending(
    *,
    event_name: str,
    previous_event_name: str | None,
    previous_restart_pending: bool,
) -> bool:
    """Track whether we've reached a terminal boundary and are awaiting next start."""
    if event_name in _TERMINAL_EVENTS:
        return True
    if event_name in _ITERATION_START_EVENTS:
        return False
    if event_name in _RUN_RESTART_EVENTS:
        return False
    if previous_event_name in _TERMINAL_EVENTS:
        return True
    return previous_restart_pending
