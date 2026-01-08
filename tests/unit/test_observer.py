"""Unit tests for the observer module."""

import pytest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch, call
from issue_orchestrator.observation.observer import SessionObserver
from issue_orchestrator.domain.models import (
    Session,
    SessionStatus,
    Issue,
    AgentConfig,
)
from issue_orchestrator.infra.config import Config
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


class TestExtractSessionNumber:
    """Test _extract_session_number for various session name prefixes."""

    @pytest.mark.parametrize(
        "session_name,expected",
        [
            ("issue-123", 123),
            ("issue-1", 1),
            ("issue-99999", 99999),
            ("review-456", 456),
            ("review-1", 1),
            ("rework-789", 789),
            ("triage-42", 42),
        ],
    )
    def test_extract_session_number_valid_prefixes(self, monitor, session_name, expected):
        """Test extracting session numbers from valid prefixes."""
        result = monitor._extract_session_number(session_name)
        assert result == expected

    def test_extract_session_number_unknown_prefix_raises(self, monitor):
        """Test that unknown session name format raises ValueError."""
        with pytest.raises(ValueError, match="Unknown session name format"):
            monitor._extract_session_number("unknown-123")

    def test_extract_session_number_invalid_format_raises(self, monitor):
        """Test that completely invalid format raises ValueError."""
        with pytest.raises(ValueError, match="Unknown session name format"):
            monitor._extract_session_number("no-prefix-here")


class TestObserveSession:
    """Test observe_session method - the main observation loop."""

    def test_observe_session_returns_terminated_when_completion_json_exists(
        self, monitor, sample_session, mock_session_runner, tmp_path
    ):
        """Test that observe_session returns TERMINATED when valid completion.json exists."""
        import json
        from issue_orchestrator.observation.observation import SessionObservation

        # Create valid completion.json
        worktree = tmp_path / "worktree"
        worktree.mkdir(parents=True)
        sample_session.worktree_path = worktree
        completion_dir = worktree / ".issue-orchestrator"
        completion_dir.mkdir(parents=True)
        completion_file = completion_dir / "completion.json"
        completion_file.write_text(json.dumps({
            "session_id": "test-session",
            "timestamp": "2024-01-01T00:00:00Z",
            "outcome": "completed",
            "summary": "Work done",
        }))

        mock_session_runner.session_exists_by_name.return_value = True

        result = monitor.observe_session(sample_session)

        assert result.observation == SessionObservation.TERMINATED
        assert result.session_exists is False  # terminated() sets session_exists=False

    def test_observe_session_ignores_incomplete_completion_json(
        self, monitor, sample_session, mock_session_runner, tmp_path
    ):
        """Test that observe_session ignores completion.json missing required fields."""
        import json
        from issue_orchestrator.observation.observation import SessionObservation

        worktree = tmp_path / "worktree"
        worktree.mkdir(parents=True)
        sample_session.worktree_path = worktree
        completion_dir = worktree / ".issue-orchestrator"
        completion_dir.mkdir(parents=True)
        completion_file = completion_dir / "completion.json"
        # Missing required fields (session_id, timestamp, outcome, summary)
        completion_file.write_text(json.dumps({"partial": "data"}))

        mock_session_runner.session_exists_by_name.return_value = True

        result = monitor.observe_session(sample_session)

        # Should be RUNNING since completion.json is incomplete
        assert result.observation == SessionObservation.RUNNING

    def test_observe_session_ignores_malformed_json(
        self, monitor, sample_session, mock_session_runner, tmp_path
    ):
        """Test that observe_session ignores malformed completion.json."""
        from issue_orchestrator.observation.observation import SessionObservation

        worktree = tmp_path / "worktree"
        worktree.mkdir(parents=True)
        sample_session.worktree_path = worktree
        completion_dir = worktree / ".issue-orchestrator"
        completion_dir.mkdir(parents=True)
        completion_file = completion_dir / "completion.json"
        completion_file.write_text("{ not valid json")

        mock_session_runner.session_exists_by_name.return_value = True

        result = monitor.observe_session(sample_session)

        # Should be RUNNING since JSON is invalid
        assert result.observation == SessionObservation.RUNNING

    def test_observe_session_returns_timed_out_when_timeout_exceeded(
        self, monitor, sample_session, mock_session_runner
    ):
        """Test that observe_session returns TIMED_OUT when session exceeds timeout."""
        from datetime import timedelta
        from issue_orchestrator.observation.observation import SessionObservation

        # Set up session that has timed out
        old_time = datetime.now() - timedelta(minutes=60)
        sample_session.started_at = old_time
        sample_session.agent_config.timeout_minutes = 30

        mock_session_runner.session_exists_by_name.return_value = True

        result = monitor.observe_session(sample_session)

        assert result.observation == SessionObservation.TIMED_OUT
        assert result.timeout_exceeded is True

    def test_observe_session_returns_running_when_session_exists(
        self, monitor, sample_session, mock_session_runner, tmp_path
    ):
        """Test that observe_session returns RUNNING when session exists and not timed out."""
        from issue_orchestrator.observation.observation import SessionObservation

        sample_session.worktree_path = tmp_path
        mock_session_runner.session_exists_by_name.return_value = True

        result = monitor.observe_session(sample_session)

        assert result.observation == SessionObservation.RUNNING
        assert result.session_exists is True

    def test_observe_session_returns_terminated_when_session_not_exists(
        self, monitor, sample_session, mock_session_runner, tmp_path
    ):
        """Test that observe_session returns TERMINATED when session doesn't exist.

        Note: Sessions must be older than the 60-second grace period to be marked
        as terminated. This prevents false terminations during startup when
        iTerm tab detection may be unreliable.
        """
        from datetime import datetime, timedelta
        from issue_orchestrator.observation.observation import SessionObservation

        sample_session.worktree_path = tmp_path
        # Set session to be older than the 60-second grace period
        sample_session.started_at = datetime.now() - timedelta(seconds=120)
        mock_session_runner.session_exists_by_name.return_value = False

        result = monitor.observe_session(sample_session)

        assert result.observation == SessionObservation.TERMINATED

    def test_observe_session_grace_period_prevents_early_termination(
        self, monitor, sample_session, mock_session_runner, tmp_path
    ):
        """Test that new sessions get a grace period before being marked as terminated.

        This prevents false terminations during startup when iTerm tab detection
        may be unreliable (e.g., AppleScript can't find the tab immediately).
        """
        from datetime import datetime
        from issue_orchestrator.observation.observation import SessionObservation

        sample_session.worktree_path = tmp_path
        # Session just started (within grace period)
        sample_session.started_at = datetime.now()
        mock_session_runner.session_exists_by_name.return_value = False

        result = monitor.observe_session(sample_session)

        # Should be treated as RUNNING during grace period, not TERMINATED
        assert result.observation == SessionObservation.RUNNING

    def test_observe_session_log_activity_prevents_termination(
        self, monitor, sample_session, mock_session_runner, tmp_path
    ):
        """Test that active log file prevents termination even after grace period.

        If the session log was modified recently, the session is clearly active
        and shouldn't be terminated just because iTerm detection failed.
        """
        import time
        from datetime import datetime, timedelta
        from issue_orchestrator.observation.observation import SessionObservation

        sample_session.worktree_path = tmp_path
        # Session is older than grace period
        sample_session.started_at = datetime.now() - timedelta(seconds=120)
        mock_session_runner.session_exists_by_name.return_value = False

        # Create an active session log (recently modified)
        log_dir = tmp_path / ".issue-orchestrator"
        log_dir.mkdir(parents=True)
        log_file = log_dir / "session.log"
        log_file.write_text("Claude is working...")
        # Touch the file to ensure it's "just modified"

        result = monitor.observe_session(sample_session)

        # Should be treated as RUNNING due to active log, not TERMINATED
        assert result.observation == SessionObservation.RUNNING

    def test_observe_session_sends_exit_when_session_has_pr(
        self, monitor, sample_session, mock_session_runner, mock_repository_host, tmp_path
    ):
        """Test that observe_session sends /exit when running session has a PR."""
        from issue_orchestrator.observation.observation import SessionObservation

        sample_session.worktree_path = tmp_path
        sample_session.exit_sent = False
        mock_session_runner.session_exists_by_name.return_value = True
        mock_session_runner.send_to_session_by_name.return_value = True
        mock_repository_host.get_prs_for_branch.return_value = [
            PRInfo(number=1, url="https://...", title="PR", branch="test", labels=[], body="", state="open")
        ]

        result = monitor.observe_session(sample_session)

        assert result.observation == SessionObservation.RUNNING
        mock_session_runner.send_to_session_by_name.assert_called_with(sample_session.terminal_id, "/exit")
        assert sample_session.exit_sent is True

    def test_observe_session_does_not_resend_exit(
        self, monitor, sample_session, mock_session_runner, mock_repository_host, tmp_path
    ):
        """Test that observe_session doesn't resend /exit if already sent."""
        sample_session.worktree_path = tmp_path
        sample_session.exit_sent = True
        mock_session_runner.session_exists_by_name.return_value = True
        mock_repository_host.get_prs_for_branch.return_value = [
            PRInfo(number=1, url="https://...", title="PR", branch="test", labels=[], body="", state="open")
        ]

        monitor.observe_session(sample_session)

        mock_session_runner.send_to_session_by_name.assert_not_called()

    def test_observe_session_timeout_with_state_machine(
        self, monitor_with_machines, sample_session, mock_session_runner, tmp_path
    ):
        """Test timeout detection via state machine takes priority."""
        from issue_orchestrator.observation.observation import SessionObservation

        sample_session.worktree_path = tmp_path

        mock_machine = MagicMock()
        mock_machine._get_runtime_minutes.return_value = 60.0
        mock_machine.timeout_minutes = 30

        monitor_with_machines.session_machines[sample_session.terminal_id] = mock_machine
        mock_session_runner.session_exists_by_name.return_value = True

        result = monitor_with_machines.observe_session(sample_session)

        assert result.observation == SessionObservation.TIMED_OUT
        assert result.timeout_exceeded is True

    def test_observe_session_emits_event_on_completion_detected(
        self, mock_config, mock_session_runner, mock_repository_host, sample_session, tmp_path
    ):
        """Test that observe_session emits event when completion.json is detected."""
        import json
        from issue_orchestrator.events import EventName
        from issue_orchestrator.ports import TraceEvent

        mock_events = MagicMock()
        monitor = SessionObserver(
            mock_config,
            events=mock_events,
            session_runner=mock_session_runner,
            repository_host=mock_repository_host,
        )

        worktree = tmp_path / "worktree"
        worktree.mkdir(parents=True)
        sample_session.worktree_path = worktree
        completion_dir = worktree / ".issue-orchestrator"
        completion_dir.mkdir(parents=True)
        completion_file = completion_dir / "completion.json"
        completion_file.write_text(json.dumps({
            "session_id": "test-session",
            "timestamp": "2024-01-01T00:00:00Z",
            "outcome": "completed",
            "summary": "Work done",
        }))

        mock_session_runner.session_exists_by_name.return_value = True

        monitor.observe_session(sample_session)

        # Verify event was published
        mock_events.publish.assert_called()
        call_args = mock_events.publish.call_args_list[0]
        event = call_args[0][0]
        assert event.name == EventName.OBSERVATION_COMPLETION_DETECTED


class TestEmitNoOutputIfStale:
    """Test _emit_no_output_if_stale method for detecting idle sessions."""

    def test_emit_no_output_when_log_unchanged_for_threshold(
        self, mock_config, mock_session_runner, mock_repository_host, sample_session, tmp_path
    ):
        """Test that no_output event is emitted when log hasn't changed past threshold."""
        import time
        from issue_orchestrator.events import EventName

        mock_events = MagicMock()
        mock_config.session_no_output_seconds = 1  # 1 second for test speed
        mock_config.session_no_output_tail_lines = 10
        mock_config.session_no_output_max_bytes = 1000
        mock_config.session_no_output_repeat_seconds = 0  # Allow immediate repeat for testing

        monitor = SessionObserver(
            mock_config,
            events=mock_events,
            session_runner=mock_session_runner,
            repository_host=mock_repository_host,
        )

        worktree = tmp_path / "worktree"
        worktree.mkdir(parents=True)
        sample_session.worktree_path = worktree

        # Create session log
        log_dir = worktree / ".issue-orchestrator"
        log_dir.mkdir(parents=True)
        log_file = log_dir / "session.log"
        log_file.write_text("Some log content\n")

        # First call - initialize tracking
        monitor._emit_no_output_if_stale(sample_session)
        assert sample_session.last_output_monotonic is not None

        # Simulate time passing (adjust the monotonic timestamp)
        sample_session.last_output_monotonic = time.monotonic() - 10  # 10 seconds ago

        # Second call - should emit event
        monitor._emit_no_output_if_stale(sample_session)

        # Verify event was published
        mock_events.publish.assert_called()
        event = mock_events.publish.call_args[0][0]
        assert event.name == EventName.SESSION_NO_OUTPUT

    def test_no_emit_when_log_changes(
        self, mock_config, mock_session_runner, mock_repository_host, sample_session, tmp_path
    ):
        """Test that no event is emitted when log file changes."""
        import time

        mock_events = MagicMock()
        mock_config.session_no_output_seconds = 1
        mock_config.session_no_output_tail_lines = 10
        mock_config.session_no_output_max_bytes = 1000

        monitor = SessionObserver(
            mock_config,
            events=mock_events,
            session_runner=mock_session_runner,
            repository_host=mock_repository_host,
        )

        worktree = tmp_path / "worktree"
        worktree.mkdir(parents=True)
        sample_session.worktree_path = worktree

        log_dir = worktree / ".issue-orchestrator"
        log_dir.mkdir(parents=True)
        log_file = log_dir / "session.log"
        log_file.write_text("Initial content\n")

        # First call - initialize
        monitor._emit_no_output_if_stale(sample_session)
        initial_mtime = sample_session.last_log_mtime

        # Change the log file
        time.sleep(0.01)  # Ensure different mtime
        log_file.write_text("Initial content\nNew content\n")

        # Second call - should NOT emit because log changed
        monitor._emit_no_output_if_stale(sample_session)

        # Verify no session_no_output event (only calls would be for log updates)
        for call in mock_events.publish.call_args_list:
            event = call[0][0]
            assert event.name != "session.no_output"

    def test_no_emit_when_log_does_not_exist(
        self, mock_config, mock_session_runner, mock_repository_host, sample_session, tmp_path
    ):
        """Test that no event is emitted when log file doesn't exist."""
        mock_events = MagicMock()

        monitor = SessionObserver(
            mock_config,
            events=mock_events,
            session_runner=mock_session_runner,
            repository_host=mock_repository_host,
        )

        worktree = tmp_path / "worktree"
        worktree.mkdir(parents=True)
        sample_session.worktree_path = worktree
        # Don't create log file

        monitor._emit_no_output_if_stale(sample_session)

        mock_events.publish.assert_not_called()

    def test_respects_repeat_interval(
        self, mock_config, mock_session_runner, mock_repository_host, sample_session, tmp_path
    ):
        """Test that no_output event respects repeat interval."""
        import time

        mock_events = MagicMock()
        mock_config.session_no_output_seconds = 1
        mock_config.session_no_output_tail_lines = 10
        mock_config.session_no_output_max_bytes = 1000
        mock_config.session_no_output_repeat_seconds = 60  # 60 second cooldown

        monitor = SessionObserver(
            mock_config,
            events=mock_events,
            session_runner=mock_session_runner,
            repository_host=mock_repository_host,
        )

        worktree = tmp_path / "worktree"
        worktree.mkdir(parents=True)
        sample_session.worktree_path = worktree

        log_dir = worktree / ".issue-orchestrator"
        log_dir.mkdir(parents=True)
        log_file = log_dir / "session.log"
        log_file.write_text("Some content\n")

        # Initialize
        monitor._emit_no_output_if_stale(sample_session)
        sample_session.last_output_monotonic = time.monotonic() - 10  # 10 seconds ago

        # First emission
        monitor._emit_no_output_if_stale(sample_session)
        first_call_count = mock_events.publish.call_count

        # Try again immediately - should be suppressed by repeat interval
        monitor._emit_no_output_if_stale(sample_session)

        assert mock_events.publish.call_count == first_call_count  # No new calls


class TestReadLogTail:
    """Test _read_log_tail method for reading log file tails."""

    def test_read_log_tail_returns_last_n_lines(self, monitor, tmp_path):
        """Test that _read_log_tail returns the last N lines."""
        log_file = tmp_path / "test.log"
        log_file.write_text("line1\nline2\nline3\nline4\nline5\n")

        result = monitor._read_log_tail(log_file, tail_lines=3, max_bytes=10000)

        assert "line3" in result
        assert "line4" in result
        assert "line5" in result
        assert "line1" not in result
        assert "line2" not in result

    def test_read_log_tail_truncates_to_max_bytes(self, monitor, tmp_path):
        """Test that _read_log_tail truncates to max_bytes."""
        log_file = tmp_path / "test.log"
        # Create content larger than max_bytes
        log_file.write_text("x" * 1000 + "\n")

        result = monitor._read_log_tail(log_file, tail_lines=10, max_bytes=100)

        assert len(result.encode("utf-8")) <= 100

    def test_read_log_tail_returns_empty_on_missing_file(self, monitor, tmp_path):
        """Test that _read_log_tail returns empty string for missing file."""
        log_file = tmp_path / "nonexistent.log"

        result = monitor._read_log_tail(log_file, tail_lines=10, max_bytes=10000)

        assert result == ""

    def test_read_log_tail_returns_empty_on_read_error(self, monitor, tmp_path):
        """Test that _read_log_tail returns empty string on read error."""
        # Create a directory instead of file to cause read error
        log_dir = tmp_path / "not_a_file.log"
        log_dir.mkdir()

        result = monitor._read_log_tail(log_dir, tail_lines=10, max_bytes=10000)

        assert result == ""


class TestPortDelegation:
    """Test port delegation methods handle None ports gracefully."""

    def test_session_exists_by_name_without_runner_returns_false(self, mock_config):
        """Test _session_exists_by_name returns False when no runner."""
        monitor = SessionObserver(mock_config)
        result = monitor._session_exists_by_name("issue-123")
        assert result is False

    def test_send_exit_by_name_without_runner_returns_false(self, mock_config):
        """Test _send_exit_to_session_by_name returns False when no runner."""
        monitor = SessionObserver(mock_config)
        result = monitor._send_exit_to_session_by_name("issue-123")
        assert result is False

    def test_get_open_prs_without_host_returns_empty_list(self, mock_config):
        """Test _get_open_prs_for_branch returns empty list when no host."""
        monitor = SessionObserver(mock_config)
        result = monitor._get_open_prs_for_branch("some-branch")
        assert result == []

    def test_get_issue_labels_without_host_returns_empty_list(self, mock_config):
        """Test _get_issue_labels returns empty list when no host."""
        monitor = SessionObserver(mock_config)
        result = monitor._get_issue_labels(123)
        assert result == []

    def test_add_label_without_host_is_noop(self, mock_config):
        """Test _add_label is a no-op when no host."""
        monitor = SessionObserver(mock_config)
        # Should not raise
        monitor._add_label(123, "some-label")

    def test_remove_label_without_host_is_noop(self, mock_config):
        """Test _remove_label is a no-op when no host."""
        monitor = SessionObserver(mock_config)
        # Should not raise
        monitor._remove_label(123, "some-label")

    def test_kill_session_without_runner_is_noop(self, mock_config):
        """Test _kill_session is a no-op when no runner."""
        monitor = SessionObserver(mock_config)
        # Should not raise
        monitor._kill_session(123)


class TestCheckSessionExceptionHandling:
    """Test exception handling in check_session."""

    def test_check_session_handles_pr_lookup_exception(
        self, monitor, sample_session, mock_session_runner, mock_repository_host
    ):
        """Test check_session handles exceptions when checking for PRs."""
        mock_session_runner.session_exists_by_name.return_value = True
        mock_repository_host.get_prs_for_branch.side_effect = Exception("Network error")
        sample_session.exit_sent = False

        # Should not raise, should return RUNNING
        status = monitor.check_session(sample_session)
        assert status == SessionStatus.RUNNING

    def test_check_session_handles_label_lookup_exception(
        self, monitor, sample_session, mock_session_runner, mock_repository_host
    ):
        """Test check_session falls back to stale labels on exception."""
        mock_session_runner.session_exists_by_name.return_value = False
        mock_repository_host.get_prs_for_branch.return_value = []
        mock_repository_host.get_issue_labels.side_effect = Exception("API error")

        # Should not raise, should use fallback (empty labels -> FAILED)
        status = monitor.check_session(sample_session)
        assert status == SessionStatus.FAILED


class TestCheckAllSessionsExceptionHandling:
    """Test exception handling in check_all_sessions."""

    def test_check_all_sessions_handles_individual_session_errors(
        self, monitor, sample_session, sample_agent_config, tmp_path, mock_session_runner
    ):
        """Test check_all_sessions continues after individual session errors."""
        # Set up to raise on first session
        mock_session_runner.session_exists_by_name.side_effect = [
            Exception("Error on first"),
            False,  # Second session just doesn't exist
        ]
        mock_session_runner.session_exists.side_effect = [
            Exception("Error on first"),
            False,
        ]

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

        result = monitor.check_all_sessions([sample_session, session2])

        # Both should be in results, first one FAILED due to error
        assert 123 in result
        assert result[123] == SessionStatus.FAILED
        assert 456 in result


class TestPortDelegationWithHost:
    """Test port delegation methods when host IS provided."""

    def test_add_label_with_host_calls_host(self, mock_config, mock_repository_host):
        """Test _add_label calls repository host when available."""
        monitor = SessionObserver(mock_config, repository_host=mock_repository_host)
        monitor._add_label(123, "some-label")
        mock_repository_host.add_label.assert_called_once_with(123, "some-label")

    def test_remove_label_with_host_calls_host(self, mock_config, mock_repository_host):
        """Test _remove_label calls repository host when available."""
        monitor = SessionObserver(mock_config, repository_host=mock_repository_host)
        monitor._remove_label(123, "some-label")
        mock_repository_host.remove_label.assert_called_once_with(123, "some-label")


class TestObserveSessionExceptionHandling:
    """Test exception handling in observe_session."""

    def test_observe_session_handles_pr_check_exception(
        self, monitor, sample_session, mock_session_runner, mock_repository_host, tmp_path
    ):
        """Test observe_session catches exceptions when checking for PRs."""
        from issue_orchestrator.observation.observation import SessionObservation

        sample_session.worktree_path = tmp_path
        sample_session.exit_sent = False
        mock_session_runner.session_exists_by_name.return_value = True
        mock_repository_host.get_prs_for_branch.side_effect = Exception("Network error")

        # Should not raise, should return RUNNING
        result = monitor.observe_session(sample_session)

        assert result.observation == SessionObservation.RUNNING
        # exit_sent should remain False since PR check failed
        assert sample_session.exit_sent is False


class TestEmitNoOutputEdgeCases:
    """Test edge cases in _emit_no_output_if_stale."""

    def test_no_emit_when_stat_raises_oserror(
        self, mock_config, mock_session_runner, mock_repository_host, sample_session, tmp_path
    ):
        """Test that no event is emitted when stat() raises OSError."""
        import os
        from unittest.mock import patch

        mock_events = MagicMock()

        monitor = SessionObserver(
            mock_config,
            events=mock_events,
            session_runner=mock_session_runner,
            repository_host=mock_repository_host,
        )

        worktree = tmp_path / "worktree"
        worktree.mkdir(parents=True)
        sample_session.worktree_path = worktree

        # Create a log file that will exist but fail on stat
        log_dir = worktree / ".issue-orchestrator"
        log_dir.mkdir(parents=True)
        log_file = log_dir / "session.log"
        log_file.write_text("content")

        # Patch stat to raise OSError after exists() returns True
        original_stat = log_file.stat
        with patch.object(log_file.__class__, 'stat', side_effect=OSError("Permission denied")):
            monitor._emit_no_output_if_stale(sample_session)

        # No events should be emitted
        mock_events.publish.assert_not_called()

    def test_no_emit_when_idle_time_under_threshold(
        self, mock_config, mock_session_runner, mock_repository_host, sample_session, tmp_path
    ):
        """Test that no event is emitted when idle time is under threshold."""
        import time

        mock_events = MagicMock()
        mock_config.session_no_output_seconds = 600  # 10 minute threshold
        mock_config.session_no_output_tail_lines = 10
        mock_config.session_no_output_max_bytes = 1000

        monitor = SessionObserver(
            mock_config,
            events=mock_events,
            session_runner=mock_session_runner,
            repository_host=mock_repository_host,
        )

        worktree = tmp_path / "worktree"
        worktree.mkdir(parents=True)
        sample_session.worktree_path = worktree

        log_dir = worktree / ".issue-orchestrator"
        log_dir.mkdir(parents=True)
        log_file = log_dir / "session.log"
        log_file.write_text("content")

        # Initialize tracking
        monitor._emit_no_output_if_stale(sample_session)

        # Only 5 seconds ago - under 600 second threshold
        sample_session.last_output_monotonic = time.monotonic() - 5

        # Second call - should NOT emit because under threshold
        monitor._emit_no_output_if_stale(sample_session)

        # No session_no_output event should be emitted
        mock_events.publish.assert_not_called()

    def test_no_emit_when_last_output_monotonic_is_none(
        self, mock_config, mock_session_runner, mock_repository_host, sample_session, tmp_path
    ):
        """Test that no event is emitted when last_output_monotonic is None but log unchanged."""
        mock_events = MagicMock()
        mock_config.session_no_output_seconds = 1
        mock_config.session_no_output_tail_lines = 10
        mock_config.session_no_output_max_bytes = 1000

        monitor = SessionObserver(
            mock_config,
            events=mock_events,
            session_runner=mock_session_runner,
            repository_host=mock_repository_host,
        )

        worktree = tmp_path / "worktree"
        worktree.mkdir(parents=True)
        sample_session.worktree_path = worktree

        log_dir = worktree / ".issue-orchestrator"
        log_dir.mkdir(parents=True)
        log_file = log_dir / "session.log"
        log_file.write_text("content")

        # Initialize - this sets the mtime/size tracking
        monitor._emit_no_output_if_stale(sample_session)

        # Clear last_output_monotonic to simulate edge case
        sample_session.last_output_monotonic = None

        # Second call - should return early because last_output_monotonic is None
        monitor._emit_no_output_if_stale(sample_session)

        # No events should be emitted
        mock_events.publish.assert_not_called()


class TestHandleCompletionKillExceptions:
    """Test exception handling when killing sessions in handle_completion."""

    def test_handle_completion_continues_after_kill_exception(
        self, monitor, sample_session, mock_session_runner
    ):
        """Test handle_completion continues when kill_session raises exception."""
        mock_session_runner.kill_session.side_effect = Exception("Kill failed")

        # Should not raise - exception is caught and logged
        monitor.handle_completion(sample_session, SessionStatus.TIMED_OUT)

        # Method should complete without raising
        mock_session_runner.kill_session.assert_called()


class TestCheckSessionPRLookupOnExit:
    """Test PR lookup exception handling after session exits."""

    def test_check_session_handles_pr_lookup_exception_after_exit(
        self, monitor, sample_session, mock_session_runner, mock_repository_host
    ):
        """Test check_session handles exception when looking up PRs for exited session."""
        mock_session_runner.session_exists_by_name.return_value = False
        mock_repository_host.get_prs_for_branch.side_effect = Exception("API error")

        # Should fall through to label checking, then to FAILED
        status = monitor.check_session(sample_session)

        # PR check failed, so should check labels next
        mock_repository_host.get_issue_labels.assert_called()
        assert status == SessionStatus.FAILED
