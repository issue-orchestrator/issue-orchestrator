"""Transition result for state machine operations.

State machines return TransitionResult instead of publishing events directly.
The caller (control layer) is responsible for emitting TraceEvents via EventSink.
This keeps state machines pure and decoupled from the event infrastructure.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Optional


@dataclass(frozen=True)
class TransitionResult:
    """Result of a state machine transition.

    Returned by state machine transition methods to inform callers about
    what happened. Callers can then emit appropriate TraceEvents.

    Attributes:
        success: Whether the transition succeeded
        from_state: State before transition (as string)
        to_state: State after transition (as string)
        event_name: Suggested TraceEvent name (e.g., "session.started")
        entity_id: ID of the entity (issue number, PR number, etc.)
        data: Additional context data for the event
        timestamp: When the transition occurred
    """

    success: bool
    from_state: str
    to_state: str
    event_name: str  # Format: "domain.action" for TraceEvent
    entity_id: int
    data: dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=datetime.now)

    @property
    def trace_event_name(self) -> str:
        """Get the TraceEvent name for this transition."""
        return self.event_name


@dataclass(frozen=True)
class TransitionError:
    """Error information when a transition fails."""

    reason: str
    from_state: str
    attempted_trigger: str
