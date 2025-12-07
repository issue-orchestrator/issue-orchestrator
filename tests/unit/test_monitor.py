"""Unit tests for the monitor module."""

import pytest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch, call
from issue_orchestrator.monitor import SessionMonitor
from issue_orchestrator.models import (
    Session,
    SessionStatus,
    Issue,
    AgentConfig,
)
from issue_orchestrator.config import Config


@pytest.fixture
def mock_config():
    """Create a mock config for testing."""
    config = Config()
    config.repo = "owner/repo"
    config.max_sessions = 3
    config.session_timeout_minutes = 45
    config.label_in_progress = "in-progress"
    config.label_blocked = "blocked"
    config.label_needs_human = "needs-human"
    config.ui_mode = "tmux"
    config.close_completed_tabs = True
    config.close_failed_tabs = False
    return config


@pytest.fixture
def monitor(mock_config):
    """Create a SessionMonitor instance for testing."""
    return SessionMonitor(mock_config)


@pytest.fixture
def sample_session(sample_agent_config, tmp_path):
    """Create a sample session for testing."""
    issue = Issue(
        number=123,
        title="Test Issue",
        labels=["agent:web"],
        body="Test body",
    )
    return Session(
        issue=issue,
        agent_config=sample_agent_config,
        tmux_session_name="issue-123",
        worktree_path=tmp_path / "worktree",
        branch_name="issue-123-test",
    )


class TestSessionMonitorInit:
    """Test SessionMonitor initialization."""

    def test_init_with_config(self, mock_config):
        """Test initializing monitor with config."""
        monitor = SessionMonitor(mock_config)
        assert monitor.config == mock_config
        assert monitor._iterm_manager is None

    def test_init_stores_config(self, mock_config):
        """Test that config is properly stored."""
        monitor = SessionMonitor(mock_config)
        assert monitor.config.repo == "owner/repo"
        assert monitor.config.max_sessions == 3


class TestSessionMonitorProperties:
    """Test SessionMonitor property methods."""

    def test_using_iterm2_tmux_mode(self, mock_config):
        """Test _using_iterm2 returns False for tmux mode."""
        mock_config.ui_mode = "tmux"
        monitor = SessionMonitor(mock_config)
        assert monitor._using_iterm2 is False

    def test_using_iterm2_iterm2_mode(self, mock_config):
        """Test _using_iterm2 returns True for iterm2 mode."""
        mock_config.ui_mode = "iterm2"
        monitor = SessionMonitor(mock_config)
        assert monitor._using_iterm2 is True

    def test_using_iterm2_web_mode(self, mock_config):
        """Test _using_iterm2 returns True for web mode."""
        mock_config.ui_mode = "web"
        monitor = SessionMonitor(mock_config)
        assert monitor._using_iterm2 is True

    @patch('issue_orchestrator.iterm2.get_iterm_manager')
    def test_get_iterm_manager_lazy_init(self, mock_get_manager, mock_config):
        """Test that iTerm manager is lazily initialized."""
        mock_config.ui_mode = "iterm2"
        monitor = SessionMonitor(mock_config)

        # Should be None initially
        assert monitor._iterm_manager is None

        # Call the method
        mock_manager = MagicMock()
        mock_get_manager.return_value = mock_manager
        result = monitor._get_iterm_manager()

        # Should have initialized
        assert result == mock_manager
        assert monitor._iterm_manager == mock_manager
        mock_get_manager.assert_called_once()

    @patch('issue_orchestrator.iterm2.get_iterm_manager')
    def test_get_iterm_manager_caching(self, mock_get_manager, mock_config):
        """Test that iTerm manager is cached after first call."""
        mock_config.ui_mode = "iterm2"
        monitor = SessionMonitor(mock_config)

        mock_manager = MagicMock()
        mock_get_manager.return_value = mock_manager

        # Call twice
        result1 = monitor._get_iterm_manager()
        result2 = monitor._get_iterm_manager()

        # Should only call once (cached)
        assert result1 == result2
        mock_get_manager.assert_called_once()


class TestSessionMonitorBackends:
    """Test backend selection methods."""

    @patch('issue_orchestrator.monitor.session_exists')
    def test_session_exists_tmux_mode(self, mock_session_exists, monitor):
        """Test _session_exists uses tmux backend in tmux mode."""
        monitor.config.ui_mode = "tmux"
        mock_session_exists.return_value = True

        result = monitor._session_exists("issue-123")

        assert result is True
        mock_session_exists.assert_called_once_with("issue-123")

    @patch('issue_orchestrator.iterm2.get_iterm_manager')
    def test_session_exists_iterm2_mode(self, mock_get_manager, monitor):
        """Test _session_exists uses iTerm2 backend in iterm2 mode."""
        monitor.config.ui_mode = "iterm2"
        mock_manager = MagicMock()
        mock_manager.session_exists.return_value = True
        mock_get_manager.return_value = mock_manager

        result = monitor._session_exists("issue-123")

        assert result is True
        mock_manager.session_exists.assert_called_once_with(123)

    @patch('issue_orchestrator.monitor.kill_session')
    def test_kill_session_tmux_mode(self, mock_kill_session, monitor):
        """Test _kill_session uses tmux backend in tmux mode."""
        monitor.config.ui_mode = "tmux"

        monitor._kill_session("issue-456")

        mock_kill_session.assert_called_once_with("issue-456")

    @patch('issue_orchestrator.iterm2.get_iterm_manager')
    def test_kill_session_iterm2_mode(self, mock_get_manager, monitor):
        """Test _kill_session uses iTerm2 backend in iterm2 mode."""
        monitor.config.ui_mode = "iterm2"
        mock_manager = MagicMock()
        mock_get_manager.return_value = mock_manager

        monitor._kill_session("issue-789")

        mock_manager.kill_session.assert_called_once_with(789)

    def test_send_exit_to_session_tmux_mode(self, monitor):
        """Test _send_exit_to_session returns False in tmux mode."""
        monitor.config.ui_mode = "tmux"

        result = monitor._send_exit_to_session(123)

        assert result is False

    @patch('issue_orchestrator.iterm2.get_iterm_manager')
    def test_send_exit_to_session_iterm2_mode(self, mock_get_manager, monitor):
        """Test _send_exit_to_session sends exit in iterm2 mode."""
        monitor.config.ui_mode = "iterm2"
        mock_manager = MagicMock()
        mock_manager.send_to_session.return_value = True
        mock_get_manager.return_value = mock_manager

        result = monitor._send_exit_to_session(456)

        assert result is True
        mock_manager.send_to_session.assert_called_once_with(456, "/exit")


class TestCheckSession:
    """Test check_session method."""

    @patch('issue_orchestrator.monitor.session_exists')
    def test_check_session_timed_out(self, mock_session_exists, monitor, sample_session):
        """Test check_session returns TIMED_OUT when session times out."""
        # Set up session that has timed out
        old_time = datetime.now() - timedelta(minutes=60)
        sample_session.started_at = old_time
        sample_session.agent_config.timeout_minutes = 30

        status = monitor.check_session(sample_session)

        assert status == SessionStatus.TIMED_OUT
        # Should not check if session exists
        mock_session_exists.assert_not_called()

    @patch('issue_orchestrator.monitor.session_exists')
    def test_check_session_still_running(self, mock_session_exists, monitor, sample_session):
        """Test check_session returns RUNNING when session is active."""
        mock_session_exists.return_value = True

        status = monitor.check_session(sample_session)

        assert status == SessionStatus.RUNNING
        mock_session_exists.assert_called_once_with("issue-123")

    @patch('issue_orchestrator.monitor.get_open_prs_for_branch')
    @patch('issue_orchestrator.monitor.session_exists')
    def test_check_session_running_with_pr_sends_exit(
        self, mock_session_exists, mock_get_prs, monitor, sample_session
    ):
        """Test that /exit is sent when session is running but has PR."""
        monitor.config.ui_mode = "iterm2"
        mock_pr = MagicMock()
        mock_get_prs.return_value = [mock_pr]

        with patch.object(monitor, '_session_exists', return_value=True):
            with patch.object(monitor, '_send_exit_to_session', return_value=True) as mock_send:
                status = monitor.check_session(sample_session)

        assert status == SessionStatus.RUNNING
        assert sample_session.exit_sent is True
        mock_send.assert_called_once_with(123)

    @patch('issue_orchestrator.monitor.get_open_prs_for_branch')
    @patch('issue_orchestrator.monitor.session_exists')
    def test_check_session_running_exit_sent_only_once(
        self, mock_session_exists, mock_get_prs, monitor, sample_session
    ):
        """Test that /exit is only sent once."""
        monitor.config.ui_mode = "iterm2"
        mock_pr = MagicMock()
        mock_get_prs.return_value = [mock_pr]
        sample_session.exit_sent = True

        with patch.object(monitor, '_session_exists', return_value=True):
            with patch.object(monitor, '_send_exit_to_session') as mock_send:
                status = monitor.check_session(sample_session)

        assert status == SessionStatus.RUNNING
        # Should not send again
        mock_send.assert_not_called()

    @patch('issue_orchestrator.monitor.get_open_prs_for_branch')
    @patch('issue_orchestrator.monitor.session_exists')
    def test_check_session_running_pr_check_exception(
        self, mock_session_exists, mock_get_prs, monitor, sample_session
    ):
        """Test that exceptions in PR check are handled gracefully."""
        mock_session_exists.return_value = True
        mock_get_prs.side_effect = Exception("GitHub API error")

        status = monitor.check_session(sample_session)

        assert status == SessionStatus.RUNNING
        assert sample_session.exit_sent is False

    @patch('issue_orchestrator.monitor.get_open_prs_for_branch')
    @patch('issue_orchestrator.monitor.session_exists')
    def test_check_session_completed_with_pr(
        self, mock_session_exists, mock_get_prs, monitor, sample_session
    ):
        """Test check_session returns COMPLETED when session exited with PR."""
        mock_session_exists.return_value = False
        mock_pr = MagicMock()
        mock_get_prs.return_value = [mock_pr]

        status = monitor.check_session(sample_session)

        assert status == SessionStatus.COMPLETED

    @patch('issue_orchestrator.monitor.get_open_prs_for_branch')
    @patch('issue_orchestrator.monitor.session_exists')
    def test_check_session_blocked_label(
        self, mock_session_exists, mock_get_prs, monitor, sample_session
    ):
        """Test check_session returns BLOCKED when issue has blocked label."""
        mock_session_exists.return_value = False
        mock_get_prs.return_value = []
        sample_session.issue.labels.append("blocked")

        status = monitor.check_session(sample_session)

        assert status == SessionStatus.BLOCKED

    @patch('issue_orchestrator.monitor.get_open_prs_for_branch')
    @patch('issue_orchestrator.monitor.session_exists')
    def test_check_session_needs_human_label(
        self, mock_session_exists, mock_get_prs, monitor, sample_session
    ):
        """Test check_session returns NEEDS_HUMAN when issue has needs-human label."""
        mock_session_exists.return_value = False
        mock_get_prs.return_value = []
        sample_session.issue.labels.append("needs-human")

        status = monitor.check_session(sample_session)

        assert status == SessionStatus.NEEDS_HUMAN

    @patch('issue_orchestrator.monitor.get_open_prs_for_branch')
    @patch('issue_orchestrator.monitor.session_exists')
    def test_check_session_failed_no_markers(
        self, mock_session_exists, mock_get_prs, monitor, sample_session
    ):
        """Test check_session returns FAILED when no completion markers found."""
        mock_session_exists.return_value = False
        mock_get_prs.return_value = []

        status = monitor.check_session(sample_session)

        assert status == SessionStatus.FAILED

    @patch('issue_orchestrator.monitor.get_open_prs_for_branch')
    @patch('issue_orchestrator.monitor.session_exists')
    def test_check_session_pr_check_exception_still_checks_labels(
        self, mock_session_exists, mock_get_prs, monitor, sample_session
    ):
        """Test that label checks still happen if PR check fails."""
        mock_session_exists.return_value = False
        mock_get_prs.side_effect = Exception("API error")
        sample_session.issue.labels.append("blocked")

        status = monitor.check_session(sample_session)

        assert status == SessionStatus.BLOCKED

    @patch('issue_orchestrator.monitor.get_open_prs_for_branch')
    @patch('issue_orchestrator.monitor.session_exists')
    def test_check_session_with_custom_labels(
        self, mock_session_exists, mock_get_prs, monitor, sample_session
    ):
        """Test check_session works with custom label configurations."""
        monitor.config.label_blocked = "custom-blocked"
        monitor.config.label_needs_human = "custom-needs-human"
        mock_session_exists.return_value = False
        mock_get_prs.return_value = []
        sample_session.issue.labels.append("custom-blocked")

        status = monitor.check_session(sample_session)

        assert status == SessionStatus.BLOCKED


class TestCheckAllSessions:
    """Test check_all_sessions method."""

    @patch('issue_orchestrator.monitor.session_exists')
    def test_check_all_sessions_empty_list(self, mock_session_exists, monitor):
        """Test checking empty list of sessions."""
        statuses = monitor.check_all_sessions([])

        assert statuses == {}
        mock_session_exists.assert_not_called()

    @patch('issue_orchestrator.monitor.session_exists')
    def test_check_all_sessions_multiple(
        self, mock_session_exists, monitor, sample_agent_config
    ):
        """Test checking multiple sessions."""
        issue1 = Issue(number=1, title="Issue 1", labels=["agent:web"])
        issue2 = Issue(number=2, title="Issue 2", labels=["agent:web"])

        session1 = Session(
            issue=issue1,
            agent_config=sample_agent_config,
            tmux_session_name="issue-1",
            worktree_path=Path("/tmp/w1"),
            branch_name="branch-1",
        )
        session2 = Session(
            issue=issue2,
            agent_config=sample_agent_config,
            tmux_session_name="issue-2",
            worktree_path=Path("/tmp/w2"),
            branch_name="branch-2",
        )

        mock_session_exists.return_value = True

        statuses = monitor.check_all_sessions([session1, session2])

        assert len(statuses) == 2
        assert statuses[1] == SessionStatus.RUNNING
        assert statuses[2] == SessionStatus.RUNNING

    @patch('issue_orchestrator.monitor.session_exists')
    def test_check_all_sessions_with_exception(
        self, mock_session_exists, monitor, sample_session
    ):
        """Test that exceptions in check_session result in FAILED status."""
        mock_session_exists.side_effect = Exception("Unexpected error")

        statuses = monitor.check_all_sessions([sample_session])

        assert statuses[123] == SessionStatus.FAILED

    @patch('issue_orchestrator.monitor.session_exists')
    @patch('issue_orchestrator.monitor.get_open_prs_for_branch')
    def test_check_all_sessions_mixed_statuses(
        self, mock_get_prs, mock_session_exists, monitor, sample_agent_config
    ):
        """Test checking sessions with different statuses."""
        # Session 1: Running
        issue1 = Issue(number=1, title="Running", labels=["agent:web"])
        session1 = Session(
            issue=issue1,
            agent_config=sample_agent_config,
            tmux_session_name="issue-1",
            worktree_path=Path("/tmp/w1"),
            branch_name="branch-1",
        )

        # Session 2: Completed (has PR)
        issue2 = Issue(number=2, title="Completed", labels=["agent:web"])
        session2 = Session(
            issue=issue2,
            agent_config=sample_agent_config,
            tmux_session_name="issue-2",
            worktree_path=Path("/tmp/w2"),
            branch_name="branch-2",
        )

        # Session 3: Blocked
        issue3 = Issue(number=3, title="Blocked", labels=["agent:web", "blocked"])
        session3 = Session(
            issue=issue3,
            agent_config=sample_agent_config,
            tmux_session_name="issue-3",
            worktree_path=Path("/tmp/w3"),
            branch_name="branch-3",
        )

        def session_exists_side_effect(name):
            return name == "issue-1"

        def get_prs_side_effect(repo, branch):
            if branch == "branch-2":
                return [MagicMock()]
            return []

        mock_session_exists.side_effect = session_exists_side_effect
        mock_get_prs.side_effect = get_prs_side_effect

        statuses = monitor.check_all_sessions([session1, session2, session3])

        assert statuses[1] == SessionStatus.RUNNING
        assert statuses[2] == SessionStatus.COMPLETED
        assert statuses[3] == SessionStatus.BLOCKED


class TestHandleCompletion:
    """Test handle_completion method."""

    @patch('issue_orchestrator.monitor.remove_label')
    @patch('issue_orchestrator.monitor.add_label')
    @patch('issue_orchestrator.monitor.kill_session')
    def test_handle_completion_timed_out(
        self, mock_kill, mock_add_label, mock_remove_label, monitor, sample_session
    ):
        """Test handling TIMED_OUT status."""
        monitor.handle_completion(sample_session, SessionStatus.TIMED_OUT)

        # Should kill session
        mock_kill.assert_called_once_with("issue-123")

        # Should add timed-out label
        mock_add_label.assert_called_once_with(
            repo="owner/repo",
            issue_number=123,
            label="timed-out",
        )

        # Should remove in-progress label
        mock_remove_label.assert_called_once_with(
            repo="owner/repo",
            issue_number=123,
            label="in-progress",
        )

    @patch('issue_orchestrator.monitor.remove_label')
    @patch('issue_orchestrator.monitor.add_label')
    def test_handle_completion_failed(
        self, mock_add_label, mock_remove_label, monitor, sample_session
    ):
        """Test handling FAILED status."""
        monitor.handle_completion(sample_session, SessionStatus.FAILED)

        # Should add failed label
        mock_add_label.assert_called_once_with(
            repo="owner/repo",
            issue_number=123,
            label="failed",
        )

        # Should remove in-progress label
        mock_remove_label.assert_called_once_with(
            repo="owner/repo",
            issue_number=123,
            label="in-progress",
        )

    @patch('issue_orchestrator.monitor.remove_label')
    def test_handle_completion_completed(
        self, mock_remove_label, monitor, sample_session
    ):
        """Test handling COMPLETED status."""
        monitor.handle_completion(sample_session, SessionStatus.COMPLETED)

        # Should remove in-progress label
        mock_remove_label.assert_called_once_with(
            repo="owner/repo",
            issue_number=123,
            label="in-progress",
        )

    @patch('issue_orchestrator.monitor.remove_label')
    def test_handle_completion_blocked(
        self, mock_remove_label, monitor, sample_session
    ):
        """Test handling BLOCKED status."""
        monitor.handle_completion(sample_session, SessionStatus.BLOCKED)

        # Should remove in-progress label
        mock_remove_label.assert_called_once_with(
            repo="owner/repo",
            issue_number=123,
            label="in-progress",
        )

    @patch('issue_orchestrator.monitor.remove_label')
    def test_handle_completion_needs_human(
        self, mock_remove_label, monitor, sample_session
    ):
        """Test handling NEEDS_HUMAN status."""
        monitor.handle_completion(sample_session, SessionStatus.NEEDS_HUMAN)

        # Should remove in-progress label
        mock_remove_label.assert_called_once_with(
            repo="owner/repo",
            issue_number=123,
            label="in-progress",
        )

    @patch('issue_orchestrator.monitor.remove_label')
    @patch('issue_orchestrator.monitor.add_label')
    @patch('issue_orchestrator.monitor.kill_session')
    def test_handle_completion_timed_out_kill_fails(
        self, mock_kill, mock_add_label, mock_remove_label, monitor, sample_session
    ):
        """Test that kill failure doesn't prevent label updates."""
        mock_kill.side_effect = Exception("Kill failed")

        monitor.handle_completion(sample_session, SessionStatus.TIMED_OUT)

        # Should still try to add label
        mock_add_label.assert_called_once()
        mock_remove_label.assert_called_once()

    @patch('issue_orchestrator.monitor.remove_label')
    @patch('issue_orchestrator.monitor.add_label')
    def test_handle_completion_add_label_fails(
        self, mock_add_label, mock_remove_label, monitor, sample_session
    ):
        """Test that add_label failure doesn't prevent remove_label."""
        mock_add_label.side_effect = Exception("API error")

        monitor.handle_completion(sample_session, SessionStatus.FAILED)

        # Should still remove in-progress label
        mock_remove_label.assert_called_once()

    @patch('issue_orchestrator.monitor.remove_label')
    def test_handle_completion_remove_label_exception(
        self, mock_remove_label, monitor, sample_session
    ):
        """Test that remove_label exception is caught."""
        mock_remove_label.side_effect = Exception("API error")

        # Should not raise
        monitor.handle_completion(sample_session, SessionStatus.COMPLETED)

    def test_handle_completion_running_does_nothing(self, monitor, sample_session):
        """Test that RUNNING status does nothing."""
        with patch('issue_orchestrator.monitor.remove_label') as mock_remove:
            monitor.handle_completion(sample_session, SessionStatus.RUNNING)
            mock_remove.assert_not_called()

    @patch('issue_orchestrator.monitor.remove_label')
    @patch('issue_orchestrator.monitor.kill_session')
    def test_handle_completion_close_completed_tabs(
        self, mock_kill, mock_remove_label, monitor, sample_session
    ):
        """Test auto-closing completed tabs when enabled."""
        monitor.config.close_completed_tabs = True

        monitor.handle_completion(sample_session, SessionStatus.COMPLETED)

        # Should close the tab
        mock_kill.assert_called_once_with("issue-123")

    @patch('issue_orchestrator.monitor.remove_label')
    @patch('issue_orchestrator.monitor.kill_session')
    def test_handle_completion_dont_close_completed_tabs(
        self, mock_kill, mock_remove_label, monitor, sample_session
    ):
        """Test not closing completed tabs when disabled."""
        monitor.config.close_completed_tabs = False

        monitor.handle_completion(sample_session, SessionStatus.COMPLETED)

        # Should not close the tab (only remove label)
        mock_kill.assert_not_called()

    @patch('issue_orchestrator.monitor.remove_label')
    @patch('issue_orchestrator.monitor.add_label')
    @patch('issue_orchestrator.monitor.kill_session')
    def test_handle_completion_close_failed_tabs(
        self, mock_kill, mock_add_label, mock_remove_label, monitor, sample_session
    ):
        """Test auto-closing failed tabs when enabled."""
        monitor.config.close_failed_tabs = True

        monitor.handle_completion(sample_session, SessionStatus.FAILED)

        # Should close the tab (kill called twice: once for tab close)
        assert mock_kill.call_count == 1

    @patch('issue_orchestrator.monitor.remove_label')
    @patch('issue_orchestrator.monitor.add_label')
    @patch('issue_orchestrator.monitor.kill_session')
    def test_handle_completion_dont_close_failed_tabs(
        self, mock_kill, mock_add_label, mock_remove_label, monitor, sample_session
    ):
        """Test not closing failed tabs when disabled."""
        monitor.config.close_failed_tabs = False

        monitor.handle_completion(sample_session, SessionStatus.FAILED)

        # Should not close the tab
        mock_kill.assert_not_called()

    @patch('issue_orchestrator.monitor.remove_label')
    @patch('issue_orchestrator.monitor.kill_session')
    def test_handle_completion_close_blocked_tabs(
        self, mock_kill, mock_remove_label, monitor, sample_session
    ):
        """Test closing blocked tabs when close_failed_tabs is enabled."""
        monitor.config.close_failed_tabs = True

        monitor.handle_completion(sample_session, SessionStatus.BLOCKED)

        # Should close the tab
        mock_kill.assert_called_once_with("issue-123")

    @patch('issue_orchestrator.monitor.remove_label')
    @patch('issue_orchestrator.monitor.kill_session')
    def test_handle_completion_close_needs_human_tabs(
        self, mock_kill, mock_remove_label, monitor, sample_session
    ):
        """Test closing needs-human tabs when close_failed_tabs is enabled."""
        monitor.config.close_failed_tabs = True

        monitor.handle_completion(sample_session, SessionStatus.NEEDS_HUMAN)

        # Should close the tab
        mock_kill.assert_called_once_with("issue-123")

    @patch('issue_orchestrator.monitor.remove_label')
    @patch('issue_orchestrator.monitor.add_label')
    @patch('issue_orchestrator.monitor.kill_session')
    def test_handle_completion_timed_out_always_kills(
        self, mock_kill, mock_add_label, mock_remove_label, monitor, sample_session
    ):
        """Test that TIMED_OUT always kills session regardless of close config."""
        monitor.config.close_completed_tabs = False
        monitor.config.close_failed_tabs = False

        monitor.handle_completion(sample_session, SessionStatus.TIMED_OUT)

        # Should still kill the session for TIMED_OUT (kill called once for timeout)
        mock_kill.assert_called_once_with("issue-123")

    @patch('issue_orchestrator.monitor.remove_label')
    @patch('issue_orchestrator.monitor.kill_session')
    def test_handle_completion_tab_close_exception(
        self, mock_kill, mock_remove_label, monitor, sample_session
    ):
        """Test that tab close exceptions are handled gracefully."""
        monitor.config.close_completed_tabs = True
        mock_kill.side_effect = Exception("Close failed")

        # Should not raise
        monitor.handle_completion(sample_session, SessionStatus.COMPLETED)

    @patch('issue_orchestrator.monitor.remove_label')
    def test_handle_completion_with_custom_in_progress_label(
        self, mock_remove_label, monitor, sample_session
    ):
        """Test that custom in-progress label is used."""
        monitor.config.label_in_progress = "custom-in-progress"

        monitor.handle_completion(sample_session, SessionStatus.COMPLETED)

        mock_remove_label.assert_called_once_with(
            repo="owner/repo",
            issue_number=123,
            label="custom-in-progress",
        )

    @patch('issue_orchestrator.monitor.remove_label')
    def test_handle_completion_unexpected_exception(
        self, mock_remove_label, monitor, sample_session
    ):
        """Test that unexpected exceptions are caught and logged."""
        mock_remove_label.side_effect = Exception("Unexpected error")

        # Should not raise
        monitor.handle_completion(sample_session, SessionStatus.COMPLETED)

    @patch('issue_orchestrator.monitor.remove_label')
    @patch('issue_orchestrator.monitor.add_label')
    @patch('issue_orchestrator.monitor.kill_session')
    def test_handle_completion_timed_out_add_label_error(
        self, mock_kill, mock_add_label, mock_remove_label, monitor, sample_session
    ):
        """Test that error adding timed-out label doesn't prevent cleanup."""
        mock_add_label.side_effect = Exception("GitHub API error")

        # Should not raise
        monitor.handle_completion(sample_session, SessionStatus.TIMED_OUT)

        # Should still try to remove in-progress label
        mock_remove_label.assert_called_once()

    def test_handle_completion_outer_exception_handler(self, monitor):
        """Test the outer exception handler catches unexpected errors."""
        # Create a session where accessing issue.number raises an exception
        broken_session = MagicMock()
        broken_session.issue.number = MagicMock(side_effect=Exception("Broken attribute"))

        # Configure to raise when .number is accessed
        type(broken_session.issue).number = MagicMock(side_effect=Exception("Broken"))

        # Should catch and log the exception without raising
        monitor.handle_completion(broken_session, SessionStatus.COMPLETED)


class TestSessionMonitorIntegration:
    """Integration tests combining multiple methods."""

    @patch('issue_orchestrator.monitor.remove_label')
    @patch('issue_orchestrator.monitor.add_label')
    @patch('issue_orchestrator.monitor.kill_session')
    @patch('issue_orchestrator.monitor.get_open_prs_for_branch')
    @patch('issue_orchestrator.monitor.session_exists')
    def test_full_workflow_timed_out(
        self,
        mock_exists,
        mock_get_prs,
        mock_kill,
        mock_add_label,
        mock_remove_label,
        monitor,
        sample_session,
    ):
        """Test full workflow for a timed-out session."""
        # Make session timed out
        old_time = datetime.now() - timedelta(minutes=60)
        sample_session.started_at = old_time
        sample_session.agent_config.timeout_minutes = 30

        # Check status
        status = monitor.check_session(sample_session)
        assert status == SessionStatus.TIMED_OUT

        # Handle completion
        monitor.handle_completion(sample_session, status)

        # Verify correct actions
        mock_kill.assert_called_once_with("issue-123")
        mock_add_label.assert_called_once_with(
            repo="owner/repo",
            issue_number=123,
            label="timed-out",
        )
        mock_remove_label.assert_called_once_with(
            repo="owner/repo",
            issue_number=123,
            label="in-progress",
        )

    @patch('issue_orchestrator.monitor.remove_label')
    @patch('issue_orchestrator.monitor.get_open_prs_for_branch')
    @patch('issue_orchestrator.monitor.session_exists')
    def test_full_workflow_completed(
        self,
        mock_exists,
        mock_get_prs,
        mock_remove_label,
        monitor,
        sample_session,
    ):
        """Test full workflow for a completed session."""
        mock_exists.return_value = False
        mock_pr = MagicMock()
        mock_get_prs.return_value = [mock_pr]

        # Check status
        status = monitor.check_session(sample_session)
        assert status == SessionStatus.COMPLETED

        # Handle completion
        monitor.handle_completion(sample_session, status)

        # Verify correct actions
        mock_remove_label.assert_called_once_with(
            repo="owner/repo",
            issue_number=123,
            label="in-progress",
        )

    @patch('issue_orchestrator.monitor.remove_label')
    @patch('issue_orchestrator.monitor.get_open_prs_for_branch')
    @patch('issue_orchestrator.monitor.session_exists')
    def test_check_and_handle_multiple_sessions(
        self,
        mock_exists,
        mock_get_prs,
        mock_remove_label,
        monitor,
        sample_agent_config,
    ):
        """Test checking and handling multiple sessions."""
        # Create multiple sessions
        issue1 = Issue(number=1, title="Running", labels=["agent:web"])
        issue2 = Issue(number=2, title="Completed", labels=["agent:web"])
        issue3 = Issue(number=3, title="Failed", labels=["agent:web"])

        session1 = Session(
            issue=issue1,
            agent_config=sample_agent_config,
            tmux_session_name="issue-1",
            worktree_path=Path("/tmp/w1"),
            branch_name="branch-1",
        )
        session2 = Session(
            issue=issue2,
            agent_config=sample_agent_config,
            tmux_session_name="issue-2",
            worktree_path=Path("/tmp/w2"),
            branch_name="branch-2",
        )
        session3 = Session(
            issue=issue3,
            agent_config=sample_agent_config,
            tmux_session_name="issue-3",
            worktree_path=Path("/tmp/w3"),
            branch_name="branch-3",
        )

        def exists_side_effect(name):
            return name == "issue-1"

        def get_prs_side_effect(repo, branch):
            if branch == "branch-2":
                return [MagicMock()]
            return []

        mock_exists.side_effect = exists_side_effect
        mock_get_prs.side_effect = get_prs_side_effect

        # Check all sessions
        statuses = monitor.check_all_sessions([session1, session2, session3])

        assert statuses[1] == SessionStatus.RUNNING
        assert statuses[2] == SessionStatus.COMPLETED
        assert statuses[3] == SessionStatus.FAILED

        # Handle completions for completed and failed
        for session in [session2, session3]:
            status = statuses[session.issue.number]
            if status != SessionStatus.RUNNING:
                monitor.handle_completion(session, status)

        # Should have removed in-progress label from both
        assert mock_remove_label.call_count == 2
