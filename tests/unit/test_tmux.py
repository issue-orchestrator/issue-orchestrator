"""Unit tests for tmux.py module."""

import os
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

import pytest

from issue_orchestrator import tmux


@pytest.fixture(autouse=True)
def reset_manager():
    """Reset the global manager between tests."""
    tmux._manager = None
    yield
    tmux._manager = None


@pytest.fixture
def mock_server():
    """Create a mock libtmux Server."""
    server = MagicMock()
    return server


@pytest.fixture
def mock_session():
    """Create a mock libtmux Session."""
    session = MagicMock()
    session.name = tmux.SESSION_NAME
    return session


@pytest.fixture
def mock_window():
    """Create a mock libtmux Window."""
    window = MagicMock()
    window.name = "issue-42"
    window.active_pane = MagicMock()
    return window


class TestTmuxManager:
    """Tests for TmuxManager class."""

    def test_init(self):
        """Test TmuxManager initialization."""
        manager = tmux.TmuxManager()
        assert manager._server is None
        assert manager._session is None

    def test_server_property_creates_server(self, mock_server):
        """Test server property creates a server on first access."""
        manager = tmux.TmuxManager()
        with patch("issue_orchestrator.tmux.libtmux.Server", return_value=mock_server):
            server = manager.server
            assert server == mock_server
            assert manager._server == mock_server

    def test_server_property_returns_cached(self, mock_server):
        """Test server property returns cached server."""
        manager = tmux.TmuxManager()
        manager._server = mock_server
        assert manager.server == mock_server

    def test_session_property_gets_existing(self, mock_server, mock_session):
        """Test session property gets existing session."""
        manager = tmux.TmuxManager()
        manager._server = mock_server
        mock_server.sessions.get.return_value = mock_session

        session = manager.session
        assert session == mock_session
        mock_server.sessions.get.assert_called_once_with(session_name=tmux.SESSION_NAME)

    def test_session_property_handles_missing(self, mock_server):
        """Test session property returns None when session doesn't exist."""
        manager = tmux.TmuxManager()
        manager._server = mock_server
        mock_server.sessions.get.side_effect = Exception("Session not found")

        session = manager.session
        assert session is None

    def test_session_property_returns_cached(self, mock_session):
        """Test session property returns cached session."""
        manager = tmux.TmuxManager()
        manager._session = mock_session
        assert manager.session == mock_session

    def test_ensure_session_returns_existing(self, mock_session):
        """Test ensure_session returns existing session."""
        manager = tmux.TmuxManager()
        manager._session = mock_session

        session = manager.ensure_session()
        assert session == mock_session

    def test_ensure_session_creates_new(self, mock_server, mock_session):
        """Test ensure_session creates new session if needed."""
        manager = tmux.TmuxManager()
        manager._server = mock_server
        # First call to sessions.get() should fail (no session exists)
        mock_server.sessions.get.side_effect = Exception("Session not found")
        mock_server.new_session.return_value = mock_session

        session = manager.ensure_session()
        assert session == mock_session
        mock_server.new_session.assert_called_once_with(
            session_name=tmux.SESSION_NAME,
            window_name=tmux.DASHBOARD_WINDOW,
        )

    def test_has_session_true(self, mock_session):
        """Test has_session returns True when session exists."""
        manager = tmux.TmuxManager()
        manager._session = mock_session
        assert manager.has_session() is True

    def test_has_session_false(self):
        """Test has_session returns False when session doesn't exist."""
        manager = tmux.TmuxManager()
        manager._session = None
        assert manager.has_session() is False

    def test_create_issue_window_success(self, mock_session, mock_window):
        """Test create_issue_window successfully creates a window."""
        manager = tmux.TmuxManager()
        manager._session = mock_session
        mock_session.windows.filter.return_value = []
        mock_session.new_window.return_value = mock_window

        working_dir = Path("/test/dir")
        window = manager.create_issue_window(42, "echo test", working_dir)

        assert window == mock_window
        mock_session.windows.filter.assert_called_once_with(window_name="issue-42")
        mock_session.new_window.assert_called_once_with(
            window_name="issue-42",
            start_directory=str(working_dir),
        )
        mock_window.active_pane.send_keys.assert_called_once_with("echo test")

    def test_create_issue_window_already_exists(self, mock_session, mock_window):
        """Test create_issue_window raises error if window exists."""
        manager = tmux.TmuxManager()
        manager._session = mock_session
        mock_session.windows.filter.return_value = [mock_window]

        with pytest.raises(ValueError, match="Window issue-42 already exists"):
            manager.create_issue_window(42, "echo test", Path("/test/dir"))

    def test_window_exists_true(self, mock_session, mock_window):
        """Test window_exists returns True when window exists."""
        manager = tmux.TmuxManager()
        manager._session = mock_session
        mock_session.windows.filter.return_value = [mock_window]

        assert manager.window_exists(42) is True
        mock_session.windows.filter.assert_called_once_with(window_name="issue-42")

    def test_window_exists_false(self, mock_session):
        """Test window_exists returns False when window doesn't exist."""
        manager = tmux.TmuxManager()
        manager._session = mock_session
        mock_session.windows.filter.return_value = []

        assert manager.window_exists(42) is False

    def test_window_exists_no_session(self):
        """Test window_exists returns False when no session."""
        manager = tmux.TmuxManager()
        manager._session = None
        assert manager.window_exists(42) is False

    def test_get_window_success(self, mock_session, mock_window):
        """Test get_window returns window when it exists."""
        manager = tmux.TmuxManager()
        manager._session = mock_session
        mock_session.windows.filter.return_value = [mock_window]

        window = manager.get_window(42)
        assert window == mock_window

    def test_get_window_not_found(self, mock_session):
        """Test get_window returns None when window doesn't exist."""
        manager = tmux.TmuxManager()
        manager._session = mock_session
        mock_session.windows.filter.return_value = []

        window = manager.get_window(42)
        assert window is None

    def test_get_window_no_session(self):
        """Test get_window returns None when no session."""
        manager = tmux.TmuxManager()
        manager._session = None
        assert manager.get_window(42) is None

    def test_kill_window_success(self, mock_session, mock_window):
        """Test kill_window kills the window."""
        manager = tmux.TmuxManager()
        manager._session = mock_session
        mock_session.windows.filter.return_value = [mock_window]

        manager.kill_window(42)
        mock_window.kill.assert_called_once()

    def test_kill_window_not_found(self, mock_session):
        """Test kill_window does nothing when window doesn't exist."""
        manager = tmux.TmuxManager()
        manager._session = mock_session
        mock_session.windows.filter.return_value = []

        # Should not raise error
        manager.kill_window(42)

    def test_select_window_success(self, mock_session, mock_window):
        """Test select_window selects the window."""
        manager = tmux.TmuxManager()
        manager._session = mock_session
        mock_session.windows.filter.return_value = [mock_window]

        result = manager.select_window(42)
        assert result is True
        mock_window.select.assert_called_once()

    def test_select_window_not_found(self, mock_session):
        """Test select_window returns False when window doesn't exist."""
        manager = tmux.TmuxManager()
        manager._session = mock_session
        mock_session.windows.filter.return_value = []

        result = manager.select_window(42)
        assert result is False

    def test_select_dashboard_success(self, mock_session, mock_window):
        """Test select_dashboard selects the dashboard window."""
        manager = tmux.TmuxManager()
        manager._session = mock_session
        dashboard_window = MagicMock()
        mock_session.windows.filter.return_value = [dashboard_window]

        result = manager.select_dashboard()
        assert result is True
        dashboard_window.select.assert_called_once()
        mock_session.windows.filter.assert_called_once_with(
            window_name=tmux.DASHBOARD_WINDOW
        )

    def test_select_dashboard_not_found(self, mock_session):
        """Test select_dashboard returns False when dashboard doesn't exist."""
        manager = tmux.TmuxManager()
        manager._session = mock_session
        mock_session.windows.filter.return_value = []

        result = manager.select_dashboard()
        assert result is False

    def test_select_dashboard_no_session(self):
        """Test select_dashboard returns False when no session."""
        manager = tmux.TmuxManager()
        manager._session = None
        assert manager.select_dashboard() is False

    def test_list_issue_windows(self, mock_session):
        """Test list_issue_windows returns all issue numbers."""
        manager = tmux.TmuxManager()
        manager._session = mock_session

        window1 = MagicMock()
        window1.name = "issue-42"
        window2 = MagicMock()
        window2.name = "issue-123"
        dashboard = MagicMock()
        dashboard.name = "dashboard"
        mock_session.windows = [window1, window2, dashboard]

        issues = manager.list_issue_windows()
        assert issues == [42, 123]

    def test_list_issue_windows_empty(self, mock_session):
        """Test list_issue_windows returns empty list when no issue windows."""
        manager = tmux.TmuxManager()
        manager._session = mock_session
        mock_session.windows = []

        issues = manager.list_issue_windows()
        assert issues == []

    def test_list_issue_windows_no_session(self):
        """Test list_issue_windows returns empty list when no session."""
        manager = tmux.TmuxManager()
        manager._session = None
        assert manager.list_issue_windows() == []

    def test_list_issue_windows_skips_invalid_names(self, mock_session):
        """Test list_issue_windows skips windows with invalid names."""
        manager = tmux.TmuxManager()
        manager._session = mock_session

        window1 = MagicMock()
        window1.name = "issue-42"
        window2 = MagicMock()
        window2.name = "issue-invalid"
        window3 = MagicMock()
        window3.name = "issue-123"
        mock_session.windows = [window1, window2, window3]

        issues = manager.list_issue_windows()
        assert issues == [42, 123]

    def test_capture_pane_output_success(self, mock_session, mock_window):
        """Test capture_pane_output captures output."""
        manager = tmux.TmuxManager()
        manager._session = mock_session
        mock_session.windows.filter.return_value = [mock_window]
        mock_window.active_pane.capture_pane.return_value = ["line1", "line2", "line3"]

        output = manager.capture_pane_output(42, lines=10)
        assert output == "line1\nline2\nline3"
        mock_window.active_pane.capture_pane.assert_called_once_with(start=-10)

    def test_capture_pane_output_default_lines(self, mock_session, mock_window):
        """Test capture_pane_output uses default lines."""
        manager = tmux.TmuxManager()
        manager._session = mock_session
        mock_session.windows.filter.return_value = [mock_window]
        mock_window.active_pane.capture_pane.return_value = ["line1"]

        output = manager.capture_pane_output(42)
        mock_window.active_pane.capture_pane.assert_called_once_with(start=-20)

    def test_capture_pane_output_empty(self, mock_session, mock_window):
        """Test capture_pane_output handles empty output."""
        manager = tmux.TmuxManager()
        manager._session = mock_session
        mock_session.windows.filter.return_value = [mock_window]
        mock_window.active_pane.capture_pane.return_value = []

        output = manager.capture_pane_output(42)
        assert output == ""

    def test_capture_pane_output_none(self, mock_session, mock_window):
        """Test capture_pane_output handles None output."""
        manager = tmux.TmuxManager()
        manager._session = mock_session
        mock_session.windows.filter.return_value = [mock_window]
        mock_window.active_pane.capture_pane.return_value = None

        output = manager.capture_pane_output(42)
        assert output == ""

    def test_capture_pane_output_no_window(self, mock_session):
        """Test capture_pane_output returns None when window doesn't exist."""
        manager = tmux.TmuxManager()
        manager._session = mock_session
        mock_session.windows.filter.return_value = []

        output = manager.capture_pane_output(42)
        assert output is None

    def test_kill_session_success(self, mock_session):
        """Test kill_session kills the session."""
        manager = tmux.TmuxManager()
        manager._session = mock_session

        manager.kill_session()
        mock_session.kill.assert_called_once()
        assert manager._session is None

    def test_kill_session_no_session(self):
        """Test kill_session does nothing when no session."""
        manager = tmux.TmuxManager()
        manager._session = None

        # Should not raise error
        manager.kill_session()
        assert manager._session is None


class TestGetManager:
    """Tests for get_manager function."""

    def test_get_manager_creates_singleton(self):
        """Test get_manager creates a singleton instance."""
        manager1 = tmux.get_manager()
        manager2 = tmux.get_manager()
        assert manager1 is manager2
        assert isinstance(manager1, tmux.TmuxManager)

    def test_get_manager_uses_global(self):
        """Test get_manager uses the global _manager."""
        existing = tmux.TmuxManager()
        tmux._manager = existing
        manager = tmux.get_manager()
        assert manager is existing


class TestBackwardCompatibleFunctions:
    """Tests for backward-compatible wrapper functions."""

    def test_create_session_success(self, mock_session, mock_window):
        """Test create_session creates a window."""
        with patch("issue_orchestrator.tmux.get_manager") as mock_get_manager:
            mock_manager = MagicMock()
            mock_get_manager.return_value = mock_manager

            working_dir = Path("/test/dir")
            tmux.create_session("issue-42", "echo test", working_dir)

            mock_manager.create_issue_window.assert_called_once_with(
                42, "echo test", working_dir, title=None
            )

    def test_create_session_invalid_name(self):
        """Test create_session raises error for invalid session name."""
        with pytest.raises(ValueError, match="Expected session name like 'issue-42'"):
            tmux.create_session("invalid", "echo test", Path("/test/dir"))

    def test_session_exists_true(self):
        """Test session_exists returns True when window exists."""
        with patch("issue_orchestrator.tmux.get_manager") as mock_get_manager:
            mock_manager = MagicMock()
            mock_manager.window_exists.return_value = True
            mock_get_manager.return_value = mock_manager

            result = tmux.session_exists("issue-42")
            assert result is True
            mock_manager.window_exists.assert_called_once_with(42)

    def test_session_exists_false(self):
        """Test session_exists returns False when window doesn't exist."""
        with patch("issue_orchestrator.tmux.get_manager") as mock_get_manager:
            mock_manager = MagicMock()
            mock_manager.window_exists.return_value = False
            mock_get_manager.return_value = mock_manager

            result = tmux.session_exists("issue-42")
            assert result is False

    def test_session_exists_invalid_name(self):
        """Test session_exists returns False for invalid session name."""
        result = tmux.session_exists("invalid")
        assert result is False

    def test_kill_session_success(self):
        """Test kill_session kills the window."""
        with patch("issue_orchestrator.tmux.get_manager") as mock_get_manager:
            mock_manager = MagicMock()
            mock_get_manager.return_value = mock_manager

            tmux.kill_session("issue-42")
            mock_manager.kill_window.assert_called_once_with(42)

    def test_kill_session_invalid_name(self):
        """Test kill_session does nothing for invalid session name."""
        with patch("issue_orchestrator.tmux.get_manager") as mock_get_manager:
            mock_manager = MagicMock()
            mock_get_manager.return_value = mock_manager

            # Should not raise error
            tmux.kill_session("invalid")
            mock_manager.kill_window.assert_not_called()

    def test_list_sessions(self):
        """Test list_sessions returns issue session names."""
        with patch("issue_orchestrator.tmux.get_manager") as mock_get_manager:
            mock_manager = MagicMock()
            mock_manager.list_issue_windows.return_value = [42, 123]
            mock_get_manager.return_value = mock_manager

            sessions = tmux.list_sessions()
            assert sessions == ["issue-42", "issue-123"]

    def test_list_sessions_empty(self):
        """Test list_sessions returns empty list."""
        with patch("issue_orchestrator.tmux.get_manager") as mock_get_manager:
            mock_manager = MagicMock()
            mock_manager.list_issue_windows.return_value = []
            mock_get_manager.return_value = mock_manager

            sessions = tmux.list_sessions()
            assert sessions == []

    def test_attach_session(self):
        """Test attach_session calls os.execvp."""
        with patch("issue_orchestrator.tmux.os.execvp") as mock_execvp:
            tmux.attach_session("issue-42")
            mock_execvp.assert_called_once_with(
                "tmux", ["tmux", "attach-session", "-t", tmux.SESSION_NAME]
            )

    def test_send_keys_with_enter(self, mock_session, mock_window):
        """Test send_keys sends keys with enter."""
        with patch("issue_orchestrator.tmux.get_manager") as mock_get_manager:
            mock_manager = MagicMock()
            mock_manager.get_window.return_value = mock_window
            mock_get_manager.return_value = mock_manager

            tmux.send_keys("issue-42", "echo test", enter=True)
            mock_window.active_pane.send_keys.assert_called_once_with("echo test")

    def test_send_keys_without_enter(self, mock_session, mock_window):
        """Test send_keys sends keys without enter."""
        with patch("issue_orchestrator.tmux.get_manager") as mock_get_manager:
            mock_manager = MagicMock()
            mock_manager.get_window.return_value = mock_window
            mock_get_manager.return_value = mock_manager

            tmux.send_keys("issue-42", "echo test", enter=False)
            mock_window.active_pane.send_keys.assert_called_once_with(
                "echo test", enter=False
            )

    def test_send_keys_no_window(self):
        """Test send_keys does nothing when window doesn't exist."""
        with patch("issue_orchestrator.tmux.get_manager") as mock_get_manager:
            mock_manager = MagicMock()
            mock_manager.get_window.return_value = None
            mock_get_manager.return_value = mock_manager

            # Should not raise error
            tmux.send_keys("issue-42", "echo test")

    def test_send_keys_invalid_name(self):
        """Test send_keys does nothing for invalid session name."""
        with patch("issue_orchestrator.tmux.get_manager") as mock_get_manager:
            mock_manager = MagicMock()
            mock_get_manager.return_value = mock_manager

            # Should not raise error
            tmux.send_keys("invalid", "echo test")
            mock_manager.get_window.assert_not_called()


class TestConstants:
    """Tests for module constants."""

    def test_session_name(self):
        """Test SESSION_NAME constant."""
        assert tmux.SESSION_NAME == "orchestrator"

    def test_dashboard_window(self):
        """Test DASHBOARD_WINDOW constant."""
        assert tmux.DASHBOARD_WINDOW == "dashboard"
