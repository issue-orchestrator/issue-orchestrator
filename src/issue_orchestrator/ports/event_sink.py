"""Event sink port for trace event emission.

This port defines the interface for emitting trace/lifecycle events from the
orchestrator core. The core calls `publish()` without knowing how events
are delivered (pluggy, SSE, IPC, files, metrics, etc.).

This is the key abstraction that keeps pluggy out of the core.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, NotRequired, Protocol, TYPE_CHECKING, TypedDict, cast

if TYPE_CHECKING:
    from issue_orchestrator.events.catalog import EventName
    RunScopedEventName = Literal[
        EventName.SESSION_STARTED,
        EventName.SESSION_PROCESSING_COMPLETED,
        EventName.SESSION_VALIDATION_PASSED,
        EventName.SESSION_VALIDATION_RETRY_NEEDED,
        EventName.SESSION_VALIDATION_FAILED,
        EventName.REVIEW_STARTED,
        EventName.REWORK_STARTED,
    ]
else:
    RunScopedEventName = Any


class RunScopedEventPayload(TypedDict):
    issue_number: int
    run_dir: str
    session_name: NotRequired[str]
    session_id: NotRequired[str]
    pr_number: NotRequired[int]
    agent: NotRequired[str]
    task: NotRequired[str]
    worktree_path: NotRequired[str]
    branch_name: NotRequired[str]
    run_id: NotRequired[str]
    tick_id: NotRequired[int]
    schema: NotRequired[int]
    completion_path: NotRequired[str]
    completion_path_absolute: NotRequired[str]
    session_prompt_path: NotRequired[str | None]
    validation_cmd: NotRequired[str | None]
    error_file: NotRequired[str | None]
    retry_count: NotRequired[int]
    max_retries: NotRequired[int]
    success: NotRequired[bool]
    message: NotRequired[str]
    actions_taken: NotRequired[list[str] | None]
    errors: NotRequired[list[str] | None]
    pr_url: NotRequired[str | None]
    rework_cycle: NotRequired[int]
    review_exchange_mode: NotRequired[str]


def make_trace_event(
    event_type: "EventName",
    data: dict[str, Any],
) -> "TraceEvent":
    """Build a trace event through a central constructor."""
    return TraceEvent(event_type, dict(data))


def make_run_scoped_event(
    event_type: RunScopedEventName,
    data: RunScopedEventPayload,
) -> "TraceEvent":
    """Build a run-scoped event with typed payload requiring run_dir."""
    return TraceEvent(event_type, dict(data))


class SessionStartedEventPayload(TypedDict):
    """Typed payload for EventName.SESSION_STARTED."""

    issue_number: int
    session_id: str
    agent: str
    task: str
    worktree_path: str
    branch_name: str
    run_id: str
    run_dir: str
    completion_path: str
    completion_path_absolute: str
    session_prompt_path: NotRequired[str | None]


class SessionProcessingCompletedEventPayload(TypedDict):
    """Typed payload for EventName.SESSION_PROCESSING_COMPLETED."""

    issue_number: int
    session_name: str
    run_dir: str
    success: bool
    message: str
    actions_taken: list[str]
    errors: list[str]
    pr_url: str | None


class SessionValidationPassedEventPayload(TypedDict):
    """Typed payload for EventName.SESSION_VALIDATION_PASSED."""

    issue_number: int
    session_name: str
    run_dir: str
    validation_cmd: str | None


class SessionValidationRetryNeededEventPayload(TypedDict):
    """Typed payload for EventName.SESSION_VALIDATION_RETRY_NEEDED."""

    issue_number: int
    session_name: str
    run_dir: str
    validation_cmd: str | None
    retry_count: int
    max_retries: int
    error_file: NotRequired[str | None]


class SessionValidationFailedEventPayload(TypedDict):
    """Typed payload for EventName.SESSION_VALIDATION_FAILED."""

    issue_number: int
    session_name: str
    run_dir: str
    validation_cmd: str | None
    retry_count: int
    error_file: NotRequired[str | None]


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

    _RUN_DIR_REQUIRED_EVENTS: frozenset[str] = field(
        default=frozenset(
            {
                "session.started",
                "session.processing_completed",
                "session.validation_passed",
                "session.validation_retry_needed",
                "session.validation_failed",
                "review.started",
                "rework.started",
            }
        ),
        init=False,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        """Validate strict event invariants at construction time."""
        if self.name not in self._RUN_DIR_REQUIRED_EVENTS:
            return
        # Some non-issue-scoped helpers emit generic session events without issue_number.
        # Enforce run_dir only for issue-scoped timeline events.
        if not isinstance(self.data.get("issue_number"), int):
            return
        run_dir = self.data.get("run_dir")
        if not isinstance(run_dir, str) or not run_dir:
            raise ValueError(
                f"{self.name} requires non-empty run_dir in event data"
            )

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


def make_session_started_event(payload: SessionStartedEventPayload) -> TraceEvent:
    """Construct a typed session.started event."""
    from ..events import EventName

    return TraceEvent(EventName.SESSION_STARTED, cast(dict[str, Any], payload))


def make_session_processing_completed_event(payload: SessionProcessingCompletedEventPayload) -> TraceEvent:
    """Construct a typed session.processing_completed event."""
    from ..events import EventName

    return TraceEvent(EventName.SESSION_PROCESSING_COMPLETED, cast(dict[str, Any], payload))


def make_session_validation_passed_event(payload: SessionValidationPassedEventPayload) -> TraceEvent:
    """Construct a typed session.validation_passed event."""
    from ..events import EventName

    return TraceEvent(EventName.SESSION_VALIDATION_PASSED, cast(dict[str, Any], payload))


def make_session_validation_retry_needed_event(payload: SessionValidationRetryNeededEventPayload) -> TraceEvent:
    """Construct a typed session.validation_retry_needed event."""
    from ..events import EventName

    return TraceEvent(EventName.SESSION_VALIDATION_RETRY_NEEDED, cast(dict[str, Any], payload))


def make_session_validation_failed_event(payload: SessionValidationFailedEventPayload) -> TraceEvent:
    """Construct a typed session.validation_failed event."""
    from ..events import EventName

    return TraceEvent(EventName.SESSION_VALIDATION_FAILED, cast(dict[str, Any], payload))


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
