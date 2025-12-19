"""Unit tests for the event system."""

import pytest
from datetime import datetime
from unittest.mock import MagicMock

from issue_orchestrator.domain.events import (
    Event,
    EventBus,
    IssueEvent,
    SessionEvent,
    ReviewEvent,
    LabelEvent,
)


class TestEvent:
    """Test the Event dataclass."""

    def test_event_creation_with_defaults(self):
        """Test creating an event with default values."""
        event = Event(
            event_type=IssueEvent.CLAIMED,
            entity_id=123
        )

        assert event.event_type == IssueEvent.CLAIMED
        assert event.entity_id == 123
        assert isinstance(event.timestamp, datetime)
        assert event.data == {}
        assert event.source == ""

    def test_event_creation_with_all_fields(self):
        """Test creating an event with all fields specified."""
        timestamp = datetime.now()
        data = {"branch": "issue-123", "user": "agent"}

        event = Event(
            event_type=SessionEvent.STARTED,
            entity_id=456,
            timestamp=timestamp,
            data=data,
            source="test_source"
        )

        assert event.event_type == SessionEvent.STARTED
        assert event.entity_id == 456
        assert event.timestamp == timestamp
        assert event.data == data
        assert event.source == "test_source"

    def test_event_is_immutable(self):
        """Test that events are immutable (frozen dataclass)."""
        event = Event(
            event_type=IssueEvent.CLAIMED,
            entity_id=123
        )

        with pytest.raises(Exception):  # FrozenInstanceError
            event.entity_id = 456

    def test_event_validates_event_type(self):
        """Test that invalid event types are rejected."""
        with pytest.raises(ValueError, match="event_type must be one of"):
            Event(
                event_type="invalid",
                entity_id=123
            )

    def test_event_accepts_all_event_enum_types(self):
        """Test that all valid event enum types are accepted."""
        # Should not raise
        Event(event_type=IssueEvent.CLAIMED, entity_id=1)
        Event(event_type=SessionEvent.STARTED, entity_id=2)
        Event(event_type=ReviewEvent.APPROVED, entity_id=3)
        Event(event_type=LabelEvent.ADDED, entity_id=4)


class TestEventBus:
    """Test the EventBus class."""

    @pytest.fixture
    def event_bus(self):
        """Create an EventBus for testing."""
        return EventBus(max_history=100)

    def test_eventbus_initialization(self):
        """Test EventBus initialization."""
        bus = EventBus(max_history=50)
        assert bus._max_history == 50
        assert len(bus._history) == 0
        assert len(bus._handlers) == 0

    def test_subscribe_handler(self, event_bus):
        """Test subscribing a handler to an event type."""
        handler = MagicMock(__name__="test_handler")

        event_bus.subscribe(IssueEvent.CLAIMED, handler)

        assert event_bus.get_handler_count(IssueEvent.CLAIMED) == 1

    def test_subscribe_multiple_handlers(self, event_bus):
        """Test subscribing multiple handlers to the same event type."""
        handler1 = MagicMock(__name__="handler1")
        handler2 = MagicMock(__name__="handler2")
        handler3 = MagicMock(__name__="handler3")

        event_bus.subscribe(IssueEvent.CLAIMED, handler1)
        event_bus.subscribe(IssueEvent.CLAIMED, handler2)
        event_bus.subscribe(IssueEvent.CLAIMED, handler3)

        assert event_bus.get_handler_count(IssueEvent.CLAIMED) == 3

    def test_subscribe_non_callable_raises_error(self, event_bus):
        """Test that subscribing a non-callable raises TypeError."""
        with pytest.raises(TypeError, match="Handler must be callable"):
            event_bus.subscribe(IssueEvent.CLAIMED, "not a function")

    def test_publish_event(self, event_bus):
        """Test publishing an event."""
        handler = MagicMock(__name__="test_handler")
        event_bus.subscribe(IssueEvent.CLAIMED, handler)

        event = event_bus.publish(
            IssueEvent.CLAIMED,
            entity_id=123,
            data={"branch": "issue-123"},
            source="test"
        )

        # Check event was created correctly
        assert event.event_type == IssueEvent.CLAIMED
        assert event.entity_id == 123
        assert event.data == {"branch": "issue-123"}
        assert event.source == "test"

        # Check handler was called
        handler.assert_called_once()
        call_event = handler.call_args[0][0]
        assert call_event.event_type == IssueEvent.CLAIMED
        assert call_event.entity_id == 123

    def test_publish_calls_all_handlers_in_order(self, event_bus):
        """Test that all handlers are called in subscription order."""
        call_order = []

        def handler1(event):
            call_order.append(1)

        def handler2(event):
            call_order.append(2)

        def handler3(event):
            call_order.append(3)

        event_bus.subscribe(IssueEvent.CLAIMED, handler1)
        event_bus.subscribe(IssueEvent.CLAIMED, handler2)
        event_bus.subscribe(IssueEvent.CLAIMED, handler3)

        event_bus.publish(IssueEvent.CLAIMED, entity_id=123)

        assert call_order == [1, 2, 3]

    def test_publish_handler_exception_doesnt_crash_bus(self, event_bus):
        """Test that handler exceptions don't prevent other handlers from running."""
        handler1 = MagicMock(__name__="handler1", side_effect=Exception("Handler 1 failed"))
        handler2 = MagicMock(__name__="handler2")
        handler3 = MagicMock(__name__="handler3", side_effect=ValueError("Handler 3 failed"))
        handler4 = MagicMock(__name__="handler4")

        event_bus.subscribe(IssueEvent.CLAIMED, handler1)
        event_bus.subscribe(IssueEvent.CLAIMED, handler2)
        event_bus.subscribe(IssueEvent.CLAIMED, handler3)
        event_bus.subscribe(IssueEvent.CLAIMED, handler4)

        # Should not raise, despite handlers failing
        event_bus.publish(IssueEvent.CLAIMED, entity_id=123)

        # All handlers should have been called
        assert handler1.called
        assert handler2.called
        assert handler3.called
        assert handler4.called

    def test_publish_only_notifies_relevant_handlers(self, event_bus):
        """Test that only handlers for the specific event type are notified."""
        claimed_handler = MagicMock(__name__="claimed_handler")
        started_handler = MagicMock(__name__="started_handler")

        event_bus.subscribe(IssueEvent.CLAIMED, claimed_handler)
        event_bus.subscribe(IssueEvent.SESSION_STARTED, started_handler)

        event_bus.publish(IssueEvent.CLAIMED, entity_id=123)

        claimed_handler.assert_called_once()
        started_handler.assert_not_called()

    def test_event_history_stores_events(self, event_bus):
        """Test that published events are stored in history."""
        event_bus.publish(IssueEvent.CLAIMED, entity_id=123)
        event_bus.publish(IssueEvent.SESSION_STARTED, entity_id=123)
        event_bus.publish(IssueEvent.COMPLETED, entity_id=123)

        history = event_bus.get_history()
        assert len(history) == 3

    def test_event_history_respects_max_size(self):
        """Test that event history is bounded by max_history."""
        bus = EventBus(max_history=3)

        bus.publish(IssueEvent.CLAIMED, entity_id=1)
        bus.publish(IssueEvent.SESSION_STARTED, entity_id=2)
        bus.publish(IssueEvent.COMPLETED, entity_id=3)
        bus.publish(IssueEvent.RELEASED, entity_id=4)

        history = bus.get_history()
        assert len(history) == 3
        # Oldest event (CLAIMED) should be dropped
        assert history[0].event_type == IssueEvent.RELEASED  # newest first
        assert history[2].event_type == IssueEvent.SESSION_STARTED

    def test_get_history_returns_newest_first(self, event_bus):
        """Test that history is returned in reverse chronological order."""
        event_bus.publish(IssueEvent.CLAIMED, entity_id=123)
        event_bus.publish(IssueEvent.SESSION_STARTED, entity_id=123)
        event_bus.publish(IssueEvent.COMPLETED, entity_id=123)

        history = event_bus.get_history()

        assert history[0].event_type == IssueEvent.COMPLETED
        assert history[1].event_type == IssueEvent.SESSION_STARTED
        assert history[2].event_type == IssueEvent.CLAIMED

    def test_get_history_filter_by_event_type(self, event_bus):
        """Test filtering history by event type."""
        event_bus.publish(IssueEvent.CLAIMED, entity_id=123)
        event_bus.publish(SessionEvent.STARTED, entity_id=456)
        event_bus.publish(IssueEvent.COMPLETED, entity_id=123)
        event_bus.publish(SessionEvent.COMPLETED, entity_id=456)

        issue_events = event_bus.get_history(event_type=IssueEvent.CLAIMED)

        assert len(issue_events) == 1
        assert issue_events[0].event_type == IssueEvent.CLAIMED

    def test_get_history_filter_by_entity_id(self, event_bus):
        """Test filtering history by entity ID."""
        event_bus.publish(IssueEvent.CLAIMED, entity_id=123)
        event_bus.publish(IssueEvent.CLAIMED, entity_id=456)
        event_bus.publish(IssueEvent.COMPLETED, entity_id=123)
        event_bus.publish(IssueEvent.COMPLETED, entity_id=456)

        events_123 = event_bus.get_history(entity_id=123)

        assert len(events_123) == 2
        assert all(e.entity_id == 123 for e in events_123)

    def test_get_history_filter_by_both(self, event_bus):
        """Test filtering history by both event type and entity ID."""
        event_bus.publish(IssueEvent.CLAIMED, entity_id=123)
        event_bus.publish(IssueEvent.CLAIMED, entity_id=456)
        event_bus.publish(IssueEvent.COMPLETED, entity_id=123)
        event_bus.publish(SessionEvent.STARTED, entity_id=123)

        events = event_bus.get_history(
            event_type=IssueEvent.CLAIMED,
            entity_id=123
        )

        assert len(events) == 1
        assert events[0].event_type == IssueEvent.CLAIMED
        assert events[0].entity_id == 123

    def test_get_history_with_limit(self, event_bus):
        """Test limiting the number of history results."""
        event_bus.publish(IssueEvent.CLAIMED, entity_id=123)
        event_bus.publish(IssueEvent.SESSION_STARTED, entity_id=123)
        event_bus.publish(IssueEvent.BLOCKED, entity_id=123)
        event_bus.publish(IssueEvent.UNBLOCKED, entity_id=123)
        event_bus.publish(IssueEvent.COMPLETED, entity_id=123)

        events = event_bus.get_history(limit=3)

        assert len(events) == 3
        # Should get the 3 newest events
        assert events[0].event_type == IssueEvent.COMPLETED
        assert events[1].event_type == IssueEvent.UNBLOCKED
        assert events[2].event_type == IssueEvent.BLOCKED

    def test_clear_history(self, event_bus):
        """Test clearing event history."""
        event_bus.publish(IssueEvent.CLAIMED, entity_id=123)
        event_bus.publish(IssueEvent.COMPLETED, entity_id=123)

        assert len(event_bus.get_history()) == 2

        event_bus.clear_history()

        assert len(event_bus.get_history()) == 0

    def test_unsubscribe_handler(self, event_bus):
        """Test unsubscribing a handler."""
        handler = MagicMock(__name__="test_handler")

        event_bus.subscribe(IssueEvent.CLAIMED, handler)
        assert event_bus.get_handler_count(IssueEvent.CLAIMED) == 1

        result = event_bus.unsubscribe(IssueEvent.CLAIMED, handler)

        assert result is True
        assert event_bus.get_handler_count(IssueEvent.CLAIMED) == 0

    def test_unsubscribe_nonexistent_handler(self, event_bus):
        """Test unsubscribing a handler that was never subscribed."""
        handler = MagicMock(__name__="test_handler")

        result = event_bus.unsubscribe(IssueEvent.CLAIMED, handler)

        assert result is False

    def test_unsubscribe_doesnt_affect_other_handlers(self, event_bus):
        """Test that unsubscribing one handler doesn't affect others."""
        handler1 = MagicMock(__name__="handler1")
        handler2 = MagicMock(__name__="handler2")
        handler3 = MagicMock(__name__="handler3")

        event_bus.subscribe(IssueEvent.CLAIMED, handler1)
        event_bus.subscribe(IssueEvent.CLAIMED, handler2)
        event_bus.subscribe(IssueEvent.CLAIMED, handler3)

        event_bus.unsubscribe(IssueEvent.CLAIMED, handler2)

        assert event_bus.get_handler_count(IssueEvent.CLAIMED) == 2

        event_bus.publish(IssueEvent.CLAIMED, entity_id=123)

        handler1.assert_called_once()
        handler2.assert_not_called()
        handler3.assert_called_once()

    def test_get_handler_count_no_handlers(self, event_bus):
        """Test getting handler count when no handlers are registered."""
        assert event_bus.get_handler_count(IssueEvent.CLAIMED) == 0

    def test_get_handler_count_total(self, event_bus):
        """Test getting total handler count across all event types."""
        handler1 = MagicMock(__name__="handler1")
        handler2 = MagicMock(__name__="handler2")
        handler3 = MagicMock(__name__="handler3")

        event_bus.subscribe(IssueEvent.CLAIMED, handler1)
        event_bus.subscribe(IssueEvent.CLAIMED, handler2)
        event_bus.subscribe(SessionEvent.STARTED, handler3)

        assert event_bus.get_handler_count() == 3

    def test_publish_with_no_data(self, event_bus):
        """Test publishing an event without optional data parameter."""
        handler = MagicMock(__name__="test_handler")
        event_bus.subscribe(IssueEvent.CLAIMED, handler)

        event = event_bus.publish(IssueEvent.CLAIMED, entity_id=123)

        assert event.data == {}
        handler.assert_called_once()

    def test_publish_returns_created_event(self, event_bus):
        """Test that publish returns the created event object."""
        event = event_bus.publish(
            IssueEvent.CLAIMED,
            entity_id=123,
            data={"test": "value"},
            source="test"
        )

        assert isinstance(event, Event)
        assert event.event_type == IssueEvent.CLAIMED
        assert event.entity_id == 123
        assert event.data == {"test": "value"}
        assert event.source == "test"

    def test_multiple_event_types(self, event_bus):
        """Test that the event bus handles multiple event enum types correctly."""
        issue_handler = MagicMock(__name__="issue_handler")
        session_handler = MagicMock(__name__="session_handler")
        review_handler = MagicMock(__name__="review_handler")

        event_bus.subscribe(IssueEvent.CLAIMED, issue_handler)
        event_bus.subscribe(SessionEvent.STARTED, session_handler)
        event_bus.subscribe(ReviewEvent.APPROVED, review_handler)

        event_bus.publish(IssueEvent.CLAIMED, entity_id=1)
        event_bus.publish(SessionEvent.STARTED, entity_id=2)
        event_bus.publish(ReviewEvent.APPROVED, entity_id=3)

        issue_handler.assert_called_once()
        session_handler.assert_called_once()
        review_handler.assert_called_once()

    def test_handler_receives_correct_event_data(self, event_bus):
        """Test that handlers receive events with all the correct data."""
        received_events = []

        def handler(event):
            received_events.append(event)

        event_bus.subscribe(IssueEvent.CLAIMED, handler)

        event_bus.publish(
            IssueEvent.CLAIMED,
            entity_id=123,
            data={"branch": "issue-123", "assignee": "agent"},
            source="orchestrator"
        )

        assert len(received_events) == 1
        event = received_events[0]
        assert event.event_type == IssueEvent.CLAIMED
        assert event.entity_id == 123
        assert event.data["branch"] == "issue-123"
        assert event.data["assignee"] == "agent"
        assert event.source == "orchestrator"
        assert isinstance(event.timestamp, datetime)
