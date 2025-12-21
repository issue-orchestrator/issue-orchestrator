"""TransitionGuard - centralized state machine transition handling.

This module provides a guard wrapper around state machine triggers that:
1. Centralizes exception handling for invalid transitions
2. Emits trace events for all transitions (applied and rejected)
3. Returns typed results instead of raising exceptions
4. Provides a consistent interface for all state machine types

Usage:
    guard = TransitionGuard(events=event_sink)
    result = guard.try_trigger(issue_machine, "claim", data={"agent": "agent-1"})
    if not result.applied:
        logger.warning(f"Invalid transition: {result.error}")
"""

from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional

from transitions import MachineError

from ..ports import EventSink, TraceEvent


class TransitionResultType(Enum):
    """Outcome of a transition attempt."""

    APPLIED = "applied"
    INVALID = "invalid"
    ERROR = "error"


@dataclass(frozen=True)
class TransitionResult:
    """Result of a transition attempt.

    Attributes:
        result_type: Whether the transition was applied, invalid, or errored
        from_state: The state before the transition attempt
        to_state: The state after (if applied) or None
        trigger: The trigger that was attempted
        entity_type: Type of entity (issue, session, review)
        entity_id: ID of the entity (issue number, PR number, session name)
        error: Error message if transition failed
        data: Additional context data
    """

    result_type: TransitionResultType
    from_state: str
    to_state: Optional[str]
    trigger: str
    entity_type: str
    entity_id: str | int
    error: Optional[str] = None
    data: Optional[dict[str, Any]] = None

    @property
    def applied(self) -> bool:
        """Check if the transition was successfully applied."""
        return self.result_type == TransitionResultType.APPLIED


class TransitionGuard:
    """Centralized guard for state machine transitions.

    Provides fail-loud semantics with centralized error handling:
    - Invalid transitions don't crash the process
    - All transitions (applied or rejected) emit trace events
    - Returns typed results for callers to handle

    This implements the "fail loud but caught" pattern from the architecture:
    invalid transitions are anomalies that should be visible, not silently ignored.
    """

    def __init__(self, events: EventSink):
        """Initialize the guard with an event sink.

        Args:
            events: EventSink for emitting transition trace events
        """
        self.events = events

    def try_trigger(
        self,
        machine: Any,
        trigger: str,
        *,
        entity_type: str,
        entity_id: str | int,
        data: Optional[dict[str, Any]] = None,
    ) -> TransitionResult:
        """Attempt a state machine transition.

        Args:
            machine: The state machine instance (IssueStateMachine, etc.)
            trigger: Name of the trigger to fire (e.g., "claim", "start")
            entity_type: Type of entity for logging ("issue", "session", "review")
            entity_id: ID of the entity (issue number, session name, PR number)
            data: Optional data to pass to the trigger

        Returns:
            TransitionResult indicating success or failure with details
        """
        from_state = str(machine.state)

        # Check if transition is valid before attempting
        trigger_func = getattr(machine, trigger, None)
        may_trigger = getattr(machine, f"may_{trigger}", None)

        if trigger_func is None:
            result = TransitionResult(
                result_type=TransitionResultType.ERROR,
                from_state=from_state,
                to_state=None,
                trigger=trigger,
                entity_type=entity_type,
                entity_id=entity_id,
                error=f"Unknown trigger: {trigger}",
                data=data,
            )
            self._emit_rejected(result)
            return result

        if may_trigger is not None and not may_trigger():
            result = TransitionResult(
                result_type=TransitionResultType.INVALID,
                from_state=from_state,
                to_state=None,
                trigger=trigger,
                entity_type=entity_type,
                entity_id=entity_id,
                error=f"Transition '{trigger}' not valid from state '{from_state}'",
                data=data,
            )
            self._emit_rejected(result)
            return result

        # Attempt the transition
        try:
            if data:
                trigger_func(data=data)
            else:
                trigger_func()

            to_state = str(machine.state)
            result = TransitionResult(
                result_type=TransitionResultType.APPLIED,
                from_state=from_state,
                to_state=to_state,
                trigger=trigger,
                entity_type=entity_type,
                entity_id=entity_id,
                data=data,
            )
            self._emit_applied(result)
            return result

        except MachineError as e:
            # Transition was invalid (shouldn't happen if may_trigger passed)
            result = TransitionResult(
                result_type=TransitionResultType.INVALID,
                from_state=from_state,
                to_state=None,
                trigger=trigger,
                entity_type=entity_type,
                entity_id=entity_id,
                error=str(e),
                data=data,
            )
            self._emit_rejected(result)
            return result

        except Exception as e:
            # Unexpected error during transition
            result = TransitionResult(
                result_type=TransitionResultType.ERROR,
                from_state=from_state,
                to_state=None,
                trigger=trigger,
                entity_type=entity_type,
                entity_id=entity_id,
                error=f"Unexpected error: {e}",
                data=data,
            )
            self._emit_rejected(result)
            return result

    def _emit_applied(self, result: TransitionResult) -> None:
        """Emit a trace event for a successful transition."""
        event = TraceEvent(
            name="transition.applied",
            data={
                "entity_type": result.entity_type,
                "entity_id": result.entity_id,
                "trigger": result.trigger,
                "from_state": result.from_state,
                "to_state": result.to_state,
                **(result.data or {}),
            },
        )
        self.events.publish(event)

    def _emit_rejected(self, result: TransitionResult) -> None:
        """Emit a trace event for a rejected/failed transition."""
        event = TraceEvent(
            name="transition.rejected",
            data={
                "entity_type": result.entity_type,
                "entity_id": result.entity_id,
                "trigger": result.trigger,
                "from_state": result.from_state,
                "error": result.error,
                "result_type": result.result_type.value,
                **(result.data or {}),
            },
        )
        self.events.publish(event)
