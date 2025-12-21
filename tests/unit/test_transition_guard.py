"""Unit tests for the TransitionGuard module."""

import pytest
from unittest.mock import MagicMock

from issue_orchestrator.control.transition_guard import (
    TransitionGuard,
    TransitionResult,
    TransitionResultType,
)
from issue_orchestrator.ports import NullEventSink, TraceEvent


class CollectingEventSink:
    """Event sink that collects events for test assertions."""

    def __init__(self):
        self.events: list[TraceEvent] = []

    def publish(self, event: TraceEvent) -> None:
        self.events.append(event)


class MockStateMachine:
    """Mock state machine for testing transitions."""

    def __init__(self, initial_state: str = "available"):
        self.state = initial_state

    def may_claim(self) -> bool:
        return self.state == "available"

    def claim(self, data: dict | None = None):
        if not self.may_claim():
            from transitions import MachineError
            raise MachineError("Can't claim from this state")
        self.state = "claimed"

    def may_start(self) -> bool:
        return self.state == "claimed"

    def start(self, data: dict | None = None):
        if not self.may_start():
            from transitions import MachineError
            raise MachineError("Can't start from this state")
        self.state = "in_progress"


class TestTransitionGuard:
    """Test the TransitionGuard class."""

    def test_successful_transition_returns_applied(self):
        """Test that valid transitions return APPLIED result."""
        sink = CollectingEventSink()
        guard = TransitionGuard(events=sink)
        machine = MockStateMachine(initial_state="available")

        result = guard.try_trigger(
            machine, "claim", entity_type="issue", entity_id=123
        )

        assert result.applied
        assert result.result_type == TransitionResultType.APPLIED
        assert result.from_state == "available"
        assert result.to_state == "claimed"
        assert result.trigger == "claim"
        assert result.entity_type == "issue"
        assert result.entity_id == 123
        assert machine.state == "claimed"

    def test_successful_transition_emits_applied_event(self):
        """Test that valid transitions emit transition.applied event."""
        sink = CollectingEventSink()
        guard = TransitionGuard(events=sink)
        machine = MockStateMachine(initial_state="available")

        guard.try_trigger(machine, "claim", entity_type="issue", entity_id=123)

        assert len(sink.events) == 1
        event = sink.events[0]
        assert event.name == "transition.applied"
        assert event.data["entity_type"] == "issue"
        assert event.data["entity_id"] == 123
        assert event.data["trigger"] == "claim"
        assert event.data["from_state"] == "available"
        assert event.data["to_state"] == "claimed"

    def test_invalid_transition_returns_invalid(self):
        """Test that invalid transitions return INVALID result."""
        sink = CollectingEventSink()
        guard = TransitionGuard(events=sink)
        machine = MockStateMachine(initial_state="available")

        # Can't start from available state
        result = guard.try_trigger(
            machine, "start", entity_type="issue", entity_id=123
        )

        assert not result.applied
        assert result.result_type == TransitionResultType.INVALID
        assert result.from_state == "available"
        assert result.to_state is None
        assert "not valid" in result.error
        assert machine.state == "available"  # State unchanged

    def test_invalid_transition_emits_rejected_event(self):
        """Test that invalid transitions emit transition.rejected event."""
        sink = CollectingEventSink()
        guard = TransitionGuard(events=sink)
        machine = MockStateMachine(initial_state="available")

        guard.try_trigger(machine, "start", entity_type="issue", entity_id=123)

        assert len(sink.events) == 1
        event = sink.events[0]
        assert event.name == "transition.rejected"
        assert event.data["entity_type"] == "issue"
        assert event.data["entity_id"] == 123
        assert event.data["trigger"] == "start"
        assert event.data["from_state"] == "available"
        assert "error" in event.data
        assert event.data["result_type"] == "invalid"

    def test_unknown_trigger_returns_error(self):
        """Test that unknown triggers return ERROR result."""
        sink = CollectingEventSink()
        guard = TransitionGuard(events=sink)
        machine = MockStateMachine(initial_state="available")

        result = guard.try_trigger(
            machine, "nonexistent", entity_type="issue", entity_id=123
        )

        assert not result.applied
        assert result.result_type == TransitionResultType.ERROR
        assert "Unknown trigger" in result.error

    def test_transition_with_data_passes_data(self):
        """Test that data is passed to the transition callback."""
        sink = CollectingEventSink()
        guard = TransitionGuard(events=sink)
        machine = MockStateMachine(initial_state="available")

        result = guard.try_trigger(
            machine, "claim",
            entity_type="issue",
            entity_id=123,
            data={"agent": "agent-1"}
        )

        assert result.applied
        assert result.data == {"agent": "agent-1"}
        # Data should also be in the event
        event = sink.events[0]
        assert event.data.get("agent") == "agent-1"

    def test_chained_transitions(self):
        """Test multiple transitions in sequence."""
        sink = CollectingEventSink()
        guard = TransitionGuard(events=sink)
        machine = MockStateMachine(initial_state="available")

        # Claim
        result1 = guard.try_trigger(
            machine, "claim", entity_type="issue", entity_id=123
        )
        assert result1.applied
        assert machine.state == "claimed"

        # Start
        result2 = guard.try_trigger(
            machine, "start", entity_type="issue", entity_id=123
        )
        assert result2.applied
        assert machine.state == "in_progress"

        # Both events emitted
        assert len(sink.events) == 2
        assert sink.events[0].name == "transition.applied"
        assert sink.events[1].name == "transition.applied"

    def test_null_event_sink_works(self):
        """Test that NullEventSink doesn't cause errors."""
        guard = TransitionGuard(events=NullEventSink())
        machine = MockStateMachine(initial_state="available")

        result = guard.try_trigger(
            machine, "claim", entity_type="issue", entity_id=123
        )

        assert result.applied
        assert machine.state == "claimed"


class TestTransitionResult:
    """Test the TransitionResult dataclass."""

    def test_applied_property_true_when_applied(self):
        """Test applied property returns True for APPLIED results."""
        result = TransitionResult(
            result_type=TransitionResultType.APPLIED,
            from_state="available",
            to_state="claimed",
            trigger="claim",
            entity_type="issue",
            entity_id=123,
        )
        assert result.applied is True

    def test_applied_property_false_when_invalid(self):
        """Test applied property returns False for INVALID results."""
        result = TransitionResult(
            result_type=TransitionResultType.INVALID,
            from_state="available",
            to_state=None,
            trigger="start",
            entity_type="issue",
            entity_id=123,
            error="Not valid",
        )
        assert result.applied is False

    def test_applied_property_false_when_error(self):
        """Test applied property returns False for ERROR results."""
        result = TransitionResult(
            result_type=TransitionResultType.ERROR,
            from_state="available",
            to_state=None,
            trigger="unknown",
            entity_type="issue",
            entity_id=123,
            error="Unknown trigger",
        )
        assert result.applied is False

    def test_result_is_frozen(self):
        """Test that TransitionResult is immutable."""
        result = TransitionResult(
            result_type=TransitionResultType.APPLIED,
            from_state="available",
            to_state="claimed",
            trigger="claim",
            entity_type="issue",
            entity_id=123,
        )
        with pytest.raises(AttributeError):
            result.from_state = "other"  # Should fail - frozen
