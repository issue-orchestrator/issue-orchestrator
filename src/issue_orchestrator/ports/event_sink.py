"""Event sink port for trace event emission.

This port defines the interface for emitting trace/lifecycle events from the
orchestrator core. The core calls `publish()` without knowing how events
are delivered (pluggy, SSE, IPC, files, metrics, etc.).

This is the key abstraction that keeps pluggy out of the core.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol


@dataclass(frozen=True)
class TraceEvent:
    """A trace event emitted by the orchestrator.

    Trace events are notifications about what happened. They're fire-and-forget
    and must not influence orchestrator behavior.

    Event naming convention: {domain}.{action}
        - session.started, session.completed, session.failed
        - issue.claimed, issue.blocked, issue.needs_human
        - pr.created
        - review.approved, review.changes_requested, review.escalated
        - orchestrator.ready, orchestrator.paused, orchestrator.resumed
    """

    name: str
    data: dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=datetime.now)

    def __post_init__(self):
        # Validate event name format
        if "." not in self.name:
            raise ValueError(f"Event name must be domain.action format: {self.name}")


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
