"""Event sink port for trace event emission.

This port defines the interface for emitting trace/lifecycle events from the
orchestrator core. The core calls `publish()` without knowing how events
are delivered (pluggy, SSE, IPC, files, metrics, etc.).

This is the key abstraction that keeps pluggy out of the core.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, TYPE_CHECKING

if TYPE_CHECKING:
    from issue_orchestrator.events.catalog import EventName


@dataclass(frozen=True)
class TraceEvent:
    """A trace event emitted by the orchestrator.

    Trace events are notifications about what happened. They're fire-and-forget
    and must not influence orchestrator behavior.

    The event_type must be an EventName from the catalog - raw strings are not
    accepted. This ensures all events are documented and type-safe.

    Usage:
        from issue_orchestrator.events import EventName
        event = TraceEvent(EventName.TICK_STARTED, {"tick_id": 1})
    """

    event_type: "EventName"
    data: dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=datetime.now)
    event_id: int | None = None

    @property
    def name(self) -> str:
        """Get the event name string for serialization."""
        return str(self.event_type)

    def with_event_id(self, event_id: int) -> "TraceEvent":
        """Return a copy of this event with an assigned event_id."""
        return TraceEvent(
            event_type=self.event_type,
            data=dict(self.data),
            timestamp=self.timestamp,
            event_id=event_id,
        )


class EventSink(Protocol):
    """Port for emitting trace events.

    Implementations may fan out to multiple sinks (SSE, IPC, logging, metrics)
    but the orchestrator doesn't know or care about that.

    Contract:
        - publish() must not raise exceptions (fire-and-forget)
        - publish() must not block the caller
        - Events may be dropped if sinks are unavailable
    """

    def publish(self, event: TraceEvent) -> None:
        """Emit a trace event. Must not raise."""
        ...


class NullEventSink:
    """No-op event sink for testing or when events aren't needed."""

    def publish(self, event: TraceEvent) -> None:
        """Silently drop all events."""
        pass


class InMemoryEventSink:
    """Event sink that collects events in memory for testing.

    Provides methods to query and wait for specific events, enabling
    deterministic test synchronization without sleeps or timeouts.

    Usage:
        sink = InMemoryEventSink()
        orchestrator = Orchestrator(..., events=sink)

        # After some operation
        assert sink.has_event("tick.started")
        events = sink.get_events("session.completed")
        sink.clear()
    """

    def __init__(self) -> None:
        self._events: list[TraceEvent] = []

    def publish(self, event: TraceEvent) -> None:
        """Store the event for later inspection."""
        self._events.append(event)

    @property
    def events(self) -> list[TraceEvent]:
        """Get all collected events."""
        return list(self._events)

    def get_events(self, name: str) -> list[TraceEvent]:
        """Get all events with the given name."""
        return [e for e in self._events if e.name == name]

    def has_event(self, name: str) -> bool:
        """Check if an event with the given name was published."""
        return any(e.name == name for e in self._events)

    def last_event(self, name: str) -> TraceEvent | None:
        """Get the most recent event with the given name."""
        for e in reversed(self._events):
            if e.name == name:
                return e
        return None

    def event_names(self) -> list[str]:
        """Get list of all event names in order."""
        return [e.name for e in self._events]

    def clear(self) -> None:
        """Clear all collected events."""
        self._events.clear()

    def __len__(self) -> int:
        """Return number of collected events."""
        return len(self._events)
