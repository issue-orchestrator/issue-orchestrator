"""Unit tests for state machines."""

import pytest
from datetime import datetime, timedelta
from unittest.mock import MagicMock
from transitions import MachineError

from issue_orchestrator.domain.events import EventBus, IssueEvent, SessionEvent, ReviewEvent
from issue_orchestrator.domain.state_machines.issue_machine import (
    IssueStateMachine,
    IssueState,
)
from issue_orchestrator.domain.state_machines.session_machine import (
    SessionStateMachine,
    SessionState,
)
from issue_orchestrator.domain.state_machines.review_machine import (
    ReviewStateMachine,
    ReviewState,
)


@pytest.fixture
def event_bus():
    """Create an EventBus for testing."""
    return EventBus(max_history=100)


class TestIssueStateMachine:
    """Test the IssueStateMachine."""

    def test_initialization_default_state(self, event_bus):
        """Test state machine initializes with default AVAILABLE state."""
        machine = IssueStateMachine(issue_number=123, event_bus=event_bus)

        assert machine.issue_number == 123
        assert machine.get_state() == IssueState.AVAILABLE

    def test_initialization_custom_state(self, event_bus):
        """Test state machine initializes with custom initial state."""
        machine = IssueStateMachine(
            issue_number=123,
            event_bus=event_bus,
            initial_state=IssueState.IN_PROGRESS
        )

        assert machine.get_state() == IssueState.IN_PROGRESS

    def test_happy_path_complete_flow(self, event_bus):
        """Test the complete happy path: available -> claimed -> in_progress -> pr_pending -> completed."""
        machine = IssueStateMachine(issue_number=123, event_bus=event_bus)

        # Available -> Claimed
        machine.claim()
        assert machine.get_state() == IssueState.CLAIMED

        # Claimed -> In Progress
        machine.start()
        assert machine.get_state() == IssueState.IN_PROGRESS

        # In Progress -> PR Pending
        machine.pr_created()
        assert machine.get_state() == IssueState.PR_PENDING

        # PR Pending -> Completed
        machine.pr_merged()
        assert machine.get_state() == IssueState.COMPLETED

    def test_blocking_flow(self, event_bus):
        """Test blocking and unblocking an issue."""
        machine = IssueStateMachine(issue_number=123, event_bus=event_bus)

        machine.claim()
        machine.start()
        assert machine.get_state() == IssueState.IN_PROGRESS

        # Block the issue
        machine.block()
        assert machine.get_state() == IssueState.BLOCKED

        # Unblock back to in_progress
        machine.unblock()
        assert machine.get_state() == IssueState.IN_PROGRESS

    def test_needs_human_flow(self, event_bus):
        """Test needs_human flow."""
        machine = IssueStateMachine(issue_number=123, event_bus=event_bus)

        machine.claim()
        machine.start()
        assert machine.get_state() == IssueState.IN_PROGRESS

        # Mark as needs human
        machine.needs_human()
        assert machine.get_state() == IssueState.NEEDS_HUMAN

        # Unblock returns to in_progress
        machine.unblock()
        assert machine.get_state() == IssueState.IN_PROGRESS

    def test_release_from_claimed(self, event_bus):
        """Test releasing an issue from claimed state."""
        machine = IssueStateMachine(issue_number=123, event_bus=event_bus)

        machine.claim()
        assert machine.get_state() == IssueState.CLAIMED

        machine.release()
        assert machine.get_state() == IssueState.AVAILABLE

    def test_release_from_in_progress(self, event_bus):
        """Test releasing an issue from in_progress state."""
        machine = IssueStateMachine(issue_number=123, event_bus=event_bus)

        machine.claim()
        machine.start()
        assert machine.get_state() == IssueState.IN_PROGRESS

        machine.release()
        assert machine.get_state() == IssueState.AVAILABLE

    def test_release_from_blocked(self, event_bus):
        """Test releasing an issue from blocked state."""
        machine = IssueStateMachine(issue_number=123, event_bus=event_bus)

        machine.claim()
        machine.start()
        machine.block()
        assert machine.get_state() == IssueState.BLOCKED

        machine.release()
        assert machine.get_state() == IssueState.AVAILABLE

    def test_release_from_needs_human(self, event_bus):
        """Test releasing an issue from needs_human state."""
        machine = IssueStateMachine(issue_number=123, event_bus=event_bus)

        machine.claim()
        machine.start()
        machine.needs_human()
        assert machine.get_state() == IssueState.NEEDS_HUMAN

        machine.release()
        assert machine.get_state() == IssueState.AVAILABLE

    def test_pr_closed_returns_to_in_progress(self, event_bus):
        """Test that closing a PR returns to in_progress state."""
        machine = IssueStateMachine(issue_number=123, event_bus=event_bus)

        machine.claim()
        machine.start()
        machine.pr_created()
        assert machine.get_state() == IssueState.PR_PENDING

        machine.pr_closed()
        assert machine.get_state() == IssueState.IN_PROGRESS

    def test_invalid_transition_raises_error(self, event_bus):
        """Test that invalid transitions raise MachineError."""
        machine = IssueStateMachine(issue_number=123, event_bus=event_bus)

        # Can't start from available state
        with pytest.raises(MachineError):
            machine.start()

    def test_invalid_claim_from_in_progress(self, event_bus):
        """Test that claiming from in_progress state is invalid."""
        machine = IssueStateMachine(issue_number=123, event_bus=event_bus)

        machine.claim()
        machine.start()

        # Can't claim from in_progress
        with pytest.raises(MachineError):
            machine.claim()

    def test_cannot_release_from_completed(self, event_bus):
        """Test that releasing from completed state is invalid."""
        machine = IssueStateMachine(issue_number=123, event_bus=event_bus)

        machine.claim()
        machine.start()
        machine.pr_created()
        machine.pr_merged()
        assert machine.get_state() == IssueState.COMPLETED

        # Can't release from completed
        with pytest.raises(MachineError):
            machine.release()

    def test_cannot_release_from_pr_pending(self, event_bus):
        """Test that releasing from pr_pending state is invalid."""
        machine = IssueStateMachine(issue_number=123, event_bus=event_bus)

        machine.claim()
        machine.start()
        machine.pr_created()
        assert machine.get_state() == IssueState.PR_PENDING

        # Can't release from pr_pending
        with pytest.raises(MachineError):
            machine.release()

    def test_events_emitted_on_claim(self, event_bus):
        """Test that CLAIMED event is emitted when claiming."""
        machine = IssueStateMachine(issue_number=123, event_bus=event_bus)

        machine.claim(data={"agent": "web"})

        events = event_bus.get_history(event_type=IssueEvent.CLAIMED)
        assert len(events) == 1
        assert events[0].entity_id == 123
        assert events[0].data == {"agent": "web"}
        assert events[0].source == "IssueStateMachine"

    def test_events_emitted_on_start(self, event_bus):
        """Test that SESSION_STARTED event is emitted when starting."""
        machine = IssueStateMachine(issue_number=123, event_bus=event_bus)

        machine.claim()
        machine.start(data={"session_id": "session-123"})

        events = event_bus.get_history(event_type=IssueEvent.SESSION_STARTED)
        assert len(events) == 1
        assert events[0].entity_id == 123
        assert events[0].data == {"session_id": "session-123"}

    def test_events_emitted_on_block(self, event_bus):
        """Test that BLOCKED event is emitted when blocking."""
        machine = IssueStateMachine(issue_number=123, event_bus=event_bus)

        machine.claim()
        machine.start()
        machine.block(data={"reason": "waiting for dependency"})

        events = event_bus.get_history(event_type=IssueEvent.BLOCKED)
        assert len(events) == 1
        assert events[0].entity_id == 123
        assert events[0].data == {"reason": "waiting for dependency"}

    def test_events_emitted_on_needs_human(self, event_bus):
        """Test that NEEDS_HUMAN event is emitted."""
        machine = IssueStateMachine(issue_number=123, event_bus=event_bus)

        machine.claim()
        machine.start()
        machine.needs_human(data={"reason": "complex decision required"})

        events = event_bus.get_history(event_type=IssueEvent.NEEDS_HUMAN)
        assert len(events) == 1
        assert events[0].entity_id == 123
        assert events[0].data == {"reason": "complex decision required"}

    def test_events_emitted_on_unblock(self, event_bus):
        """Test that UNBLOCKED event is emitted when unblocking."""
        machine = IssueStateMachine(issue_number=123, event_bus=event_bus)

        machine.claim()
        machine.start()
        machine.block()
        machine.unblock(data={"resolved_by": "human"})

        events = event_bus.get_history(event_type=IssueEvent.UNBLOCKED)
        assert len(events) == 1
        assert events[0].entity_id == 123
        assert events[0].data == {"resolved_by": "human"}

    def test_events_emitted_on_pr_created(self, event_bus):
        """Test that PR_CREATED event is emitted when PR is created."""
        machine = IssueStateMachine(issue_number=123, event_bus=event_bus)

        machine.claim()
        machine.start()
        machine.pr_created(data={"pr_number": 456})

        events = event_bus.get_history(event_type=IssueEvent.PR_CREATED)
        assert len(events) == 1
        assert events[0].entity_id == 123
        assert events[0].data == {"pr_number": 456}

    def test_events_emitted_on_pr_rejected(self, event_bus):
        """Test that PR_REJECTED event is emitted when PR is closed."""
        machine = IssueStateMachine(issue_number=123, event_bus=event_bus)

        machine.claim()
        machine.start()
        machine.pr_created()
        machine.pr_closed(data={"reason": "changes requested"})

        events = event_bus.get_history(event_type=IssueEvent.PR_REJECTED)
        assert len(events) == 1
        assert events[0].entity_id == 123
        assert events[0].data == {"reason": "changes requested"}

    def test_events_emitted_on_completed(self, event_bus):
        """Test that COMPLETED event is emitted when PR is merged."""
        machine = IssueStateMachine(issue_number=123, event_bus=event_bus)

        machine.claim()
        machine.start()
        machine.pr_created()
        machine.pr_merged(data={"merged_by": "bot"})

        events = event_bus.get_history(event_type=IssueEvent.COMPLETED)
        assert len(events) == 1
        assert events[0].entity_id == 123
        assert events[0].data == {"merged_by": "bot"}

    def test_events_emitted_on_release(self, event_bus):
        """Test that RELEASED event is emitted when releasing."""
        machine = IssueStateMachine(issue_number=123, event_bus=event_bus)

        machine.claim()
        machine.release(data={"reason": "agent failed"})

        events = event_bus.get_history(event_type=IssueEvent.RELEASED)
        assert len(events) == 1
        assert events[0].entity_id == 123
        assert events[0].data == {"reason": "agent failed"}

    def test_can_transition_returns_true_for_valid(self, event_bus):
        """Test that can_transition returns True for valid transitions."""
        machine = IssueStateMachine(issue_number=123, event_bus=event_bus)

        assert machine.can_transition('claim') is True
        assert machine.can_transition('start') is False  # Not valid from AVAILABLE

        machine.claim()
        assert machine.can_transition('claim') is False
        assert machine.can_transition('start') is True

    def test_can_transition_returns_false_for_invalid(self, event_bus):
        """Test that can_transition returns False for invalid transitions."""
        machine = IssueStateMachine(issue_number=123, event_bus=event_bus)

        machine.claim()
        machine.start()
        machine.pr_created()

        # From PR_PENDING state, can't claim or start
        assert machine.can_transition('claim') is False
        assert machine.can_transition('start') is False
        # But can merge or close
        assert machine.can_transition('pr_merged') is True
        assert machine.can_transition('pr_closed') is True


class TestSessionStateMachine:
    """Test the SessionStateMachine."""

    def test_initialization_default_state(self, event_bus):
        """Test state machine initializes with default PENDING state."""
        machine = SessionStateMachine(
            session_id="session-123",
            issue_number=456,
            event_bus=event_bus
        )

        assert machine.session_id == "session-123"
        assert machine.issue_number == 456
        assert machine.get_state() == SessionState.PENDING

    def test_launch_to_started_to_running_to_completed(self, event_bus):
        """Test happy path: pending -> starting -> running -> completed."""
        machine = SessionStateMachine(
            session_id="session-123",
            issue_number=456,
            event_bus=event_bus
        )

        # Launch
        machine.launch()
        assert machine.get_state() == SessionState.STARTING

        # Started
        machine.started()
        assert machine.get_state() == SessionState.RUNNING
        assert machine.started_at is not None

        # Complete
        machine.complete()
        assert machine.get_state() == SessionState.COMPLETED

    def test_mark_slow(self, event_bus):
        """Test marking a running session as slow."""
        machine = SessionStateMachine(
            session_id="session-123",
            issue_number=456,
            event_bus=event_bus
        )

        machine.launch()
        machine.started()
        assert machine.get_state() == SessionState.RUNNING

        machine.mark_slow()
        assert machine.get_state() == SessionState.SLOW

    def test_complete_from_slow(self, event_bus):
        """Test completing a session from slow state."""
        machine = SessionStateMachine(
            session_id="session-123",
            issue_number=456,
            event_bus=event_bus
        )

        machine.launch()
        machine.started()
        machine.mark_slow()
        assert machine.get_state() == SessionState.SLOW

        machine.complete()
        assert machine.get_state() == SessionState.COMPLETED

    def test_fail_from_starting(self, event_bus):
        """Test session failing during startup."""
        machine = SessionStateMachine(
            session_id="session-123",
            issue_number=456,
            event_bus=event_bus
        )

        machine.launch()
        assert machine.get_state() == SessionState.STARTING

        machine.fail(data={"error": "failed to initialize"})
        assert machine.get_state() == SessionState.FAILED

    def test_fail_from_running(self, event_bus):
        """Test session failing while running."""
        machine = SessionStateMachine(
            session_id="session-123",
            issue_number=456,
            event_bus=event_bus
        )

        machine.launch()
        machine.started()
        assert machine.get_state() == SessionState.RUNNING

        machine.fail()
        assert machine.get_state() == SessionState.FAILED

    def test_fail_from_slow(self, event_bus):
        """Test session failing while slow."""
        machine = SessionStateMachine(
            session_id="session-123",
            issue_number=456,
            event_bus=event_bus
        )

        machine.launch()
        machine.started()
        machine.mark_slow()
        assert machine.get_state() == SessionState.SLOW

        machine.fail()
        assert machine.get_state() == SessionState.FAILED

    def test_timeout_from_running(self, event_bus):
        """Test session timing out while running."""
        machine = SessionStateMachine(
            session_id="session-123",
            issue_number=456,
            event_bus=event_bus
        )

        machine.launch()
        machine.started()

        machine.timeout()
        assert machine.get_state() == SessionState.TIMED_OUT

    def test_timeout_from_slow(self, event_bus):
        """Test session timing out while slow."""
        machine = SessionStateMachine(
            session_id="session-123",
            issue_number=456,
            event_bus=event_bus
        )

        machine.launch()
        machine.started()
        machine.mark_slow()

        machine.timeout()
        assert machine.get_state() == SessionState.TIMED_OUT

    def test_block_from_running(self, event_bus):
        """Test blocking a running session."""
        machine = SessionStateMachine(
            session_id="session-123",
            issue_number=456,
            event_bus=event_bus
        )

        machine.launch()
        machine.started()

        machine.block()
        assert machine.get_state() == SessionState.BLOCKED

    def test_needs_human_from_running(self, event_bus):
        """Test session needs human from running."""
        machine = SessionStateMachine(
            session_id="session-123",
            issue_number=456,
            event_bus=event_bus
        )

        machine.launch()
        machine.started()

        machine.needs_human()
        assert machine.get_state() == SessionState.NEEDS_HUMAN

    def test_resume_from_blocked(self, event_bus):
        """Test resuming from blocked state."""
        machine = SessionStateMachine(
            session_id="session-123",
            issue_number=456,
            event_bus=event_bus
        )

        machine.launch()
        machine.started()
        machine.block()
        assert machine.get_state() == SessionState.BLOCKED

        machine.resume()
        assert machine.get_state() == SessionState.RUNNING

    def test_resume_from_needs_human(self, event_bus):
        """Test resuming from needs_human state."""
        machine = SessionStateMachine(
            session_id="session-123",
            issue_number=456,
            event_bus=event_bus
        )

        machine.launch()
        machine.started()
        machine.needs_human()
        assert machine.get_state() == SessionState.NEEDS_HUMAN

        machine.resume()
        assert machine.get_state() == SessionState.RUNNING

    def test_check_timeout_no_timeout_configured(self, event_bus):
        """Test check_timeout when no timeout is configured."""
        machine = SessionStateMachine(
            session_id="session-123",
            issue_number=456,
            event_bus=event_bus,
            timeout_minutes=None
        )

        machine.launch()
        machine.started()

        result = machine.check_timeout()
        assert result is False
        assert machine.get_state() == SessionState.RUNNING

    def test_check_timeout_not_exceeded(self, event_bus):
        """Test check_timeout when timeout hasn't been exceeded."""
        machine = SessionStateMachine(
            session_id="session-123",
            issue_number=456,
            event_bus=event_bus,
            timeout_minutes=60
        )

        machine.launch()
        machine.started()
        # Just started, so shouldn't be timed out
        result = machine.check_timeout()
        assert result is False
        assert machine.get_state() == SessionState.RUNNING

    def test_check_timeout_exceeded(self, event_bus):
        """Test check_timeout when timeout has been exceeded."""
        machine = SessionStateMachine(
            session_id="session-123",
            issue_number=456,
            event_bus=event_bus,
            timeout_minutes=60
        )

        machine.launch()
        machine.started()

        # Manually set started_at to 61 minutes ago
        machine.started_at = datetime.now() - timedelta(minutes=61)

        result = machine.check_timeout()
        assert result is True
        assert machine.get_state() == SessionState.TIMED_OUT

    def test_check_timeout_only_affects_running_or_slow(self, event_bus):
        """Test that check_timeout only triggers for RUNNING or SLOW states."""
        machine = SessionStateMachine(
            session_id="session-123",
            issue_number=456,
            event_bus=event_bus,
            timeout_minutes=60
        )

        machine.launch()
        machine.started()
        machine.block()
        assert machine.get_state() == SessionState.BLOCKED

        # Set started_at to past timeout
        machine.started_at = datetime.now() - timedelta(minutes=61)

        # Should not timeout from BLOCKED state
        result = machine.check_timeout()
        assert result is False
        assert machine.get_state() == SessionState.BLOCKED

    def test_events_emitted_on_launched(self, event_bus):
        """Test that LAUNCHED event is emitted."""
        machine = SessionStateMachine(
            session_id="session-123",
            issue_number=456,
            event_bus=event_bus
        )

        machine.launch(data={"worktree": "/path/to/worktree"})

        events = event_bus.get_history(event_type=SessionEvent.LAUNCHED)
        assert len(events) == 1
        assert events[0].entity_id == 456
        assert events[0].data["session_id"] == "session-123"
        assert events[0].data["worktree"] == "/path/to/worktree"

    def test_events_emitted_on_started(self, event_bus):
        """Test that STARTED event is emitted with timestamp."""
        machine = SessionStateMachine(
            session_id="session-123",
            issue_number=456,
            event_bus=event_bus
        )

        machine.launch()
        machine.started()

        events = event_bus.get_history(event_type=SessionEvent.STARTED)
        assert len(events) == 1
        assert events[0].entity_id == 456
        assert events[0].data["session_id"] == "session-123"
        assert "started_at" in events[0].data

    def test_events_emitted_on_slow(self, event_bus):
        """Test that SLOW event is emitted with runtime."""
        machine = SessionStateMachine(
            session_id="session-123",
            issue_number=456,
            event_bus=event_bus
        )

        machine.launch()
        machine.started()
        machine.mark_slow()

        events = event_bus.get_history(event_type=SessionEvent.SLOW)
        assert len(events) == 1
        assert events[0].entity_id == 456
        assert events[0].data["session_id"] == "session-123"
        assert "runtime_minutes" in events[0].data

    def test_events_emitted_on_completed(self, event_bus):
        """Test that COMPLETED event is emitted with runtime."""
        machine = SessionStateMachine(
            session_id="session-123",
            issue_number=456,
            event_bus=event_bus
        )

        machine.launch()
        machine.started()
        machine.complete()

        events = event_bus.get_history(event_type=SessionEvent.COMPLETED)
        assert len(events) == 1
        assert events[0].entity_id == 456
        assert "runtime_minutes" in events[0].data

    def test_events_emitted_on_failed(self, event_bus):
        """Test that FAILED event is emitted."""
        machine = SessionStateMachine(
            session_id="session-123",
            issue_number=456,
            event_bus=event_bus
        )

        machine.launch()
        machine.fail(data={"error": "test error"})

        events = event_bus.get_history(event_type=SessionEvent.FAILED)
        assert len(events) == 1
        assert events[0].data["error"] == "test error"

    def test_events_emitted_on_timed_out(self, event_bus):
        """Test that TIMED_OUT event is emitted."""
        machine = SessionStateMachine(
            session_id="session-123",
            issue_number=456,
            event_bus=event_bus,
            timeout_minutes=60
        )

        machine.launch()
        machine.started()
        machine.started_at = datetime.now() - timedelta(minutes=61)
        machine.check_timeout()

        events = event_bus.get_history(event_type=SessionEvent.TIMED_OUT)
        assert len(events) == 1
        assert "runtime_minutes" in events[0].data
        assert "timeout_minutes" in events[0].data

    def test_events_emitted_on_blocked(self, event_bus):
        """Test that BLOCKED event is emitted."""
        machine = SessionStateMachine(
            session_id="session-123",
            issue_number=456,
            event_bus=event_bus
        )

        machine.launch()
        machine.started()
        machine.block()

        events = event_bus.get_history(event_type=SessionEvent.BLOCKED)
        assert len(events) == 1

    def test_events_emitted_on_needs_human(self, event_bus):
        """Test that NEEDS_HUMAN event is emitted."""
        machine = SessionStateMachine(
            session_id="session-123",
            issue_number=456,
            event_bus=event_bus
        )

        machine.launch()
        machine.started()
        machine.needs_human()

        events = event_bus.get_history(event_type=SessionEvent.NEEDS_HUMAN)
        assert len(events) == 1

    def test_get_runtime_info(self, event_bus):
        """Test getting runtime information."""
        machine = SessionStateMachine(
            session_id="session-123",
            issue_number=456,
            event_bus=event_bus,
            timeout_minutes=60
        )

        # Before starting
        info = machine.get_runtime_info()
        assert info["started_at"] is None
        assert info["runtime_minutes"] is None
        assert info["timeout_minutes"] == 60
        assert info["is_timed_out"] is False

        # After starting
        machine.launch()
        machine.started()

        info = machine.get_runtime_info()
        assert info["started_at"] is not None
        assert info["runtime_minutes"] is not None
        assert info["runtime_minutes"] >= 0
        assert info["timeout_minutes"] == 60
        assert info["is_timed_out"] is False

    def test_invalid_transitions_raise_error(self, event_bus):
        """Test that invalid transitions raise MachineError."""
        machine = SessionStateMachine(
            session_id="session-123",
            issue_number=456,
            event_bus=event_bus
        )

        # Can't start from PENDING
        with pytest.raises(MachineError):
            machine.started()

        # Can't complete from PENDING
        with pytest.raises(MachineError):
            machine.complete()


class TestReviewStateMachine:
    """Test the ReviewStateMachine."""

    def test_initialization_default_state(self, event_bus):
        """Test state machine initializes with default PENDING state."""
        machine = ReviewStateMachine(
            pr_number=123,
            issue_number=456,
            event_bus=event_bus
        )

        assert machine.pr_number == 123
        assert machine.issue_number == 456
        assert machine.get_state() == ReviewState.PENDING
        assert machine.rework_count == 0

    def test_approve_to_merge_flow(self, event_bus):
        """Test happy path: pending -> in_review -> approved -> merged."""
        machine = ReviewStateMachine(
            pr_number=123,
            issue_number=456,
            event_bus=event_bus
        )

        machine.start_review()
        assert machine.get_state() == ReviewState.IN_REVIEW

        machine.approve()
        assert machine.get_state() == ReviewState.APPROVED

        machine.merge()
        assert machine.get_state() == ReviewState.MERGED

    def test_changes_requested_rework_cycle(self, event_bus):
        """Test changes requested and rework cycle."""
        machine = ReviewStateMachine(
            pr_number=123,
            issue_number=456,
            event_bus=event_bus
        )

        machine.start_review()
        machine.request_changes()
        assert machine.get_state() == ReviewState.CHANGES_REQUESTED
        assert machine.rework_count == 1

        machine.queue_rework()
        assert machine.get_state() == ReviewState.REWORK_PENDING

        machine.start_rework()
        assert machine.get_state() == ReviewState.REWORK_IN_PROGRESS

        machine.complete_rework()
        assert machine.get_state() == ReviewState.IN_REVIEW

    def test_multiple_rework_cycles(self, event_bus):
        """Test multiple rework cycles increment count correctly."""
        machine = ReviewStateMachine(
            pr_number=123,
            issue_number=456,
            event_bus=event_bus
        )

        machine.start_review()

        # First rework cycle
        machine.request_changes()
        assert machine.rework_count == 1
        machine.queue_rework()
        machine.start_rework()
        machine.complete_rework()

        # Second rework cycle
        machine.request_changes()
        assert machine.rework_count == 2
        machine.queue_rework()
        machine.start_rework()
        machine.complete_rework()

        # Third rework cycle
        machine.request_changes()
        assert machine.rework_count == 3

    def test_cto_review_flow(self, event_bus):
        """Test triage review flow."""
        machine = ReviewStateMachine(
            pr_number=123,
            issue_number=456,
            event_bus=event_bus
        )

        machine.start_review()
        machine.approve()
        assert machine.get_state() == ReviewState.APPROVED

        machine.request_triage_review()
        assert machine.get_state() == ReviewState.TRIAGE_PENDING

        machine.triage_reviewed()
        assert machine.get_state() == ReviewState.TRIAGE_REVIEWED

        machine.merge()
        assert machine.get_state() == ReviewState.MERGED

    def test_cto_review_followed_by_changes_requested(self, event_bus):
        """Test changes requested after triage review."""
        machine = ReviewStateMachine(
            pr_number=123,
            issue_number=456,
            event_bus=event_bus
        )

        machine.start_review()
        machine.approve()
        machine.request_triage_review()
        machine.triage_reviewed()
        assert machine.get_state() == ReviewState.TRIAGE_REVIEWED

        # CTO can request changes
        machine.request_changes_after_triage()
        assert machine.get_state() == ReviewState.CHANGES_REQUESTED
        assert machine.rework_count == 1

    def test_max_rework_cycles_unlimited(self, event_bus):
        """Test that with no max_rework_cycles, rework is always allowed."""
        machine = ReviewStateMachine(
            pr_number=123,
            issue_number=456,
            event_bus=event_bus,
            max_rework_cycles=None
        )

        machine.start_review()

        # Do 10 rework cycles
        for i in range(10):
            machine.request_changes()
            assert machine.can_transition('queue_rework') is True
            machine.queue_rework()
            machine.start_rework()
            machine.complete_rework()

        assert machine.rework_count == 10
        assert machine.has_exceeded_rework_limit() is False

    def test_max_rework_cycles_enforced(self, event_bus):
        """Test that max_rework_cycles is enforced."""
        machine = ReviewStateMachine(
            pr_number=123,
            issue_number=456,
            event_bus=event_bus,
            max_rework_cycles=2
        )

        machine.start_review()

        # First rework cycle
        machine.request_changes()
        assert machine.can_transition('queue_rework') is True
        machine.queue_rework()
        machine.start_rework()
        machine.complete_rework()

        # Second rework cycle
        machine.request_changes()
        assert machine.can_transition('queue_rework') is True
        machine.queue_rework()
        machine.start_rework()
        machine.complete_rework()

        # Third rework cycle - should be blocked
        machine.request_changes()
        assert machine.rework_count == 3
        assert machine.can_transition('queue_rework') is False
        assert machine.has_exceeded_rework_limit() is True

        # Attempting queue_rework when conditions aren't met raises MachineError
        # The transition won't execute because _can_rework() condition returns False
        try:
            machine.queue_rework()
            # If it didn't raise, verify state didn't change
            assert machine.get_state() == ReviewState.CHANGES_REQUESTED
        except MachineError:
            # This is also acceptable - some versions of transitions raise on failed conditions
            pass

    def test_close_from_pending(self, event_bus):
        """Test closing from pending state."""
        machine = ReviewStateMachine(
            pr_number=123,
            issue_number=456,
            event_bus=event_bus
        )

        machine.close()
        assert machine.get_state() == ReviewState.CLOSED

    def test_close_from_in_review(self, event_bus):
        """Test closing from in_review state."""
        machine = ReviewStateMachine(
            pr_number=123,
            issue_number=456,
            event_bus=event_bus
        )

        machine.start_review()
        machine.close()
        assert machine.get_state() == ReviewState.CLOSED

    def test_close_from_approved(self, event_bus):
        """Test closing from approved state."""
        machine = ReviewStateMachine(
            pr_number=123,
            issue_number=456,
            event_bus=event_bus
        )

        machine.start_review()
        machine.approve()
        machine.close()
        assert machine.get_state() == ReviewState.CLOSED

    def test_close_from_rework_in_progress(self, event_bus):
        """Test closing from rework_in_progress state."""
        machine = ReviewStateMachine(
            pr_number=123,
            issue_number=456,
            event_bus=event_bus
        )

        machine.start_review()
        machine.request_changes()
        machine.queue_rework()
        machine.start_rework()
        machine.close()
        assert machine.get_state() == ReviewState.CLOSED

    def test_cannot_close_from_merged(self, event_bus):
        """Test that closing from merged state is invalid."""
        machine = ReviewStateMachine(
            pr_number=123,
            issue_number=456,
            event_bus=event_bus
        )

        machine.start_review()
        machine.approve()
        machine.merge()
        assert machine.get_state() == ReviewState.MERGED

        # Can't close from merged
        with pytest.raises(MachineError):
            machine.close()

    def test_events_emitted_on_review_started(self, event_bus):
        """Test that REVIEW_STARTED event is emitted."""
        machine = ReviewStateMachine(
            pr_number=123,
            issue_number=456,
            event_bus=event_bus
        )

        machine.start_review()

        events = event_bus.get_history(event_type=ReviewEvent.REVIEW_STARTED)
        assert len(events) == 1
        assert events[0].entity_id == 123
        assert events[0].data["issue_number"] == 456

    def test_events_emitted_on_approved(self, event_bus):
        """Test that APPROVED event is emitted."""
        machine = ReviewStateMachine(
            pr_number=123,
            issue_number=456,
            event_bus=event_bus
        )

        machine.start_review()
        machine.approve()

        events = event_bus.get_history(event_type=ReviewEvent.APPROVED)
        assert len(events) == 1
        assert events[0].entity_id == 123

    def test_events_emitted_on_changes_requested(self, event_bus):
        """Test that CHANGES_REQUESTED event is emitted with rework count."""
        machine = ReviewStateMachine(
            pr_number=123,
            issue_number=456,
            event_bus=event_bus
        )

        machine.start_review()
        machine.request_changes()

        events = event_bus.get_history(event_type=ReviewEvent.CHANGES_REQUESTED)
        assert len(events) == 1
        assert events[0].entity_id == 123
        assert events[0].data["rework_count"] == 1

    def test_events_emitted_on_rework_started(self, event_bus):
        """Test that REWORK_STARTED event is emitted."""
        machine = ReviewStateMachine(
            pr_number=123,
            issue_number=456,
            event_bus=event_bus
        )

        machine.start_review()
        machine.request_changes()
        machine.queue_rework()
        machine.start_rework()

        events = event_bus.get_history(event_type=ReviewEvent.REWORK_STARTED)
        assert len(events) == 1
        assert events[0].data["rework_count"] == 1

    def test_events_emitted_on_rework_completed(self, event_bus):
        """Test that REWORK_COMPLETED event is emitted."""
        machine = ReviewStateMachine(
            pr_number=123,
            issue_number=456,
            event_bus=event_bus
        )

        machine.start_review()
        machine.request_changes()
        machine.queue_rework()
        machine.start_rework()
        machine.complete_rework()

        events = event_bus.get_history(event_type=ReviewEvent.REWORK_COMPLETED)
        assert len(events) == 1

    def test_events_emitted_on_triage_review_started(self, event_bus):
        """Test that TRIAGE_REVIEW_STARTED event is emitted."""
        machine = ReviewStateMachine(
            pr_number=123,
            issue_number=456,
            event_bus=event_bus
        )

        machine.start_review()
        machine.approve()
        machine.request_triage_review()

        events = event_bus.get_history(event_type=ReviewEvent.TRIAGE_REVIEW_STARTED)
        assert len(events) == 1

    def test_events_emitted_on_triage_approved(self, event_bus):
        """Test that TRIAGE_APPROVED event is emitted."""
        machine = ReviewStateMachine(
            pr_number=123,
            issue_number=456,
            event_bus=event_bus
        )

        machine.start_review()
        machine.approve()
        machine.request_triage_review()
        machine.triage_reviewed()

        events = event_bus.get_history(event_type=ReviewEvent.TRIAGE_APPROVED)
        assert len(events) == 1

    def test_events_emitted_on_merged(self, event_bus):
        """Test that MERGED event is emitted with rework count."""
        machine = ReviewStateMachine(
            pr_number=123,
            issue_number=456,
            event_bus=event_bus
        )

        machine.start_review()
        machine.request_changes()
        machine.queue_rework()
        machine.start_rework()
        machine.complete_rework()
        machine.approve()
        machine.merge()

        events = event_bus.get_history(event_type=ReviewEvent.MERGED)
        assert len(events) == 1
        assert events[0].data["rework_count"] == 1

    def test_events_emitted_on_closed(self, event_bus):
        """Test that CLOSED event is emitted."""
        machine = ReviewStateMachine(
            pr_number=123,
            issue_number=456,
            event_bus=event_bus
        )

        machine.start_review()
        machine.close()

        events = event_bus.get_history(event_type=ReviewEvent.CLOSED)
        assert len(events) == 1

    def test_get_rework_info(self, event_bus):
        """Test getting rework information."""
        machine = ReviewStateMachine(
            pr_number=123,
            issue_number=456,
            event_bus=event_bus,
            max_rework_cycles=2
        )

        # Initially
        info = machine.get_rework_info()
        assert info["rework_count"] == 0
        assert info["max_rework_cycles"] == 2
        assert info["can_rework"] is True

        # After first rework
        machine.start_review()
        machine.request_changes()

        info = machine.get_rework_info()
        assert info["rework_count"] == 1
        assert info["can_rework"] is True

        # After second rework
        machine.queue_rework()
        machine.start_rework()
        machine.complete_rework()
        machine.request_changes()

        info = machine.get_rework_info()
        assert info["rework_count"] == 2
        assert info["can_rework"] is False

    def test_can_transition_validates_rework_limit(self, event_bus):
        """Test that can_transition respects rework limit."""
        machine = ReviewStateMachine(
            pr_number=123,
            issue_number=456,
            event_bus=event_bus,
            max_rework_cycles=1
        )

        machine.start_review()
        machine.request_changes()
        assert machine.can_transition('queue_rework') is True

        machine.queue_rework()
        machine.start_rework()
        machine.complete_rework()

        # Second rework cycle - should fail limit check
        machine.request_changes()
        assert machine.rework_count == 2
        assert machine.can_transition('queue_rework') is False

    def test_escalate_when_rework_limit_exceeded(self, event_bus):
        """Test explicit escalation when rework limit is exceeded."""
        machine = ReviewStateMachine(
            pr_number=123,
            issue_number=456,
            event_bus=event_bus,
            max_rework_cycles=2
        )

        machine.start_review()

        # Do 2 rework cycles (max allowed)
        for _ in range(2):
            machine.request_changes()
            machine.queue_rework()
            machine.start_rework()
            machine.complete_rework()

        # Third changes_requested exceeds limit
        machine.request_changes()
        assert machine.rework_count == 3
        assert machine.has_exceeded_rework_limit() is True
        assert machine.get_state() == ReviewState.CHANGES_REQUESTED

        # Cannot queue_rework anymore
        assert machine.can_transition('queue_rework') is False

        # But CAN escalate
        assert machine.can_transition('escalate') is True
        machine.escalate()
        assert machine.get_state() == ReviewState.ESCALATED

    def test_escalate_emits_event(self, event_bus):
        """Test that ESCALATED event is emitted with full context."""
        machine = ReviewStateMachine(
            pr_number=123,
            issue_number=456,
            event_bus=event_bus,
            max_rework_cycles=1
        )

        machine.start_review()
        machine.request_changes()
        machine.queue_rework()
        machine.start_rework()
        machine.complete_rework()

        # Second changes_requested exceeds limit
        machine.request_changes()
        machine.escalate()

        events = event_bus.get_history(event_type=ReviewEvent.ESCALATED)
        assert len(events) == 1
        assert events[0].entity_id == 123
        assert events[0].data["issue_number"] == 456
        assert events[0].data["rework_count"] == 2
        assert events[0].data["max_rework_cycles"] == 1

    def test_cannot_close_from_escalated(self, event_bus):
        """Test that escalated state cannot be closed (needs human resolution)."""
        machine = ReviewStateMachine(
            pr_number=123,
            issue_number=456,
            event_bus=event_bus,
            max_rework_cycles=1
        )

        machine.start_review()
        machine.request_changes()
        machine.queue_rework()
        machine.start_rework()
        machine.complete_rework()
        machine.request_changes()
        machine.escalate()

        assert machine.get_state() == ReviewState.ESCALATED

        # Cannot close from escalated - needs human intervention
        with pytest.raises(MachineError):
            machine.close()

    def test_escalated_state_is_terminal(self, event_bus):
        """Test that ESCALATED is a terminal state requiring human intervention."""
        machine = ReviewStateMachine(
            pr_number=123,
            issue_number=456,
            event_bus=event_bus,
            max_rework_cycles=1
        )

        machine.start_review()
        machine.request_changes()
        machine.queue_rework()
        machine.start_rework()
        machine.complete_rework()
        machine.request_changes()
        machine.escalate()

        # Cannot transition out of escalated state automatically
        assert machine.can_transition('queue_rework') is False
        assert machine.can_transition('start_review') is False
        assert machine.can_transition('approve') is False
        assert machine.can_transition('merge') is False

    def test_invalid_transitions_raise_error(self, event_bus):
        """Test that invalid transitions raise MachineError."""
        machine = ReviewStateMachine(
            pr_number=123,
            issue_number=456,
            event_bus=event_bus
        )

        # Can't approve from PENDING
        with pytest.raises(MachineError):
            machine.approve()

        # Can't merge from PENDING
        with pytest.raises(MachineError):
            machine.merge()
