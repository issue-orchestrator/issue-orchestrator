"""Unit tests for the event catalog and context system."""

from uuid import UUID

from issue_orchestrator.events import EventName, EventContext
from issue_orchestrator.events.catalog import EVENT_SCHEMA_VERSION
from issue_orchestrator.ports import TraceEvent, InMemoryEventSink


class TestEventName:
    """Test EventName enum constants."""

    def test_event_names_are_unique(self):
        """All event names should be unique."""
        values = [e.value for e in EventName]
        assert len(values) == len(set(values)), "Duplicate event name values found"

    def test_event_names_follow_domain_action_format(self):
        """All event names should follow domain.action format."""
        for event in EventName:
            assert "." in event.value, f"{event.name} does not follow domain.action format"
            parts = event.value.split(".")
            assert len(parts) == 2, f"{event.name} should have exactly one dot"
            domain, action = parts
            assert domain, f"{event.name} has empty domain"
            assert action, f"{event.name} has empty action"

    def test_str_returns_value(self):
        """str() should return the event name string."""
        assert str(EventName.TICK_STARTED) == "tick.started"
        assert str(EventName.ORCHESTRATOR_PAUSED) == "orchestrator.paused"

    def test_can_use_in_trace_event(self):
        """EventName should work directly in TraceEvent constructor."""
        event = TraceEvent(EventName.TICK_STARTED, {"tick_id": 1})
        assert event.name == "tick.started"


class TestEventContext:
    """Test EventContext for payload enrichment."""

    def test_creates_with_unique_run_id(self):
        """Each context should have a unique run_id."""
        ctx1 = EventContext()
        ctx2 = EventContext()
        assert ctx1.run_id != ctx2.run_id
        assert isinstance(ctx1.run_id, UUID)

    def test_starts_with_tick_id_zero(self):
        """tick_id should start at 0."""
        ctx = EventContext()
        assert ctx.tick_id == 0

    def test_enrich_adds_context_fields(self):
        """enrich() should add run_id, tick_id, and schema."""
        ctx = EventContext()
        ctx.tick_id = 5

        payload = ctx.enrich({"custom": "data"})

        assert payload["schema"] == EVENT_SCHEMA_VERSION
        assert payload["run_id"] == str(ctx.run_id)
        assert payload["tick_id"] == 5
        assert payload["custom"] == "data"

    def test_enrich_does_not_mutate_original(self):
        """enrich() should return new dict, not mutate input."""
        ctx = EventContext()
        original = {"key": "value"}

        result = ctx.enrich(original)

        assert "schema" in result
        assert "schema" not in original

    def test_for_issue_creates_issue_payload(self):
        """for_issue() should create payload with issue identifiers."""
        ctx = EventContext()
        ctx.tick_id = 3

        payload = ctx.for_issue("M1-011", issue_number=42)

        assert payload["issue_key"] == "M1-011"
        assert payload["issue_number"] == 42
        assert payload["tick_id"] == 3
        assert "run_id" in payload

    def test_for_issue_without_number(self):
        """for_issue() should work without issue_number."""
        ctx = EventContext()
        payload = ctx.for_issue("M1-011")

        assert payload["issue_key"] == "M1-011"
        assert "issue_number" not in payload

    def test_for_session_creates_session_payload(self):
        """for_session() should create payload with session identifiers."""
        ctx = EventContext()
        ctx.tick_id = 7

        payload = ctx.for_session("session-123", "M1-011", issue_number=42)

        assert payload["session_id"] == "session-123"
        assert payload["issue_key"] == "M1-011"
        assert payload["issue_number"] == 42
        assert payload["tick_id"] == 7


class TestInMemoryEventSink:
    """Test InMemoryEventSink for test utilities."""

    def test_collects_published_events(self):
        """publish() should store events for inspection."""
        sink = InMemoryEventSink()

        event1 = TraceEvent("tick.started", {"tick_id": 1})  # type: ignore - Union type narrowing limitation
        event2 = TraceEvent("tick.completed", {"tick_id": 1})  # type: ignore - Union type narrowing limitation
        sink.publish(event1)
        sink.publish(event2)

        assert len(sink) == 2
        assert sink.events[0] == event1
        assert sink.events[1] == event2

    def test_has_event_returns_true_when_present(self):
        """has_event() should return True if event was published."""
        sink = InMemoryEventSink()
        sink.publish(TraceEvent("tick.started", {}))  # type: ignore - Union type narrowing limitation
        assert sink.has_event("tick.started") is True
        assert sink.has_event("tick.completed") is False

    def test_get_events_filters_by_name(self):
        """get_events() should return only events with matching name."""
        sink = InMemoryEventSink()
        sink.publish(TraceEvent("tick.started", {"tick_id": 1}))  # type: ignore - Union type narrowing limitation
        sink.publish(TraceEvent("tick.completed", {"tick_id": 1}))  # type: ignore - Union type narrowing limitation
        sink.publish(TraceEvent("tick.started", {"tick_id": 2}))  # type: ignore - Union type narrowing limitation
        started = sink.get_events("tick.started")

        assert len(started) == 2
        assert all(e.name == "tick.started" for e in started)

    def test_last_event_returns_most_recent(self):
        """last_event() should return the most recent matching event."""
        sink = InMemoryEventSink()
        sink.publish(TraceEvent("tick.started", {"tick_id": 1}))  # type: ignore - Union type narrowing limitation
        sink.publish(TraceEvent("tick.completed", {"tick_id": 1}))  # type: ignore - Union type narrowing limitation
        sink.publish(TraceEvent("tick.started", {"tick_id": 2}))  # type: ignore - Union type narrowing limitation
        last = sink.last_event("tick.started")

        assert last is not None
        assert last.data["tick_id"] == 2

    def test_last_event_returns_none_when_not_found(self):
        """last_event() should return None if no matching event."""
        sink = InMemoryEventSink()
        sink.publish(TraceEvent("tick.started", {}))  # type: ignore - Union type narrowing limitation
        assert sink.last_event("tick.completed") is None

    def test_event_names_returns_ordered_list(self):
        """event_names() should return names in order."""
        sink = InMemoryEventSink()
        sink.publish(TraceEvent("orchestrator.started", {}))  # type: ignore - Union type narrowing limitation
        sink.publish(TraceEvent("tick.started", {}))  # type: ignore - Union type narrowing limitation
        sink.publish(TraceEvent("tick.completed", {}))  # type: ignore - Union type narrowing limitation
        names = sink.event_names()

        assert names == ["orchestrator.started", "tick.started", "tick.completed"]

    def test_clear_removes_all_events(self):
        """clear() should remove all collected events."""
        sink = InMemoryEventSink()
        sink.publish(TraceEvent("tick.started", {}))  # type: ignore - Union type narrowing limitation
        sink.publish(TraceEvent("tick.completed", {}))  # type: ignore - Union type narrowing limitation
        sink.clear()

        assert len(sink) == 0
        assert sink.events == []

    def test_events_returns_copy(self):
        """events property should return a copy, not the internal list."""
        sink = InMemoryEventSink()
        sink.publish(TraceEvent("tick.started", {}))  # type: ignore - Union type narrowing limitation
        events = sink.events
        events.clear()  # Modify the returned list

        assert len(sink) == 1  # Internal list unchanged


class TestEventSchemaVersion:
    """Test event schema versioning."""

    def test_schema_version_is_positive_integer(self):
        """Schema version should be a positive integer."""
        assert isinstance(EVENT_SCHEMA_VERSION, int)
        assert EVENT_SCHEMA_VERSION >= 1
