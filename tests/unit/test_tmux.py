"""Unit tests for tmux.py module."""

import os
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

import pytest

from issue_orchestrator.adapters.terminal import _tmux as tmux


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
        with patch("issue_orchestrator.adapters.terminal._tmux.libtmux.Server", return_value=mock_server):
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

    def test_has_session_false(self, mock_server):
        """Test has_session returns False when session doesn't exist."""
        manager = tmux.TmuxManager()
        manager._server = mock_server
        manager._session = None
        # Mock server.sessions.get to return None (no session found)
        mock_server.sessions.get.return_value = None
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
        # send_keys is called twice: first to add scripts to PATH, then to run command
        assert mock_window.active_pane.send_keys.call_count == 2
        # Second (last) call should be the actual command
        mock_window.active_pane.send_keys.assert_called_with("echo test")

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

    def test_select_dashboard_no_session(self, mock_server):
        """Test select_dashboard returns False when no session."""
        manager = tmux.TmuxManager()
        manager._server = mock_server
        manager._session = None
        # Mock server.sessions.get to return None (no session found)
        mock_server.sessions.get.return_value = None
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

    def test_list_issue_windows_no_session(self, mock_server):
        """Test list_issue_windows returns empty list when no session."""
        manager = tmux.TmuxManager()
        manager._server = mock_server
        # Mock server.sessions.get to return None (no session found)
        mock_server.sessions.get.return_value = None
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
        with patch("issue_orchestrator.adapters.terminal._tmux.get_manager") as mock_get_manager:
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
        with patch("issue_orchestrator.adapters.terminal._tmux.get_manager") as mock_get_manager:
            mock_manager = MagicMock()
            mock_manager.window_exists_by_name.return_value = True
            mock_get_manager.return_value = mock_manager

            result = tmux.session_exists("issue-42")
            assert result is True
            mock_manager.window_exists_by_name.assert_called_once_with("issue-42")

    def test_session_exists_false(self):
        """Test session_exists returns False when window doesn't exist."""
        with patch("issue_orchestrator.adapters.terminal._tmux.get_manager") as mock_get_manager:
            mock_manager = MagicMock()
            mock_manager.window_exists_by_name.return_value = False
            mock_get_manager.return_value = mock_manager

            result = tmux.session_exists("issue-42")
            assert result is False

    def test_session_exists_invalid_name(self):
        """Test session_exists returns False for invalid session name."""
        result = tmux.session_exists("invalid")
        assert result is False

    def test_kill_session_success(self):
        """Test kill_session kills the window."""
        with patch("issue_orchestrator.adapters.terminal._tmux.get_manager") as mock_get_manager:
            mock_manager = MagicMock()
            mock_get_manager.return_value = mock_manager

            tmux.kill_session("issue-42")
            mock_manager.kill_window_by_name.assert_called_once_with("issue-42")

    def test_kill_session_invalid_name(self):
        """Test kill_session does nothing for invalid session name."""
        with patch("issue_orchestrator.adapters.terminal._tmux.get_manager") as mock_get_manager:
            mock_manager = MagicMock()
            mock_get_manager.return_value = mock_manager

            # Should not raise error
            tmux.kill_session("invalid")
            mock_manager.kill_window.assert_not_called()

    def test_list_sessions(self):
        """Test list_sessions returns issue session names."""
        with patch("issue_orchestrator.adapters.terminal._tmux.get_manager") as mock_get_manager:
            mock_manager = MagicMock()
            mock_manager.list_issue_windows.return_value = [42, 123]
            mock_get_manager.return_value = mock_manager

            sessions = tmux.list_sessions()
            assert sessions == ["issue-42", "issue-123"]

    def test_list_sessions_empty(self):
        """Test list_sessions returns empty list."""
        with patch("issue_orchestrator.adapters.terminal._tmux.get_manager") as mock_get_manager:
            mock_manager = MagicMock()
            mock_manager.list_issue_windows.return_value = []
            mock_get_manager.return_value = mock_manager

            sessions = tmux.list_sessions()
            assert sessions == []

    def test_attach_session(self):
        """Test attach_session calls os.execvp."""
        with patch("issue_orchestrator.adapters.terminal._tmux.os.execvp") as mock_execvp:
            tmux.attach_session("issue-42")
            mock_execvp.assert_called_once_with(
                "tmux", ["tmux", "attach-session", "-t", tmux.SESSION_NAME]
            )

    def test_send_keys_with_enter(self, mock_session, mock_window):
        """Test send_keys sends keys with enter."""
        with patch("issue_orchestrator.adapters.terminal._tmux.get_manager") as mock_get_manager:
            mock_manager = MagicMock()
            mock_manager.send_keys_by_name.return_value = True
            mock_get_manager.return_value = mock_manager

            tmux.send_keys("issue-42", "echo test", enter=True)
            mock_manager.send_keys_by_name.assert_called_once_with("issue-42", "echo test", True)

    def test_send_keys_without_enter(self, mock_session, mock_window):
        """Test send_keys sends keys without enter."""
        with patch("issue_orchestrator.adapters.terminal._tmux.get_manager") as mock_get_manager:
            mock_manager = MagicMock()
            mock_manager.send_keys_by_name.return_value = True
            mock_get_manager.return_value = mock_manager

            tmux.send_keys("issue-42", "echo test", enter=False)
            mock_manager.send_keys_by_name.assert_called_once_with(
                "issue-42", "echo test", False
            )

    def test_send_keys_no_window(self):
        """Test send_keys does nothing when window doesn't exist."""
        with patch("issue_orchestrator.adapters.terminal._tmux.get_manager") as mock_get_manager:
            mock_manager = MagicMock()
            mock_manager.get_window.return_value = None
            mock_get_manager.return_value = mock_manager

            # Should not raise error
            tmux.send_keys("issue-42", "echo test")

    def test_send_keys_invalid_name(self):
        """Test send_keys does nothing for invalid session name."""
        with patch("issue_orchestrator.adapters.terminal._tmux.get_manager") as mock_get_manager:
            mock_manager = MagicMock()
            mock_get_manager.return_value = mock_manager

            # Should not raise error
            tmux.send_keys("invalid", "echo test")
            mock_manager.get_window.assert_not_called()


class TestTmuxPaneState:
    """Tests for TmuxPaneState class."""

    def test_init(self):
        """Test TmuxPaneState initialization."""
        mock_pane = MagicMock()
        state = tmux.TmuxPaneState(mock_pane)
        assert state._pane == mock_pane
        assert state._ttl_ms == tmux.PANE_STATE_TTL_MS
        assert state._last_refresh == 0

    def test_init_custom_ttl(self):
        """Test TmuxPaneState with custom TTL."""
        mock_pane = MagicMock()
        state = tmux.TmuxPaneState(mock_pane, ttl_ms=1000)
        assert state._ttl_ms == 1000

    def test_is_dead_true(self):
        """Test is_dead returns True when pane_dead is '1'."""
        mock_pane = MagicMock()
        mock_pane.pane_dead = "1"
        state = tmux.TmuxPaneState(mock_pane)
        assert state.is_dead is True

    def test_is_dead_false(self):
        """Test is_dead returns False when pane_dead is '0'."""
        mock_pane = MagicMock()
        mock_pane.pane_dead = "0"
        state = tmux.TmuxPaneState(mock_pane)
        assert state.is_dead is False

    def test_is_dead_missing_attribute(self):
        """Test is_dead handles missing pane_dead attribute."""
        mock_pane = MagicMock()
        # Remove pane_dead attribute to simulate missing
        del mock_pane.pane_dead
        state = tmux.TmuxPaneState(mock_pane)
        assert state.is_dead is False  # Default when attribute missing

    def test_exit_code_numeric(self):
        """Test exit_code returns integer from pane_dead_status."""
        mock_pane = MagicMock()
        mock_pane.pane_dead_status = "137"
        state = tmux.TmuxPaneState(mock_pane)
        assert state.exit_code == 137

    def test_exit_code_zero(self):
        """Test exit_code returns 0 for successful exit."""
        mock_pane = MagicMock()
        mock_pane.pane_dead_status = "0"
        state = tmux.TmuxPaneState(mock_pane)
        assert state.exit_code == 0

    def test_exit_code_none_when_running(self):
        """Test exit_code returns None when process is running."""
        mock_pane = MagicMock()
        mock_pane.pane_dead_status = None
        state = tmux.TmuxPaneState(mock_pane)
        assert state.exit_code is None

    def test_exit_code_none_when_empty(self):
        """Test exit_code returns None when pane_dead_status is empty."""
        mock_pane = MagicMock()
        mock_pane.pane_dead_status = ""
        state = tmux.TmuxPaneState(mock_pane)
        assert state.exit_code is None

    def test_signal_present(self):
        """Test signal returns signal name."""
        mock_pane = MagicMock()
        mock_pane.pane_dead_signal = "SIGTERM"
        state = tmux.TmuxPaneState(mock_pane)
        assert state.signal == "SIGTERM"

    def test_signal_none_when_normal_exit(self):
        """Test signal returns None for normal exit."""
        mock_pane = MagicMock()
        mock_pane.pane_dead_signal = None
        state = tmux.TmuxPaneState(mock_pane)
        assert state.signal is None

    def test_signal_none_when_empty(self):
        """Test signal returns None when pane_dead_signal is empty."""
        mock_pane = MagicMock()
        mock_pane.pane_dead_signal = ""
        state = tmux.TmuxPaneState(mock_pane)
        assert state.signal is None

    def test_pane_property(self):
        """Test pane property returns underlying pane."""
        mock_pane = MagicMock()
        state = tmux.TmuxPaneState(mock_pane)
        assert state.pane == mock_pane

    def test_ensure_fresh_refreshes_on_first_access(self):
        """Test _ensure_fresh calls refresh on first access."""
        mock_pane = MagicMock()
        mock_pane.pane_dead = "0"
        state = tmux.TmuxPaneState(mock_pane)

        _ = state.is_dead
        mock_pane.refresh.assert_called_once()

    def test_ensure_fresh_uses_cache(self):
        """Test _ensure_fresh uses cached value within TTL."""
        mock_pane = MagicMock()
        mock_pane.pane_dead = "0"
        state = tmux.TmuxPaneState(mock_pane, ttl_ms=10000)  # Long TTL

        _ = state.is_dead
        _ = state.exit_code
        _ = state.signal
        # Should only refresh once due to TTL
        assert mock_pane.refresh.call_count == 1


class TestTerminalObserverMethods:
    """Tests for TerminalObserver methods in TmuxManager."""

    @pytest.fixture
    def mock_manager_with_window(self, mock_server, mock_session, mock_window):
        """Create a manager with a mock window."""
        manager = tmux.TmuxManager()
        manager._server = mock_server
        manager._session = mock_session
        mock_session.windows.filter.return_value = [mock_window]
        return manager, mock_window

    def test_get_process_state_running(self, mock_manager_with_window):
        """Test get_process_state returns RUNNING when process is alive."""
        manager, mock_window = mock_manager_with_window
        mock_pane = mock_window.active_pane
        mock_pane.pane_dead = "0"

        from issue_orchestrator.domain import ProcessState
        state = manager.get_process_state("issue-42")
        assert state == ProcessState.RUNNING

    def test_get_process_state_exited(self, mock_manager_with_window):
        """Test get_process_state returns EXITED when process exited normally."""
        manager, mock_window = mock_manager_with_window
        mock_pane = mock_window.active_pane
        mock_pane.pane_dead = "1"
        mock_pane.pane_dead_signal = None

        from issue_orchestrator.domain import ProcessState
        state = manager.get_process_state("issue-42")
        assert state == ProcessState.EXITED

    def test_get_process_state_signaled(self, mock_manager_with_window):
        """Test get_process_state returns SIGNALED when process killed by signal."""
        manager, mock_window = mock_manager_with_window
        mock_pane = mock_window.active_pane
        mock_pane.pane_dead = "1"
        mock_pane.pane_dead_signal = "SIGKILL"

        from issue_orchestrator.domain import ProcessState
        state = manager.get_process_state("issue-42")
        assert state == ProcessState.SIGNALED

    def test_get_process_state_unknown_no_window(self, mock_server, mock_session):
        """Test get_process_state returns UNKNOWN when window doesn't exist."""
        manager = tmux.TmuxManager()
        manager._server = mock_server
        manager._session = mock_session
        mock_session.windows.filter.return_value = []  # No matching window

        from issue_orchestrator.domain import ProcessState
        state = manager.get_process_state("nonexistent")
        assert state == ProcessState.UNKNOWN

    def test_get_exit_info_success(self, mock_manager_with_window):
        """Test get_exit_info returns exit info for terminated process."""
        manager, mock_window = mock_manager_with_window
        mock_pane = mock_window.active_pane
        mock_pane.pane_dead = "1"
        mock_pane.pane_dead_status = "1"
        mock_pane.pane_dead_signal = None

        exit_info = manager.get_exit_info("issue-42")
        assert exit_info is not None
        assert exit_info.exit_code == 1
        assert exit_info.signal is None
        assert exit_info.exit_time is not None

    def test_get_exit_info_with_signal(self, mock_manager_with_window):
        """Test get_exit_info captures signal information."""
        manager, mock_window = mock_manager_with_window
        mock_pane = mock_window.active_pane
        mock_pane.pane_dead = "1"
        mock_pane.pane_dead_status = "137"
        mock_pane.pane_dead_signal = "SIGKILL"

        exit_info = manager.get_exit_info("issue-42")
        assert exit_info is not None
        assert exit_info.exit_code == 137
        assert exit_info.signal == "SIGKILL"

    def test_get_exit_info_none_when_running(self, mock_manager_with_window):
        """Test get_exit_info returns None when process is still running."""
        manager, mock_window = mock_manager_with_window
        mock_pane = mock_window.active_pane
        mock_pane.pane_dead = "0"

        exit_info = manager.get_exit_info("issue-42")
        assert exit_info is None

    def test_get_exit_info_none_when_no_window(self, mock_server, mock_session):
        """Test get_exit_info returns None when window doesn't exist."""
        manager = tmux.TmuxManager()
        manager._server = mock_server
        manager._session = mock_session
        mock_session.windows.filter.return_value = []

        exit_info = manager.get_exit_info("nonexistent")
        assert exit_info is None

    def test_is_process_alive_true(self, mock_manager_with_window):
        """Test is_process_alive returns True when process is running."""
        manager, mock_window = mock_manager_with_window
        mock_pane = mock_window.active_pane
        mock_pane.pane_dead = "0"

        assert manager.is_process_alive("issue-42") is True

    def test_is_process_alive_false_exited(self, mock_manager_with_window):
        """Test is_process_alive returns False when process exited."""
        manager, mock_window = mock_manager_with_window
        mock_pane = mock_window.active_pane
        mock_pane.pane_dead = "1"
        mock_pane.pane_dead_signal = None

        assert manager.is_process_alive("issue-42") is False

    def test_is_process_alive_false_no_window(self, mock_server, mock_session):
        """Test is_process_alive returns False when window doesn't exist."""
        manager = tmux.TmuxManager()
        manager._server = mock_server
        manager._session = mock_session
        mock_session.windows.filter.return_value = []

        assert manager.is_process_alive("nonexistent") is False

    def test_capture_full_output_success(self, mock_manager_with_window):
        """Test capture_full_output returns full scrollback."""
        manager, mock_window = mock_manager_with_window
        mock_pane = mock_window.active_pane
        mock_pane.capture_pane.return_value = ["line1", "line2", "line3"]

        output = manager.capture_full_output("issue-42")
        assert output == "line1\nline2\nline3"
        mock_pane.capture_pane.assert_called_once_with(start="-")

    def test_capture_full_output_empty(self, mock_manager_with_window):
        """Test capture_full_output handles empty output."""
        manager, mock_window = mock_manager_with_window
        mock_pane = mock_window.active_pane
        mock_pane.capture_pane.return_value = []

        output = manager.capture_full_output("issue-42")
        assert output == ""

    def test_capture_full_output_none_no_window(self, mock_server, mock_session):
        """Test capture_full_output returns None when window doesn't exist."""
        manager = tmux.TmuxManager()
        manager._server = mock_server
        manager._session = mock_session
        mock_session.windows.filter.return_value = []

        output = manager.capture_full_output("nonexistent")
        assert output is None

    def test_capture_full_output_handles_exception(self, mock_manager_with_window):
        """Test capture_full_output returns None on exception."""
        manager, mock_window = mock_manager_with_window
        mock_pane = mock_window.active_pane
        mock_pane.capture_pane.side_effect = Exception("tmux error")

        output = manager.capture_full_output("issue-42")
        assert output is None


class TestConstants:
    """Tests for module constants."""

    def test_session_name_default(self):
        """Test SESSION_NAME defaults to 'orchestrator' when env var not set."""
        # Note: SESSION_NAME is evaluated at import time, so this tests the default
        # when ORCHESTRATOR_TMUX_SESSION is not set in the test environment
        import os
        if "ORCHESTRATOR_TMUX_SESSION" not in os.environ:
            assert tmux.SESSION_NAME == "orchestrator"

    def test_session_name_env_override(self):
        """Test that SESSION_NAME can be overridden via ORCHESTRATOR_TMUX_SESSION.

        The env var is read at module import time. For e2e test isolation,
        we set ORCHESTRATOR_TMUX_SESSION before starting the orchestrator subprocess.
        This test verifies the code pattern is correct.
        """
        import os
        # Verify the code reads from the env var (the actual override happens at import)
        expected = os.environ.get("ORCHESTRATOR_TMUX_SESSION", "orchestrator")
        assert tmux.SESSION_NAME == expected

    def test_dashboard_window(self):
        """Test DASHBOARD_WINDOW constant."""
        assert tmux.DASHBOARD_WINDOW == "dashboard"
