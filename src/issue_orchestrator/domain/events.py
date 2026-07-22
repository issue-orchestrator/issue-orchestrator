"""Event system for issue orchestrator state machine.

This module provides an event-driven architecture for tracking and responding to
state changes across the issue orchestration lifecycle.
"""

import logging
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


class IssueEvent(Enum):
    """Events that can occur during an issue's lifecycle."""

    CLAIMED = "claimed"
    SESSION_STARTED = "session_started"
    BLOCKED = "blocked"
    NEEDS_HUMAN = "needs_human"
    UNBLOCKED = "unblocked"
    PR_CREATED = "pr_created"
    PR_REJECTED = "pr_rejected"
    COMPLETED = "completed"
    RELEASED = "released"


class SessionEvent(Enum):
    """Events that can occur during a development session."""

    LAUNCHED = "launched"
    STARTED = "started"
    SLOW = "slow"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    BLOCKED = "blocked"
    NEEDS_HUMAN = "needs_human"


class ReviewEvent(Enum):
    """Events that can occur during code review and merge process."""

    REVIEW_STARTED = "review_started"
    APPROVED = "approved"
    CHANGES_REQUESTED = "changes_requested"
    REWORK_STARTED = "rework_started"
    REWORK_COMPLETED = "rework_completed"
    TECH_LEAD_REVIEW_STARTED = "tech_lead_review_started"
    TECH_LEAD_APPROVED = "tech_lead_approved"
    MERGED = "merged"
    CLOSED = "closed"
    ESCALATED = "escalated"  # Rework limit exceeded, requires human intervention


class LabelEvent(Enum):
    """Events for tracking label changes."""

    ADDED = "added"
    REMOVED = "removed"


@dataclass(frozen=True)
class Event:
    """Immutable event record.

    Events are the fundamental building blocks of the event-driven architecture.
    They capture what happened, when it happened, and any relevant context.

    Attributes:
        event_type: The type of event (from one of the event enums)
        entity_id: The ID of the entity this event pertains to (issue number, PR number, etc.)
        timestamp: When the event occurred (defaults to now)
        data: Additional context data for the event
        source: The component that originated this event
    """

    event_type: Enum
    entity_id: int
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    data: Dict[str, Any] = field(default_factory=dict)
    source: str = ""

    def __post_init__(self):
        """Validate event type."""
        if not isinstance(self.event_type, (IssueEvent, SessionEvent, ReviewEvent, LabelEvent)):
            raise ValueError(
                f"event_type must be one of IssueEvent, SessionEvent, ReviewEvent, or LabelEvent, "
                f"got {type(self.event_type)}"
            )


EventHandler = Callable[[Event], None]


class EventBus:
    """Central event bus for publishing and subscribing to events.

    The EventBus implements a simple pub/sub pattern that allows components to
    communicate without tight coupling. Components can subscribe to specific event
    types and will be notified when those events are published.

    Features:
    - Synchronous event delivery
    - Event history with configurable retention
    - Graceful error handling (failures in one handler don't affect others)
    - Comprehensive audit logging

    Thread safety: This implementation is NOT thread-safe. If used in a multi-threaded
    context, external synchronization is required.
    """

    def __init__(self, max_history: int = 1000):
        """Initialize the event bus.

        Args:
            max_history: Maximum number of events to retain in history (default: 1000)
        """
        self._handlers: Dict[Enum, List[EventHandler]] = defaultdict(list)
        self._history: deque[Event] = deque(maxlen=max_history)
        self._max_history = max_history
        logger.info(f"EventBus initialized with max_history={max_history}")

    def subscribe(self, event_type: Enum, handler: EventHandler) -> None:
        """Register a handler for a specific event type.

        Handlers are called synchronously in the order they were registered when
        an event of the subscribed type is published.

        Args:
            event_type: The type of event to subscribe to
            handler: Callable that takes an Event and returns None

        Example:
            def on_issue_claimed(event: Event):
                print(f"Issue {event.entity_id} was claimed")

            bus.subscribe(IssueEvent.CLAIMED, on_issue_claimed)
        """
        if not callable(handler):
            raise TypeError(f"Handler must be callable, got {type(handler)}")

        self._handlers[event_type].append(handler)
        logger.debug(f"Subscribed handler {handler.__name__} to {event_type}")

    def publish(
        self,
        event_type: Enum,
        entity_id: int,
        data: Optional[Dict[str, Any]] = None,
        source: str = ""
    ) -> Event:
        """Emit an event to all registered handlers.

        Creates an Event object and delivers it to all handlers subscribed to the
        event type. If any handler raises an exception, it is logged but does not
        prevent other handlers from running.

        Args:
            event_type: The type of event being published
            entity_id: ID of the entity this event pertains to
            data: Optional additional context data
            source: Optional identifier of the component publishing the event

        Returns:
            The Event object that was created and published

        Example:
            bus.publish(
                IssueEvent.CLAIMED,
                entity_id=123,
                data={"branch": "issue-123"},
                source="orchestrator"
            )
        """
        event = Event(
            event_type=event_type,
            entity_id=entity_id,
            timestamp=datetime.now(timezone.utc),
            data=data or {},
            source=source
        )

        # Add to history
        self._history.append(event)

        # Log the event
        logger.info(
            f"Event published: {event_type.value} for entity {entity_id} "
            f"from {source or 'unknown'}"
        )
        if event.data:
            logger.debug(f"Event data: {event.data}")

        # Notify all handlers
        handlers = self._handlers.get(event_type, [])
        for handler in handlers:
            try:
                handler(event)
            except Exception as e:
                logger.error(
                    f"Handler {handler.__name__} failed for event {event_type.value}: {e}",
                    exc_info=True
                )

        return event

    def get_history(
        self,
        event_type: Optional[Enum] = None,
        entity_id: Optional[int] = None,
        limit: Optional[int] = None
    ) -> List[Event]:
        """Query past events from the event history.

        Returns events in reverse chronological order (newest first).

        Args:
            event_type: Filter by event type (if None, returns all types)
            entity_id: Filter by entity ID (if None, returns all entities)
            limit: Maximum number of events to return (if None, returns all matching)

        Returns:
            List of Event objects matching the filters, newest first

        Example:
            # Get last 10 events for issue 123
            events = bus.get_history(entity_id=123, limit=10)

            # Get all CLAIMED events
            events = bus.get_history(event_type=IssueEvent.CLAIMED)
        """
        # Start with all history in reverse order (newest first)
        filtered = reversed(list(self._history))

        # Apply filters
        if event_type is not None:
            filtered = (e for e in filtered if e.event_type == event_type)

        if entity_id is not None:
            filtered = (e for e in filtered if e.entity_id == entity_id)

        # Apply limit and convert to list
        if limit is not None:
            filtered = list(filtered)[:limit]
        else:
            filtered = list(filtered)

        return filtered

    def clear_history(self) -> None:
        """Clear all event history.

        This is primarily useful for testing. In production, history is automatically
        bounded by the max_history parameter.
        """
        self._history.clear()
        logger.info("Event history cleared")

    def unsubscribe(self, event_type: Enum, handler: EventHandler) -> bool:
        """Unregister a handler for a specific event type.

        Args:
            event_type: The type of event to unsubscribe from
            handler: The handler to remove

        Returns:
            True if the handler was found and removed, False otherwise
        """
        handlers = self._handlers.get(event_type, [])
        try:
            handlers.remove(handler)
            logger.debug(f"Unsubscribed handler {handler.__name__} from {event_type}")
            return True
        except ValueError:
            logger.warning(
                f"Attempted to unsubscribe handler {handler.__name__} from {event_type}, "
                f"but it was not registered"
            )
            return False

    def get_handler_count(self, event_type: Optional[Enum] = None) -> int:
        """Get the number of handlers registered.

        Args:
            event_type: If provided, returns count for this event type only.
                       If None, returns total count across all event types.

        Returns:
            Number of registered handlers
        """
        if event_type is not None:
            return len(self._handlers.get(event_type, []))
        else:
            return sum(len(handlers) for handlers in self._handlers.values())
