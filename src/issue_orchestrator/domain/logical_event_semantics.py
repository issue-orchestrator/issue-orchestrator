"""Backend-owned logical timeline semantics.

Assigns canonical logical run/cycle/phase fields once at event-write time.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .event_taxonomy import EventIntent, infer_event_intent

_TERMINAL_EVENTS = frozenset(
    {
        "issue.blocked",
        "issue.needs_human",
        "issue.completed",
        "session.failed",
        "session.invalid_completion_record",
        "session.timeout",
        "session.blocked",
    }
)
_RUN_RESTART_EVENTS = frozenset({"issue.unblocked"})
_CYCLE_BOUNDARY_EVENTS = frozenset(
    {
        "session.validation_retry_needed",
    }
)
_ITERATION_START_EVENTS = frozenset(
    {
        "session.started",
        "rework.started",
        "rework.launching",
        "review.started",
        "review_exchange.started",
    }
)
_ROUND_CURRENT_CYCLE_EVENTS = frozenset(
    {
        "review_exchange.round_started",
        "review_exchange.round_completed",
    }
)
_ROUND_NEXT_CYCLE_EVENTS = frozenset(
    {
        "review.rework_started",
        "review.rework_completed",
    }
)
_ROUND_COUNT_EVENTS = frozenset(
    {
        "review_exchange.completed",
    }
)
_REWORK_ATTEMPT_START_EVENTS = frozenset(
    {
        "rework.started",
    }
)


@dataclass(frozen=True)
class LogicalSemantics:
    logical_run: int
    logical_cycle: int
    logical_phase: str
    event_intent: str
    review_oriented: bool
    restart_pending: bool
    rework_driven: bool


def enrich_logical_semantics(
    *,
    event_name: str,
    event_data: dict[str, Any],
    previous_event_name: str | None,
    previous_data: dict[str, Any] | None,
    current_instance_id: str = "",
    previous_instance_id: str = "",
) -> LogicalSemantics:
    """Compute canonical logical fields for one timeline event."""
    task = event_data.get("task")
    task_value = task if isinstance(task, str) else None
    intent = infer_event_intent(event_name=event_name, task=task_value)

    prev_run = _as_positive_int(
        previous_data.get("logical_run") if previous_data else None
    )
    prev_cycle = _as_positive_int(
        previous_data.get("logical_cycle") if previous_data else None
    )
    prev_restart_pending = (
        bool(previous_data.get("_logical_restart_pending")) if previous_data else False
    )
    logical_run = prev_run or 1

    restart_due_to_transition = (
        previous_event_name in _TERMINAL_EVENTS or prev_restart_pending
    ) and event_name in _ITERATION_START_EVENTS
    restart_due_to_event = prev_run is not None and event_name in _RUN_RESTART_EVENTS
    # Orchestrator restart: instance_id changed between consecutive events
    restart_due_to_instance = bool(
        current_instance_id
        and previous_instance_id
        and current_instance_id != previous_instance_id
    )
    if restart_due_to_transition or restart_due_to_event or restart_due_to_instance:
        logical_run = logical_run + 1

    signal_cycle = _cycle_from_signal(event_data.get("rework_cycle"))
    round_cycle = _cycle_from_review_round(event_name, event_data)
    rework_driven = False
    if signal_cycle is not None and (
        logical_run == (prev_run or 1) or signal_cycle > 1
    ):
        logical_cycle = _logical_cycle_from_rework_signal(
            event_name=event_name,
            event_data=event_data,
            signal_cycle=signal_cycle,
            previous_cycle=prev_cycle,
            same_logical_run=logical_run == (prev_run or 1),
        )
        rework_driven = True
    elif logical_run != (prev_run or 1):
        logical_cycle = 1
    elif round_cycle is not None:
        # Review-exchange round indices are local to the exchange. They may
        # advance an initial review into a later cycle when no stronger cycle
        # signal exists, but they must never pull a validation-retry/restart
        # cycle backward.
        logical_cycle = max(round_cycle, prev_cycle or round_cycle)
        rework_driven = logical_cycle > 1
    elif event_name in _CYCLE_BOUNDARY_EVENTS:
        logical_cycle = (prev_cycle or 1) + 1
    else:
        logical_cycle = prev_cycle or 1
        rework_driven = bool(
            previous_data and previous_data.get("_logical_rework_driven")
        )

    logical_phase = _phase_for_intent(intent)
    if rework_driven and logical_cycle > 1 and logical_phase == "coding":
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
        rework_driven=rework_driven,
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


def _logical_cycle_from_rework_signal(
    *,
    event_name: str,
    event_data: dict[str, Any],
    signal_cycle: int,
    previous_cycle: int | None,
    same_logical_run: bool,
) -> int:
    if previous_cycle is None or not same_logical_run:
        return signal_cycle
    if (
        _starts_new_positive_rework_attempt(
            event_name=event_name,
            event_data=event_data,
        )
        and signal_cycle <= previous_cycle
    ):
        return previous_cycle + 1
    return max(signal_cycle, previous_cycle)


def _starts_new_positive_rework_attempt(
    *,
    event_name: str,
    event_data: dict[str, Any],
) -> bool:
    return event_name in _REWORK_ATTEMPT_START_EVENTS and _cycle_from_signal(
        event_data.get("rework_cycle")
    ) not in (None, 1)


def _cycle_from_review_round(event_name: str, event_data: dict[str, Any]) -> int | None:
    round_index = _as_positive_int(event_data.get("round_index"))
    if event_name in _ROUND_CURRENT_CYCLE_EVENTS and round_index is not None:
        return round_index
    if event_name in _ROUND_NEXT_CYCLE_EVENTS and round_index is not None:
        return round_index + 1
    if event_name in _ROUND_COUNT_EVENTS:
        # `rounds` is a cumulative exchange summary ("approved after 2 rounds"),
        # not a stable cycle identifier. Using it here incorrectly back-assigns
        # later review outcomes into earlier cycles after retries/restarts.
        return None
    return None


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
