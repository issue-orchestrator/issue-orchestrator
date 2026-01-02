"""Unit tests for the observer module."""

import pytest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch, call
from issue_orchestrator.observation.observer import SessionObserver
from issue_orchestrator.models import (
    Session,
    SessionStatus,
    Issue,
    AgentConfig,
)
from issue_orchestrator.config import Config
from issue_orchestrator.ports import PRInfo
from issue_orchestrator.domain.issue_key import FakeIssueKey
from issue_orchestrator.domain.session_key import SessionKey, TaskKind


@pytest.fixture
def mock_config():
    """Create a mock config for testing."""
    config = Config()
    config.repo = "owner/repo"
    config.max_concurrent_sessions = 3
    config.session_timeout_minutes = 45
    config.label_in_progress = "in-progress"
    config.label_blocked = "blocked"
    config.label_needs_human = "needs-human"
    config.ui_mode = "tmux"
    config.close_completed_tabs = True
    config.close_failed_tabs = False
    return config


@pytest.fixture
def mock_session_runner():
    """Create a mock SessionRunner port."""
    runner = MagicMock()
    runner.session_exists.return_value = False
    runner.session_exists_by_name.return_value = False
    runner.kill_session.return_value = None
    runner.send_to_session.return_value = False
    runner.send_to_session_by_name.return_value = False
    return runner


@pytest.fixture
def mock_repository_host():
    """Create a mock RepositoryHost port."""
    host = MagicMock()
    host.get_prs_for_branch.return_value = []
    host.get_issue_labels.return_value = []
    host.add_label.return_value = None
    host.remove_label.return_value = None
    return host


@pytest.fixture
def monitor(mock_config, mock_session_runner, mock_repository_host):
    """Create a SessionObserver instance for testing."""
    return SessionObserver(
        mock_config,
        session_runner=mock_session_runner,
        repository_host=mock_repository_host,
    )


@pytest.fixture
def monitor_with_machines(mock_config, mock_session_runner, mock_repository_host):
    """Create a SessionObserver instance with mock session machines for testing."""
    return SessionObserver(
        mock_config,
        session_machines={},
        session_runner=mock_session_runner,
        repository_host=mock_repository_host,
    )


@pytest.fixture
def sample_session(sample_agent_config, tmp_path):
    """Create a sample session for testing."""
    issue = Issue(
        number=123,
        title="Test Issue",
        labels=["agent:web"],
        body="Test body",
    )
    issue_key = FakeIssueKey(name="123")
    session_key = SessionKey(issue=issue_key, task=TaskKind.CODE)
    return Session(
        key=session_key,
        issue=issue,
        agent_config=sample_agent_config,
        terminal_id="issue-123",
        worktree_path=tmp_path / "worktree",
        branch_name="issue-123-test",
    )


class TestSessionObserverInit:
    """Test SessionObserver initialization."""

    def test_init_with_config(self, mock_config):
        """Test initializing monitor with config."""
        monitor = SessionObserver(mock_config)
        assert monitor.config == mock_config
        assert monitor.session_machines == {}

    def test_init_stores_config(self, mock_config):
        """Test that config is properly stored."""
        monitor = SessionObserver(mock_config)
        assert monitor.config.repo == "owner/repo"
        assert monitor.config.max_concurrent_sessions == 3

    def test_init_with_session_machines(self, mock_config):
        """Test initializing monitor with session machines."""
        machines = {"issue-1": MagicMock(), "issue-2": MagicMock()}
        monitor = SessionObserver(mock_config, session_machines=machines)
        assert monitor.config == mock_config
        assert monitor.session_machines == machines

    def test_init_session_machines_defaults_to_empty_dict(self, mock_config):
        """Test that session_machines defaults to empty dict when None."""
        monitor = SessionObserver(mock_config, session_machines=None)
        assert monitor.session_machines == {}

    def test_init_with_ports(self, mock_config, mock_session_runner, mock_repository_host):
        """Test initializing monitor with ports."""
        monitor = SessionObserver(
            mock_config,
            session_runner=mock_session_runner,
            repository_host=mock_repository_host,
        )
        assert monitor._session_runner == mock_session_runner
        assert monitor._repository_host == mock_repository_host


class TestSessionObserverBackends:
    """Test backend delegation methods."""

    def test_session_exists_uses_runner(self, mock_config, mock_session_runner, mock_repository_host):
        """Test _session_exists delegates to session runner."""
        mock_session_runner.session_exists.return_value = True
        monitor = SessionObserver(
            mock_config,
            session_runner=mock_session_runner,
            repository_host=mock_repository_host,
        )

        result = monitor._session_exists(123)

        assert result is True
        mock_session_runner.session_exists.assert_called_once_with(123)

    def test_session_exists_returns_false_without_runner(self, mock_config):
        """Test _session_exists returns False when no runner."""
        monitor = SessionObserver(mock_config)

        result = monitor._session_exists(123)

        assert result is False

    def test_kill_session_uses_runner(self, mock_config, mock_session_runner, mock_repository_host):
        """Test _kill_session delegates to session runner."""
        monitor = SessionObserver(
            mock_config,
            session_runner=mock_session_runner,
            repository_host=mock_repository_host,
        )

        monitor._kill_session(456)

        mock_session_runner.kill_session.assert_called_once_with(456)

    def test_send_exit_to_session_uses_runner(self, mock_config, mock_session_runner, mock_repository_host):
        """Test _send_exit_to_session delegates to session runner."""
        mock_session_runner.send_to_session.return_value = True
        monitor = SessionObserver(
            mock_config,
            session_runner=mock_session_runner,
            repository_host=mock_repository_host,
        )

        result = monitor._send_exit_to_session(789)

        assert result is True
        mock_session_runner.send_to_session.assert_called_once_with(789, "/exit")

    def test_send_exit_to_session_returns_false_without_runner(self, mock_config):
        """Test _send_exit_to_session returns False when no runner."""
        monitor = SessionObserver(mock_config)

        result = monitor._send_exit_to_session(123)

        assert result is False


class TestCheckSession:
    """Test check_session method."""

    def test_check_session_timed_out(self, monitor, sample_session):
        """Test check_session returns TIMED_OUT when session times out."""
        # Set up session that has timed out
        old_time = datetime.now() - timedelta(minutes=60)
        sample_session.started_at = old_time
        sample_session.agent_config.timeout_minutes = 30

        status = monitor.check_session(sample_session)

        assert status == SessionStatus.TIMED_OUT

    def test_check_session_timed_out_via_state_machine(self, monitor_with_machines, sample_session):
        """Test check_session returns TIMED_OUT when state machine detects timeout."""
        # Create a mock state machine that indicates timeout
        mock_machine = MagicMock()
        mock_machine.check_timeout.return_value = True
        mock_machine._get_runtime_minutes.return_value = 60.0
        mock_machine.timeout_minutes = 30

        # Add machine to monitor
        monitor_with_machines.session_machines[sample_session.terminal_id] = mock_machine

        status = monitor_with_machines.check_session(sample_session)

        assert status == SessionStatus.TIMED_OUT

    def test_check_session_not_timed_out_via_state_machine(
        self, monitor_with_machines, sample_session, mock_session_runner
    ):
        """Test check_session respects state machine when not timed out."""
        mock_machine = MagicMock()
        mock_machine.check_timeout.return_value = False
        mock_session_runner.session_exists_by_name.return_value = True

        monitor_with_machines.session_machines[sample_session.terminal_id] = mock_machine

        status = monitor_with_machines.check_session(sample_session)

        assert status == SessionStatus.RUNNING

    def test_check_session_fallback_timeout_when_no_machine(self, monitor, sample_session):
        """Test check_session uses session.is_timed_out when no state machine."""
        old_time = datetime.now() - timedelta(minutes=60)
        sample_session.started_at = old_time
        sample_session.agent_config.timeout_minutes = 30

        status = monitor.check_session(sample_session)

        assert status == SessionStatus.TIMED_OUT

    def test_check_session_still_running(self, monitor, sample_session, mock_session_runner):
        """Test check_session returns RUNNING when session exists."""
        mock_session_runner.session_exists_by_name.return_value = True

        status = monitor.check_session(sample_session)

        assert status == SessionStatus.RUNNING
        mock_session_runner.session_exists_by_name.assert_called_with(sample_session.terminal_id)

    def test_check_session_running_with_pr_sends_exit(
        self, monitor, sample_session, mock_session_runner, mock_repository_host
    ):
        """Test check_session sends /exit when session has PR but still running."""
        mock_session_runner.session_exists_by_name.return_value = True
        mock_session_runner.send_to_session_by_name.return_value = True
        mock_repository_host.get_prs_for_branch.return_value = [
            PRInfo(number=1, url="https://...", title="PR", branch="test", labels=[], body="", state="open")
        ]
        sample_session.exit_sent = False

        status = monitor.check_session(sample_session)

        assert status == SessionStatus.RUNNING
        mock_session_runner.send_to_session_by_name.assert_called_with(sample_session.terminal_id, "/exit")
        assert sample_session.exit_sent is True

    def test_check_session_running_exit_sent_only_once(
        self, monitor, sample_session, mock_session_runner, mock_repository_host
    ):
        """Test that exit is only sent once."""
        mock_session_runner.session_exists_by_name.return_value = True
        mock_repository_host.get_prs_for_branch.return_value = [
            PRInfo(number=1, url="https://...", title="PR", branch="test", labels=[], body="", state="open")
        ]
        sample_session.exit_sent = True  # Already sent

        status = monitor.check_session(sample_session)

        assert status == SessionStatus.RUNNING
        mock_session_runner.send_to_session_by_name.assert_not_called()

    def test_check_session_completed_with_pr(
        self, monitor, sample_session, mock_session_runner, mock_repository_host
    ):
        """Test check_session returns COMPLETED when PR exists."""
        mock_session_runner.session_exists_by_name.return_value = False
        mock_repository_host.get_prs_for_branch.return_value = [
            PRInfo(number=1, url="https://...", title="PR", branch="test", labels=[], body="", state="open")
        ]

        status = monitor.check_session(sample_session)

        assert status == SessionStatus.COMPLETED

    def test_check_session_blocked_label(
        self, monitor, sample_session, mock_session_runner, mock_repository_host
    ):
        """Test check_session returns BLOCKED when issue has blocked label."""
        mock_session_runner.session_exists_by_name.return_value = False
        mock_repository_host.get_prs_for_branch.return_value = []
        mock_repository_host.get_issue_labels.return_value = ["blocked"]

        status = monitor.check_session(sample_session)

        assert status == SessionStatus.BLOCKED

    def test_check_session_needs_human_label(
        self, monitor, sample_session, mock_session_runner, mock_repository_host
    ):
        """Test check_session returns NEEDS_HUMAN when issue has needs-human label."""
        mock_session_runner.session_exists_by_name.return_value = False
        mock_repository_host.get_prs_for_branch.return_value = []
        mock_repository_host.get_issue_labels.return_value = ["needs-human"]

        status = monitor.check_session(sample_session)

        assert status == SessionStatus.NEEDS_HUMAN

    def test_check_session_failed_no_markers(
        self, monitor, sample_session, mock_session_runner, mock_repository_host
    ):
        """Test check_session returns FAILED when no success markers."""
        mock_session_runner.session_exists_by_name.return_value = False
        mock_repository_host.get_prs_for_branch.return_value = []
        mock_repository_host.get_issue_labels.return_value = []

        status = monitor.check_session(sample_session)

        assert status == SessionStatus.FAILED


class TestCheckAllSessions:
    """Test check_all_sessions method."""

    def test_check_all_sessions_empty_list(self, monitor):
        """Test check_all_sessions with empty list."""
        result = monitor.check_all_sessions([])
        assert result == {}

    def test_check_all_sessions_multiple(
        self, monitor, sample_session, sample_agent_config, tmp_path, mock_session_runner
    ):
        """Test check_all_sessions with multiple sessions."""
        # First session is running
        mock_session_runner.session_exists.side_effect = [True, False]

        session1 = sample_session
        issue2 = Issue(number=456, title="Issue 2", labels=["agent:web"])
        issue_key2 = FakeIssueKey(name="456")
        session_key2 = SessionKey(issue=issue_key2, task=TaskKind.CODE)
        session2 = Session(
            key=session_key2,
            issue=issue2,
            agent_config=sample_agent_config,
            terminal_id="issue-456",
            worktree_path=tmp_path / "worktree2",
            branch_name="issue-456-test",
        )

        result = monitor.check_all_sessions([session1, session2])

        assert 123 in result
        assert 456 in result


class TestHandleCompletion:
    """Test handle_completion method.

    Note: Label operations (add blocked-failed, remove in-progress) are now
    handled via actions generated by the orchestrator, not the observer.
    The observer only handles session-level cleanup (killing sessions, closing tabs).
    """

    def test_handle_completion_timed_out_kills_session(
        self, monitor, sample_session, mock_session_runner, mock_repository_host
    ):
        """Test handle_completion for TIMED_OUT kills session."""
        monitor.handle_completion(sample_session, SessionStatus.TIMED_OUT)

        # Observer kills timed-out sessions
        mock_session_runner.kill_session.assert_called_with(sample_session.issue.number)
        # Labels are now handled via actions, not observer
        mock_repository_host.add_label.assert_not_called()
        mock_repository_host.remove_label.assert_not_called()

    def test_handle_completion_failed_no_label_operations(
        self, monitor, sample_session, mock_repository_host
    ):
        """Test handle_completion for FAILED doesn't do label ops (handled via actions)."""
        monitor.handle_completion(sample_session, SessionStatus.FAILED)

        # Labels are now handled via actions, not observer
        mock_repository_host.add_label.assert_not_called()
        mock_repository_host.remove_label.assert_not_called()

    def test_handle_completion_completed_closes_tab_when_configured(
        self, monitor, sample_session, mock_session_runner, mock_repository_host
    ):
        """Test handle_completion for COMPLETED may close tab based on config."""
        monitor.config.close_completed_tabs = True

        monitor.handle_completion(sample_session, SessionStatus.COMPLETED)

        # Labels are now handled via actions, not observer
        mock_repository_host.remove_label.assert_not_called()
        # But tab closing is still observer's responsibility
        mock_session_runner.kill_session.assert_called()

    def test_handle_completion_blocked_no_label_operations(
        self, monitor, sample_session, mock_repository_host
    ):
        """Test handle_completion for BLOCKED doesn't do label ops."""
        monitor.handle_completion(sample_session, SessionStatus.BLOCKED)

        # Labels handled via actions
        mock_repository_host.add_label.assert_not_called()
        mock_repository_host.remove_label.assert_not_called()

    def test_handle_completion_needs_human_no_label_operations(
        self, monitor, sample_session, mock_repository_host
    ):
        """Test handle_completion for NEEDS_HUMAN doesn't do label ops."""
        monitor.handle_completion(sample_session, SessionStatus.NEEDS_HUMAN)

        # Labels handled via actions
        mock_repository_host.add_label.assert_not_called()
        mock_repository_host.remove_label.assert_not_called()

    def test_handle_completion_close_completed_tabs(
        self, monitor, sample_session, mock_session_runner
    ):
        """Test close_completed_tabs config closes tab on completion."""
        monitor.config.close_completed_tabs = True

        monitor.handle_completion(sample_session, SessionStatus.COMPLETED)

        mock_session_runner.kill_session.assert_called()

    def test_handle_completion_dont_close_completed_tabs(
        self, monitor, sample_session, mock_session_runner
    ):
        """Test close_completed_tabs=False keeps tab open."""
        monitor.config.close_completed_tabs = False

        monitor.handle_completion(sample_session, SessionStatus.COMPLETED)

        # kill_session should not be called for tab closing
        # (it's only called for TIMED_OUT status kill)
        mock_session_runner.kill_session.assert_not_called()

    def test_handle_completion_close_failed_tabs(
        self, monitor, sample_session, mock_session_runner
    ):
        """Test close_failed_tabs config closes tab on failure."""
        monitor.config.close_failed_tabs = True

        monitor.handle_completion(sample_session, SessionStatus.FAILED)

        mock_session_runner.kill_session.assert_called()

    def test_handle_completion_outer_exception_handler(
        self, monitor, sample_session, mock_session_runner
    ):
        """Test that outer exception handler catches unexpected errors."""
        mock_session_runner.kill_session.side_effect = Exception("Unexpected error")
        monitor.config.close_completed_tabs = True

        # Should not raise - exception is caught and logged
        monitor.handle_completion(sample_session, SessionStatus.COMPLETED)


class TestSessionObserverIntegration:
    """Integration tests for SessionObserver."""

    def test_full_workflow_timed_out(
        self, monitor, sample_session, mock_session_runner, mock_repository_host
    ):
        """Test full workflow for timed out session."""
        # Session has timed out
        old_time = datetime.now() - timedelta(minutes=60)
        sample_session.started_at = old_time
        sample_session.agent_config.timeout_minutes = 30

        # Check and handle
        status = monitor.check_session(sample_session)
        assert status == SessionStatus.TIMED_OUT

        monitor.handle_completion(sample_session, status)

        # Observer kills the session
        mock_session_runner.kill_session.assert_called()
        # Labels are now handled via actions in orchestrator, not observer
        mock_repository_host.add_label.assert_not_called()

    def test_full_workflow_completed(
        self, monitor, sample_session, mock_session_runner, mock_repository_host
    ):
        """Test full workflow for completed session."""
        mock_session_runner.session_exists_by_name.return_value = False
        mock_repository_host.get_prs_for_branch.return_value = [
            PRInfo(number=1, url="https://...", title="PR", branch="test", labels=[], body="", state="open")
        ]

        status = monitor.check_session(sample_session)
        assert status == SessionStatus.COMPLETED

        monitor.handle_completion(sample_session, status)

        # Labels are now handled via actions in orchestrator, not observer
        mock_repository_host.remove_label.assert_not_called()
