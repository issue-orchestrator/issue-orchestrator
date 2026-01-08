"""Comprehensive unit tests for terminal adapters.

Tests for:
- TmuxPlugin (execution/terminal_tmux.py)
- PluggySessionRunner (execution/session_runner_adapter.py)

These tests mock external dependencies (libtmux)
and verify the adapter logic without requiring actual terminal processes.
"""

import logging
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch, call

import pytest


class TestTmuxPlugin:
    """Tests for TmuxPlugin which wraps TmuxManager.

    The plugin delegates to TmuxManager, so we mock the manager
    to test the plugin's hookimpl logic.
    """

    @pytest.fixture
    def mock_tmux_manager(self):
        """Mock TmuxManager for testing plugin delegation."""
        with patch("issue_orchestrator.execution.terminal_tmux.TmuxManager") as MockManager:
            mock_manager = MagicMock()
            MockManager.return_value = mock_manager
            yield mock_manager

    @pytest.fixture
    def tmux_plugin(self, mock_tmux_manager):
        """Create TmuxPlugin with mocked manager."""
        from issue_orchestrator.execution.terminal_tmux import TmuxPlugin
        return TmuxPlugin()

    def test_create_session_success(self, tmux_plugin, mock_tmux_manager):
        """create_session delegates to manager and returns True on success."""
        mock_tmux_manager.create_issue_window.return_value = MagicMock()

        result = tmux_plugin.create_session(
            session_id=42,
            command="claude --prompt issue.md",
            working_dir="/tmp/worktree",
            title="Fix the bug",
        )

        assert result is True
        mock_tmux_manager.create_issue_window.assert_called_once_with(
            issue_number=42,
            command="claude --prompt issue.md",
            working_dir=Path("/tmp/worktree"),
            title="Fix the bug",
        )

    def test_create_session_window_already_exists(self, tmux_plugin, mock_tmux_manager):
        """create_session returns False when window already exists (ValueError)."""
        mock_tmux_manager.create_issue_window.side_effect = ValueError("Window already exists")

        result = tmux_plugin.create_session(
            session_id=42,
            command="claude",
            working_dir="/tmp/worktree",
            title=None,
        )

        assert result is False

    def test_create_session_other_exception(self, tmux_plugin, mock_tmux_manager):
        """create_session returns False on any other exception."""
        mock_tmux_manager.create_issue_window.side_effect = RuntimeError("Tmux server down")

        result = tmux_plugin.create_session(
            session_id=42,
            command="claude",
            working_dir="/tmp/worktree",
            title=None,
        )

        assert result is False

    def test_session_exists_true(self, tmux_plugin, mock_tmux_manager):
        """session_exists returns True when window exists."""
        mock_tmux_manager.window_exists.return_value = True

        result = tmux_plugin.session_exists(session_id=42)

        assert result is True
        mock_tmux_manager.window_exists.assert_called_once_with(42)

    def test_session_exists_false(self, tmux_plugin, mock_tmux_manager):
        """session_exists returns False when window doesn't exist."""
        mock_tmux_manager.window_exists.return_value = False

        result = tmux_plugin.session_exists(session_id=99)

        assert result is False
        mock_tmux_manager.window_exists.assert_called_once_with(99)

    def test_kill_session(self, tmux_plugin, mock_tmux_manager):
        """kill_session delegates to manager and returns True."""
        result = tmux_plugin.kill_session(session_id=42)

        assert result is True
        mock_tmux_manager.kill_window.assert_called_once_with(42)

    def test_discover_running_sessions(self, tmux_plugin, mock_tmux_manager):
        """discover_running_sessions returns list of sessions from manager."""
        mock_tmux_manager.list_issue_windows.return_value = [42, 123, 456]

        result = tmux_plugin.discover_running_sessions()

        assert result == [
            {"issue_number": 42, "tab_name": "issue-42", "is_review": False},
            {"issue_number": 123, "tab_name": "issue-123", "is_review": False},
            {"issue_number": 456, "tab_name": "issue-456", "is_review": False},
        ]

    def test_discover_running_sessions_empty(self, tmux_plugin, mock_tmux_manager):
        """discover_running_sessions returns empty list when no windows."""
        mock_tmux_manager.list_issue_windows.return_value = []

        result = tmux_plugin.discover_running_sessions()

        assert result == []

    def test_cleanup_idle_sessions(self, tmux_plugin, mock_tmux_manager):
        """cleanup_idle_sessions returns 0 (not implemented for tmux)."""
        result = tmux_plugin.cleanup_idle_sessions()
        assert result == 0

    def test_get_session_output(self, tmux_plugin, mock_tmux_manager):
        """get_session_output delegates to manager."""
        mock_tmux_manager.capture_pane_output.return_value = "foo\nbar\nbaz"

        result = tmux_plugin.get_session_output(session_id=42, lines=50)

        assert result == "foo\nbar\nbaz"
        mock_tmux_manager.capture_pane_output.assert_called_once_with(42, 50)

    def test_get_session_output_none(self, tmux_plugin, mock_tmux_manager):
        """get_session_output returns None when window doesn't exist."""
        mock_tmux_manager.capture_pane_output.return_value = None

        result = tmux_plugin.get_session_output(session_id=99, lines=20)

        assert result is None

    def test_send_to_session_success(self, tmux_plugin, mock_tmux_manager):
        """send_to_session returns True on success."""
        result = tmux_plugin.send_to_session(session_id=42, text="/exit")

        assert result is True
        mock_tmux_manager.send_keys.assert_called_once_with(42, "/exit")

    def test_send_to_session_exception(self, tmux_plugin, mock_tmux_manager):
        """send_to_session returns False on exception."""
        mock_tmux_manager.send_keys.side_effect = RuntimeError("Window gone")

        result = tmux_plugin.send_to_session(session_id=42, text="/exit")

        assert result is False

    def test_session_exists_by_name(self, tmux_plugin, mock_tmux_manager):
        """session_exists_by_name delegates to manager."""
        mock_tmux_manager.window_exists_by_name.return_value = True

        result = tmux_plugin.session_exists_by_name(session_name="review-456")

        assert result is True
        mock_tmux_manager.window_exists_by_name.assert_called_once_with("review-456")

    def test_send_to_session_by_name(self, tmux_plugin, mock_tmux_manager):
        """send_to_session_by_name delegates to manager."""
        mock_tmux_manager.send_keys_by_name.return_value = True

        result = tmux_plugin.send_to_session_by_name(session_name="review-456", text="/exit")

        assert result is True
        mock_tmux_manager.send_keys_by_name.assert_called_once_with("review-456", "/exit")


class TestPluggySessionRunner:
    """Tests for PluggySessionRunner which wraps pluggy PluginManager.

    This adapter bridges the SessionRunner protocol and pluggy hooks.
    We mock the PluginManager to test the delegation logic.
    """

    @pytest.fixture
    def mock_plugin_manager(self):
        """Create a mock pluggy PluginManager."""
        mock_pm = MagicMock()
        mock_pm.hook = MagicMock()
        return mock_pm

    @pytest.fixture
    def session_runner(self, mock_plugin_manager):
        """Create PluggySessionRunner with mocked plugin manager."""
        from issue_orchestrator.execution.session_runner_adapter import PluggySessionRunner
        return PluggySessionRunner(mock_plugin_manager)

    def test_create_session_success(self, session_runner, mock_plugin_manager, caplog):
        """create_session delegates to pluggy hook and returns result."""
        mock_plugin_manager.hook.create_session.return_value = True

        with caplog.at_level(logging.INFO):
            result = session_runner.create_session(
                session_id=42,
                command="claude --prompt issue.md",
                working_dir="/tmp/worktree",
                title="Fix the bug",
            )

        assert result is True
        mock_plugin_manager.hook.create_session.assert_called_once_with(
            session_id=42,
            command="claude --prompt issue.md",
            working_dir="/tmp/worktree",
            title="Fix the bug",
        )
        assert "Creating session via terminal hook" in caplog.text
        assert "id=42" in caplog.text

    def test_create_session_returns_none(self, session_runner, mock_plugin_manager):
        """create_session returns False when hook returns None."""
        mock_plugin_manager.hook.create_session.return_value = None

        result = session_runner.create_session(
            session_id=42,
            command="claude",
            working_dir="/tmp/worktree",
            title=None,
        )

        assert result is False

    def test_create_session_returns_false(self, session_runner, mock_plugin_manager):
        """create_session returns False when hook returns False."""
        mock_plugin_manager.hook.create_session.return_value = False

        result = session_runner.create_session(
            session_id=42,
            command="claude",
            working_dir="/tmp/worktree",
            title=None,
        )

        assert result is False

    def test_session_exists_true(self, session_runner, mock_plugin_manager):
        """session_exists returns True when hook returns True."""
        mock_plugin_manager.hook.session_exists.return_value = True

        result = session_runner.session_exists(session_id=42)

        assert result is True
        mock_plugin_manager.hook.session_exists.assert_called_once_with(session_id=42)

    def test_session_exists_false(self, session_runner, mock_plugin_manager):
        """session_exists returns False when hook returns False."""
        mock_plugin_manager.hook.session_exists.return_value = False

        result = session_runner.session_exists(session_id=42)

        assert result is False

    def test_session_exists_none(self, session_runner, mock_plugin_manager):
        """session_exists returns False when hook returns None."""
        mock_plugin_manager.hook.session_exists.return_value = None

        result = session_runner.session_exists(session_id=42)

        assert result is False

    def test_kill_session(self, session_runner, mock_plugin_manager):
        """kill_session delegates to pluggy hook."""
        session_runner.kill_session(session_id=42)

        mock_plugin_manager.hook.kill_session.assert_called_once_with(session_id=42)

    def test_discover_running_sessions_with_results(self, session_runner, mock_plugin_manager):
        """discover_running_sessions returns list from hook."""
        mock_sessions = [
            {"issue_number": 42, "tab_name": "issue-42", "is_review": False},
            {"issue_number": 123, "tab_name": "review-123", "is_review": True},
        ]
        mock_plugin_manager.hook.discover_running_sessions.return_value = mock_sessions

        result = session_runner.discover_running_sessions()

        assert result == mock_sessions

    def test_discover_running_sessions_empty(self, session_runner, mock_plugin_manager):
        """discover_running_sessions returns empty list when hook returns None."""
        mock_plugin_manager.hook.discover_running_sessions.return_value = None

        result = session_runner.discover_running_sessions()

        assert result == []

    def test_cleanup_idle_sessions_returns_count(self, session_runner, mock_plugin_manager):
        """cleanup_idle_sessions returns count from hook."""
        mock_plugin_manager.hook.cleanup_idle_sessions.return_value = 3

        result = session_runner.cleanup_idle_sessions()

        assert result == 3

    def test_cleanup_idle_sessions_returns_none(self, session_runner, mock_plugin_manager):
        """cleanup_idle_sessions returns 0 when hook returns None."""
        mock_plugin_manager.hook.cleanup_idle_sessions.return_value = None

        result = session_runner.cleanup_idle_sessions()

        assert result == 0

    def test_get_session_output_with_result(self, session_runner, mock_plugin_manager):
        """get_session_output returns output from hook."""
        mock_plugin_manager.hook.get_session_output.return_value = "foo\nbar\nbaz"

        result = session_runner.get_session_output(session_id=42, lines=50)

        assert result == "foo\nbar\nbaz"
        mock_plugin_manager.hook.get_session_output.assert_called_once_with(
            session_id=42, lines=50
        )

    def test_get_session_output_none(self, session_runner, mock_plugin_manager):
        """get_session_output returns None when hook returns None."""
        mock_plugin_manager.hook.get_session_output.return_value = None

        result = session_runner.get_session_output(session_id=42, lines=20)

        assert result is None

    def test_get_session_output_default_lines(self, session_runner, mock_plugin_manager):
        """get_session_output uses default lines=50."""
        mock_plugin_manager.hook.get_session_output.return_value = "output"

        session_runner.get_session_output(session_id=42)

        mock_plugin_manager.hook.get_session_output.assert_called_once_with(
            session_id=42, lines=50
        )

    def test_send_to_session_success(self, session_runner, mock_plugin_manager):
        """send_to_session returns True when hook returns True."""
        mock_plugin_manager.hook.send_to_session.return_value = True

        result = session_runner.send_to_session(session_id=42, text="/exit")

        assert result is True
        mock_plugin_manager.hook.send_to_session.assert_called_once_with(
            session_id=42, text="/exit"
        )

    def test_send_to_session_failure(self, session_runner, mock_plugin_manager):
        """send_to_session returns False when hook returns False."""
        mock_plugin_manager.hook.send_to_session.return_value = False

        result = session_runner.send_to_session(session_id=42, text="/exit")

        assert result is False

    def test_send_to_session_none(self, session_runner, mock_plugin_manager):
        """send_to_session returns False when hook returns None."""
        mock_plugin_manager.hook.send_to_session.return_value = None

        result = session_runner.send_to_session(session_id=42, text="/exit")

        assert result is False

    def test_session_exists_by_name_true(self, session_runner, mock_plugin_manager):
        """session_exists_by_name returns True when hook returns True."""
        mock_plugin_manager.hook.session_exists_by_name.return_value = True

        result = session_runner.session_exists_by_name(session_name="review-456")

        assert result is True
        mock_plugin_manager.hook.session_exists_by_name.assert_called_once_with(
            session_name="review-456"
        )

    def test_session_exists_by_name_false(self, session_runner, mock_plugin_manager):
        """session_exists_by_name returns False when hook returns False."""
        mock_plugin_manager.hook.session_exists_by_name.return_value = False

        result = session_runner.session_exists_by_name(session_name="review-456")

        assert result is False

    def test_session_exists_by_name_none(self, session_runner, mock_plugin_manager):
        """session_exists_by_name returns False when hook returns None."""
        mock_plugin_manager.hook.session_exists_by_name.return_value = None

        result = session_runner.session_exists_by_name(session_name="review-456")

        assert result is False

    def test_send_to_session_by_name_success(self, session_runner, mock_plugin_manager):
        """send_to_session_by_name returns True when hook returns True."""
        mock_plugin_manager.hook.send_to_session_by_name.return_value = True

        result = session_runner.send_to_session_by_name(
            session_name="review-456", text="/exit"
        )

        assert result is True
        mock_plugin_manager.hook.send_to_session_by_name.assert_called_once_with(
            session_name="review-456", text="/exit"
        )

    def test_send_to_session_by_name_failure(self, session_runner, mock_plugin_manager):
        """send_to_session_by_name returns False when hook returns False."""
        mock_plugin_manager.hook.send_to_session_by_name.return_value = False

        result = session_runner.send_to_session_by_name(
            session_name="review-456", text="/exit"
        )

        assert result is False

    def test_send_to_session_by_name_none(self, session_runner, mock_plugin_manager):
        """send_to_session_by_name returns False when hook returns None."""
        mock_plugin_manager.hook.send_to_session_by_name.return_value = None

        result = session_runner.send_to_session_by_name(
            session_name="review-456", text="/exit"
        )

        assert result is False


class TestTmuxManagerIntegration:
    """Integration tests for TmuxManager with mocked libtmux.

    These tests verify TmuxManager's logic while mocking the libtmux library
    to avoid needing a real tmux server.
    """

    @pytest.fixture
    def mock_libtmux_server(self):
        """Mock libtmux.Server and related objects."""
        with patch("issue_orchestrator.adapters.terminal._tmux.libtmux") as mock_libtmux:
            mock_server = MagicMock()
            mock_session = MagicMock()
            mock_window = MagicMock()
            mock_pane = MagicMock()

            # Setup relationships
            mock_libtmux.Server.return_value = mock_server
            mock_server.sessions.get.return_value = mock_session
            mock_server.new_session.return_value = mock_session
            mock_session.new_window.return_value = mock_window
            # Make windows a MagicMock with filter method, not a list
            mock_windows = MagicMock()
            mock_windows.filter.return_value = []
            mock_session.windows = mock_windows
            mock_window.active_pane = mock_pane
            mock_window.name = None

            yield {
                "server": mock_server,
                "session": mock_session,
                "window": mock_window,
                "pane": mock_pane,
                "windows": mock_windows,
            }

    @pytest.fixture
    def tmux_manager(self, mock_libtmux_server):
        """Create TmuxManager with mocked libtmux."""
        from issue_orchestrator.adapters.terminal._tmux import TmuxManager
        return TmuxManager()

    def test_ensure_session_creates_new(self, tmux_manager, mock_libtmux_server):
        """ensure_session creates new session if it doesn't exist."""
        mock_server = mock_libtmux_server["server"]
        mock_session = mock_libtmux_server["session"]
        mock_server.sessions.get.side_effect = Exception("Session not found")

        result = tmux_manager.ensure_session()

        assert result == mock_session
        mock_server.new_session.assert_called_once_with(
            session_name="orchestrator",
            window_name="dashboard",
        )

    def test_create_issue_window_success(self, tmux_manager, mock_libtmux_server):
        """create_issue_window creates window and sends command."""
        mock_session = mock_libtmux_server["session"]
        mock_window = mock_libtmux_server["window"]
        mock_pane = mock_libtmux_server["pane"]

        # Ensure session exists first (this will trigger new_session since get raises)
        tmux_manager._session = mock_session  # Set directly to avoid second call

        result = tmux_manager.create_issue_window(
            issue_number=42,
            command="claude --prompt issue.md",
            working_dir=Path("/tmp/worktree"),
            title="Fix the bug",
        )

        assert result == mock_window
        # Verify window name includes issue number and truncated title
        mock_session.new_window.assert_called_once()
        call_kwargs = mock_session.new_window.call_args[1]
        assert call_kwargs["window_name"] == "#42-Fix-the-bug"
        assert call_kwargs["start_directory"] == "/tmp/worktree"

        # Verify PATH setup and command were sent
        assert mock_pane.send_keys.call_count == 2
        path_cmd = mock_pane.send_keys.call_args_list[0][0][0]
        assert "export PATH=" in path_cmd
        assert mock_pane.send_keys.call_args_list[1][0][0] == "claude --prompt issue.md"

    def test_create_issue_window_duplicate_raises(self, tmux_manager, mock_libtmux_server):
        """create_issue_window raises ValueError if window already exists."""
        mock_session = mock_libtmux_server["session"]
        mock_windows = mock_libtmux_server["windows"]
        mock_existing_window = MagicMock()
        mock_windows.filter.return_value = [mock_existing_window]

        tmux_manager._session = mock_session  # Set directly

        with pytest.raises(ValueError, match="already exists"):
            tmux_manager.create_issue_window(
                issue_number=42,
                command="claude",
                working_dir=Path("/tmp/worktree"),
                title=None,
            )

    def test_window_exists_by_name(self, tmux_manager, mock_libtmux_server):
        """window_exists_by_name checks if window exists by name."""
        mock_session = mock_libtmux_server["session"]
        mock_windows = mock_libtmux_server["windows"]
        mock_window = MagicMock()
        mock_windows.filter.return_value = [mock_window]

        tmux_manager._session = mock_session  # Set directly

        result = tmux_manager.window_exists_by_name("review-456")

        assert result is True
        mock_windows.filter.assert_called_with(window_name="review-456")

    def test_kill_window(self, tmux_manager, mock_libtmux_server):
        """kill_window stops pipe-pane and kills the window."""
        mock_session = mock_libtmux_server["session"]
        mock_window = MagicMock()
        mock_pane = MagicMock()
        mock_window.active_pane = mock_pane
        mock_window.name = "#42-test"

        # Mock _find_issue_window to return our mock window
        with patch.object(tmux_manager, "_find_issue_window", return_value=mock_window):
            tmux_manager.kill_window(42)

        # Verify pipe-pane was stopped (prevents lingering subprocesses)
        mock_pane.cmd.assert_called_once_with("pipe-pane")
        mock_window.kill.assert_called_once()

    def test_send_keys_by_name(self, tmux_manager, mock_libtmux_server):
        """send_keys_by_name sends keys to window by name."""
        mock_session = mock_libtmux_server["session"]
        mock_windows = mock_libtmux_server["windows"]
        mock_window = MagicMock()
        mock_pane = MagicMock()
        mock_window.active_pane = mock_pane
        mock_windows.filter.return_value = [mock_window]

        tmux_manager._session = mock_session  # Set directly

        result = tmux_manager.send_keys_by_name("review-456", "/exit", enter=True)

        assert result is True
        mock_pane.send_keys.assert_called_once_with("/exit")

    def test_send_keys_by_name_no_enter(self, tmux_manager, mock_libtmux_server):
        """send_keys_by_name can send keys without pressing enter."""
        mock_session = mock_libtmux_server["session"]
        mock_windows = mock_libtmux_server["windows"]
        mock_window = MagicMock()
        mock_pane = MagicMock()
        mock_window.active_pane = mock_pane
        mock_windows.filter.return_value = [mock_window]

        tmux_manager._session = mock_session  # Set directly

        result = tmux_manager.send_keys_by_name("review-456", "text", enter=False)

        assert result is True
        mock_pane.send_keys.assert_called_once_with("text", enter=False)

    def test_list_issue_windows_new_format(self, tmux_manager, mock_libtmux_server):
        """list_issue_windows extracts issue numbers from new format (#N-title)."""
        mock_session = mock_libtmux_server["session"]
        mock_window1 = MagicMock()
        mock_window1.name = "#42-fix-bug"
        mock_window2 = MagicMock()
        mock_window2.name = "#123-add-feature"
        # Make windows iterable
        mock_session.windows = [mock_window1, mock_window2]

        tmux_manager._session = mock_session  # Set directly

        result = tmux_manager.list_issue_windows()

        assert result == [42, 123]

    def test_list_issue_windows_old_format(self, tmux_manager, mock_libtmux_server):
        """list_issue_windows extracts issue numbers from old format (issue-N)."""
        mock_session = mock_libtmux_server["session"]
        mock_window1 = MagicMock()
        mock_window1.name = "issue-42"
        mock_window2 = MagicMock()
        mock_window2.name = "issue-123"
        # Make windows iterable
        mock_session.windows = [mock_window1, mock_window2]

        tmux_manager._session = mock_session  # Set directly

        result = tmux_manager.list_issue_windows()

        assert result == [42, 123]

    def test_list_issue_windows_mixed_formats(self, tmux_manager, mock_libtmux_server):
        """list_issue_windows handles mixed formats and skips non-issue windows."""
        mock_session = mock_libtmux_server["session"]
        windows = []
        for name in ["#42-fix", "issue-99", "dashboard", "random"]:
            w = MagicMock()
            w.name = name
            windows.append(w)
        # Make windows iterable
        mock_session.windows = windows

        tmux_manager._session = mock_session  # Set directly

        result = tmux_manager.list_issue_windows()

        assert sorted(result) == [42, 99]

    def test_capture_pane_output(self, tmux_manager, mock_libtmux_server):
        """capture_pane_output retrieves output from pane."""
        mock_window = MagicMock()
        mock_pane = MagicMock()
        mock_window.active_pane = mock_pane
        mock_pane.capture_pane.return_value = ["line1", "line2", "line3"]

        with patch.object(tmux_manager, "get_window", return_value=mock_window):
            result = tmux_manager.capture_pane_output(42, lines=20)

        assert result == "line1\nline2\nline3"
        mock_pane.capture_pane.assert_called_once_with(start=-20)
