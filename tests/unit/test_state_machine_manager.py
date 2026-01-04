"""Unit tests for StateMachineManager.

Tests focus on behavior-centric scenarios:
- Machine registration and lookup (get-or-create semantics)
- State persistence across lookups
- Terminal state replacement for session machines
- Error-free handling of missing machines
- Concurrent access patterns
"""

import pytest

from issue_orchestrator.control.state_machine_manager import StateMachineManager
from issue_orchestrator.domain.state_machines import (
    IssueStateMachine,
    IssueState,
    SessionStateMachine,
    SessionState,
    ReviewStateMachine,
    ReviewState,
)
from issue_orchestrator.domain.models import Issue
from issue_orchestrator.infra.config import Config
from issue_orchestrator.ports import NullEventSink


@pytest.fixture
def config() -> Config:
    """Create a minimal config for tests."""
    return Config(
        session_timeout_minutes=30,
        max_rework_cycles=3,
    )


@pytest.fixture
def events() -> NullEventSink:
    """Create a null event sink for tests."""
    return NullEventSink()


@pytest.fixture
def manager(config: Config, events: NullEventSink) -> StateMachineManager:
    """Create a StateMachineManager for tests."""
    return StateMachineManager(config=config, events=events)


@pytest.fixture
def sample_issue() -> Issue:
    """Create a sample Issue for tests."""
    return Issue(number=123, title="Test Issue", labels=["bug"])


class TestIssueMachineManagement:
    """Tests for issue state machine management."""

    def test_get_issue_machine_creates_new_machine_for_new_issue(
        self, manager: StateMachineManager, sample_issue: Issue
    ):
        """First lookup for an issue creates a new state machine."""
        machine = manager.get_issue_machine(sample_issue)

        assert isinstance(machine, IssueStateMachine)
        assert machine.issue_number == 123
        assert machine.get_state() == IssueState.AVAILABLE

    def test_get_issue_machine_returns_same_machine_on_subsequent_lookups(
        self, manager: StateMachineManager, sample_issue: Issue
    ):
        """Subsequent lookups return the same machine instance."""
        machine1 = manager.get_issue_machine(sample_issue)
        machine2 = manager.get_issue_machine(sample_issue)

        assert machine1 is machine2

    def test_state_changes_persist_across_lookups(
        self, manager: StateMachineManager, sample_issue: Issue
    ):
        """State changes made to a machine persist across lookups."""
        machine = manager.get_issue_machine(sample_issue)
        machine.claim()
        machine.start()

        # Look up the same machine again
        same_machine = manager.get_issue_machine(sample_issue)

        assert same_machine.get_state() == IssueState.IN_PROGRESS

    def test_different_issues_get_different_machines(
        self, manager: StateMachineManager
    ):
        """Different issue numbers get separate state machines."""
        issue1 = Issue(number=100, title="Issue 100", labels=[])
        issue2 = Issue(number=200, title="Issue 200", labels=[])

        machine1 = manager.get_issue_machine(issue1)
        machine2 = manager.get_issue_machine(issue2)

        assert machine1 is not machine2
        assert machine1.issue_number == 100
        assert machine2.issue_number == 200

    def test_has_issue_machine_returns_false_for_unknown_issue(
        self, manager: StateMachineManager
    ):
        """has_issue_machine returns False for issues not yet registered."""
        assert manager.has_issue_machine(999) is False

    def test_has_issue_machine_returns_true_after_get(
        self, manager: StateMachineManager, sample_issue: Issue
    ):
        """has_issue_machine returns True after a machine is created."""
        assert manager.has_issue_machine(sample_issue.number) is False

        manager.get_issue_machine(sample_issue)

        assert manager.has_issue_machine(sample_issue.number) is True

    def test_issue_machines_property_returns_all_machines(
        self, manager: StateMachineManager
    ):
        """issue_machines property provides access to all registered machines."""
        issue1 = Issue(number=1, title="Issue 1", labels=[])
        issue2 = Issue(number=2, title="Issue 2", labels=[])

        manager.get_issue_machine(issue1)
        manager.get_issue_machine(issue2)

        machines = manager.issue_machines
        assert len(machines) == 2
        assert 1 in machines
        assert 2 in machines


class TestSessionMachineManagement:
    """Tests for session state machine management."""

    def test_get_session_machine_creates_new_machine(
        self, manager: StateMachineManager
    ):
        """First lookup creates a new session state machine."""
        machine = manager.get_session_machine(
            session_name="session-123",
            issue_number=123,
        )

        assert isinstance(machine, SessionStateMachine)
        assert machine.session_id == "session-123"
        assert machine.issue_number == 123
        assert machine.get_state() == SessionState.PENDING

    def test_get_session_machine_uses_config_timeout_by_default(
        self, manager: StateMachineManager
    ):
        """Session machine uses config default timeout when none specified."""
        machine = manager.get_session_machine(
            session_name="session-123",
            issue_number=123,
        )

        assert machine.timeout_minutes == 30  # from config fixture

    def test_get_session_machine_accepts_custom_timeout(
        self, manager: StateMachineManager
    ):
        """Custom timeout overrides config default."""
        machine = manager.get_session_machine(
            session_name="session-123",
            issue_number=123,
            timeout_minutes=60,
        )

        assert machine.timeout_minutes == 60

    def test_get_session_machine_returns_same_machine_on_reuse(
        self, manager: StateMachineManager
    ):
        """Non-terminal sessions return the same machine instance."""
        machine1 = manager.get_session_machine("session-123", issue_number=1)
        machine1.launch()
        machine1.started()

        machine2 = manager.get_session_machine("session-123", issue_number=1)

        assert machine1 is machine2
        assert machine2.get_state() == SessionState.RUNNING

    def test_completed_session_is_replaced_with_fresh_machine(
        self, manager: StateMachineManager
    ):
        """Completed sessions are replaced with fresh machines."""
        # Create and complete a session
        original = manager.get_session_machine("session-123", issue_number=1)
        original.launch()
        original.started()
        original.complete()
        assert original.get_state() == SessionState.COMPLETED

        # Request the same session name again
        replacement = manager.get_session_machine("session-123", issue_number=1)

        assert replacement is not original
        assert replacement.get_state() == SessionState.PENDING

    def test_failed_session_is_replaced_with_fresh_machine(
        self, manager: StateMachineManager
    ):
        """Failed sessions are replaced with fresh machines."""
        # Create and fail a session
        original = manager.get_session_machine("session-123", issue_number=1)
        original.launch()
        original.fail()
        assert original.get_state() == SessionState.FAILED

        # Request the same session name again
        replacement = manager.get_session_machine("session-123", issue_number=1)

        assert replacement is not original
        assert replacement.get_state() == SessionState.PENDING

    def test_timed_out_session_is_replaced_with_fresh_machine(
        self, manager: StateMachineManager
    ):
        """Timed out sessions are replaced with fresh machines."""
        # Create and timeout a session
        original = manager.get_session_machine("session-123", issue_number=1)
        original.launch()
        original.started()
        original.timeout()
        assert original.get_state() == SessionState.TIMED_OUT

        # Request the same session name again
        replacement = manager.get_session_machine("session-123", issue_number=1)

        assert replacement is not original
        assert replacement.get_state() == SessionState.PENDING

    def test_blocked_session_is_not_replaced(
        self, manager: StateMachineManager
    ):
        """Non-terminal states like BLOCKED are not replaced."""
        original = manager.get_session_machine("session-123", issue_number=1)
        original.launch()
        original.started()
        original.block()
        assert original.get_state() == SessionState.BLOCKED

        # Request same session - should return existing blocked session
        same = manager.get_session_machine("session-123", issue_number=1)

        assert same is original
        assert same.get_state() == SessionState.BLOCKED

    def test_has_session_machine_returns_false_for_unknown(
        self, manager: StateMachineManager
    ):
        """has_session_machine returns False for unknown session names."""
        assert manager.has_session_machine("unknown-session") is False

    def test_has_session_machine_returns_true_after_creation(
        self, manager: StateMachineManager
    ):
        """has_session_machine returns True after machine is created."""
        assert manager.has_session_machine("session-123") is False

        manager.get_session_machine("session-123", issue_number=1)

        assert manager.has_session_machine("session-123") is True

    def test_remove_session_machine_removes_existing_machine(
        self, manager: StateMachineManager
    ):
        """remove_session_machine removes an existing machine."""
        manager.get_session_machine("session-123", issue_number=1)
        assert manager.has_session_machine("session-123") is True

        manager.remove_session_machine("session-123")

        assert manager.has_session_machine("session-123") is False

    def test_remove_session_machine_is_safe_for_nonexistent_session(
        self, manager: StateMachineManager
    ):
        """remove_session_machine handles nonexistent sessions gracefully."""
        # Should not raise
        manager.remove_session_machine("nonexistent-session")

        # Verify manager is still functional
        machine = manager.get_session_machine("new-session", issue_number=1)
        assert machine is not None

    def test_session_machines_property_returns_all_machines(
        self, manager: StateMachineManager
    ):
        """session_machines property provides access to all registered machines."""
        manager.get_session_machine("session-1", issue_number=1)
        manager.get_session_machine("session-2", issue_number=2)

        machines = manager.session_machines
        assert len(machines) == 2
        assert "session-1" in machines
        assert "session-2" in machines


class TestReviewMachineManagement:
    """Tests for review state machine management."""

    def test_get_review_machine_creates_new_machine(
        self, manager: StateMachineManager
    ):
        """First lookup creates a new review state machine."""
        machine = manager.get_review_machine(pr_number=456, issue_number=123)

        assert isinstance(machine, ReviewStateMachine)
        assert machine.pr_number == 456
        assert machine.issue_number == 123
        assert machine.get_state() == ReviewState.PENDING

    def test_get_review_machine_uses_config_max_rework_cycles(
        self, manager: StateMachineManager
    ):
        """Review machine uses config's max_rework_cycles setting."""
        machine = manager.get_review_machine(pr_number=456, issue_number=123)

        assert machine.max_rework_cycles == 3  # from config fixture

    def test_get_review_machine_returns_same_machine_on_reuse(
        self, manager: StateMachineManager
    ):
        """Subsequent lookups return the same review machine."""
        machine1 = manager.get_review_machine(pr_number=456, issue_number=123)
        machine1.start_review()

        machine2 = manager.get_review_machine(pr_number=456, issue_number=123)

        assert machine1 is machine2
        assert machine2.get_state() == ReviewState.IN_REVIEW

    def test_different_prs_get_different_machines(
        self, manager: StateMachineManager
    ):
        """Different PR numbers get separate state machines."""
        machine1 = manager.get_review_machine(pr_number=100, issue_number=1)
        machine2 = manager.get_review_machine(pr_number=200, issue_number=2)

        assert machine1 is not machine2
        assert machine1.pr_number == 100
        assert machine2.pr_number == 200

    def test_has_review_machine_returns_false_for_unknown_pr(
        self, manager: StateMachineManager
    ):
        """has_review_machine returns False for PRs not yet registered."""
        assert manager.has_review_machine(999) is False

    def test_has_review_machine_returns_true_after_creation(
        self, manager: StateMachineManager
    ):
        """has_review_machine returns True after machine is created."""
        assert manager.has_review_machine(456) is False

        manager.get_review_machine(pr_number=456, issue_number=123)

        assert manager.has_review_machine(456) is True

    def test_review_machines_property_returns_all_machines(
        self, manager: StateMachineManager
    ):
        """review_machines property provides access to all registered machines."""
        manager.get_review_machine(pr_number=1, issue_number=10)
        manager.get_review_machine(pr_number=2, issue_number=20)

        machines = manager.review_machines
        assert len(machines) == 2
        assert 1 in machines
        assert 2 in machines


class TestSequentialMultipleAccess:
    """Tests for multiple lookups of the same entity.

    Note: The StateMachineManager is designed for single-threaded use
    (Python's GIL provides some protection, but it's not thread-safe).
    These tests verify correct behavior under sequential access patterns.
    """

    def test_multiple_get_issue_machine_same_issue(
        self, manager: StateMachineManager
    ):
        """Multiple sequential requests for the same issue return consistent results."""
        issue = Issue(number=123, title="Test", labels=[])
        machines = []

        for _ in range(10):
            machines.append(manager.get_issue_machine(issue))

        # All should be the same instance
        assert all(m is machines[0] for m in machines)

    def test_multiple_get_session_machine_different_sessions(
        self, manager: StateMachineManager
    ):
        """Creating different sessions produces unique machines."""
        machines = []

        for i in range(10):
            machine = manager.get_session_machine(
                session_name=f"session-{i}",
                issue_number=i,
            )
            machines.append(machine)

        # All should be unique
        session_ids = [m.session_id for m in machines]
        assert len(set(session_ids)) == 10

    def test_multiple_get_review_machine_same_pr(
        self, manager: StateMachineManager
    ):
        """Multiple sequential requests for the same PR return consistent results."""
        machines = []

        for _ in range(10):
            machines.append(manager.get_review_machine(pr_number=123, issue_number=456))

        # All should be the same instance
        assert all(m is machines[0] for m in machines)


class TestManagerInitialization:
    """Tests for manager initialization and configuration."""

    def test_manager_initializes_with_empty_caches(
        self, config: Config, events: NullEventSink
    ):
        """New manager starts with no machines registered."""
        manager = StateMachineManager(config=config, events=events)

        assert len(manager.issue_machines) == 0
        assert len(manager.session_machines) == 0
        assert len(manager.review_machines) == 0

    def test_manager_stores_config_reference(
        self, config: Config, events: NullEventSink
    ):
        """Manager stores reference to config for use by machines."""
        manager = StateMachineManager(config=config, events=events)

        assert manager.config is config

    def test_manager_stores_events_reference(
        self, config: Config, events: NullEventSink
    ):
        """Manager stores reference to events sink."""
        manager = StateMachineManager(config=config, events=events)

        assert manager.events is events


class TestEdgeCases:
    """Tests for edge cases and boundary conditions."""

    def test_session_replacement_preserves_other_sessions(
        self, manager: StateMachineManager
    ):
        """Replacing a terminal session doesn't affect other sessions."""
        # Create two sessions
        session1 = manager.get_session_machine("session-1", issue_number=1)
        session2 = manager.get_session_machine("session-2", issue_number=2)

        session1.launch()
        session1.started()
        session2.launch()
        session2.started()

        # Complete session 1 only
        session1.complete()

        # Get replacement for session 1
        session1_new = manager.get_session_machine("session-1", issue_number=1)

        # Session 2 should be unchanged
        session2_lookup = manager.get_session_machine("session-2", issue_number=2)
        assert session2_lookup is session2
        assert session2_lookup.get_state() == SessionState.RUNNING

        # Session 1 should be new
        assert session1_new is not session1

    def test_issue_machine_with_different_issue_objects_same_number(
        self, manager: StateMachineManager
    ):
        """Same issue number returns same machine even with different Issue objects."""
        issue_v1 = Issue(number=123, title="Version 1", labels=["old"])
        issue_v2 = Issue(number=123, title="Version 2", labels=["new"])

        machine1 = manager.get_issue_machine(issue_v1)
        machine2 = manager.get_issue_machine(issue_v2)

        # Should be same machine since number matches
        assert machine1 is machine2

    def test_session_with_zero_timeout_uses_config(
        self, manager: StateMachineManager
    ):
        """Timeout of None uses config default."""
        machine = manager.get_session_machine(
            session_name="session-123",
            issue_number=123,
            timeout_minutes=None,
        )

        assert machine.timeout_minutes == 30  # config default
