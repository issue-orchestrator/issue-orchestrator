"""Unit tests for iTerm2 integration module."""

import pytest
from unittest.mock import MagicMock, patch, call
import subprocess
import os
from pathlib import Path

from issue_orchestrator.iterm2 import (
    is_iterm2_available,
    is_running_in_iterm2,
    run_applescript,
    select_tab_by_name,
    select_tab_by_index,
    get_tab_count,
    split_pane_vertical,
    split_pane_horizontal,
    send_text_to_session,
    create_new_tab_with_command,
    attach_to_tmux_cc,
    ITermSessionManager,
    get_iterm_manager,
)


class TestIsIterm2Available:
    """Test the is_iterm2_available function."""

    @patch("issue_orchestrator.iterm2.os.uname")
    def test_not_darwin(self, mock_uname):
        """Test that it returns False on non-macOS systems."""
        mock_uname.return_value = MagicMock(sysname="Linux")
        assert is_iterm2_available() is False

    @patch("issue_orchestrator.iterm2.subprocess.run")
    @patch("issue_orchestrator.iterm2.os.uname")
    def test_darwin_iterm_running(self, mock_uname, mock_run):
        """Test that it returns True when iTerm2 is running on macOS."""
        mock_uname.return_value = MagicMock(sysname="Darwin")
        mock_run.return_value = MagicMock(stdout="true", returncode=0)

        assert is_iterm2_available() is True
        mock_run.assert_called_once_with(
            ["osascript", "-e", 'tell application "System Events" to (name of processes) contains "iTerm"'],
            capture_output=True,
            text=True
        )

    @patch("issue_orchestrator.iterm2.subprocess.run")
    @patch("issue_orchestrator.iterm2.os.uname")
    def test_darwin_iterm_not_running(self, mock_uname, mock_run):
        """Test that it returns False when iTerm2 is not running on macOS."""
        mock_uname.return_value = MagicMock(sysname="Darwin")
        mock_run.return_value = MagicMock(stdout="false", returncode=0)

        assert is_iterm2_available() is False

    @patch("issue_orchestrator.iterm2.subprocess.run")
    @patch("issue_orchestrator.iterm2.os.uname")
    def test_darwin_iterm_case_insensitive(self, mock_uname, mock_run):
        """Test that 'true' check is case-insensitive."""
        mock_uname.return_value = MagicMock(sysname="Darwin")
        mock_run.return_value = MagicMock(stdout="TRUE", returncode=0)

        assert is_iterm2_available() is True


class TestIsRunningInIterm2:
    """Test the is_running_in_iterm2 function."""

    @patch.dict(os.environ, {"TERM_PROGRAM": "iTerm.app"})
    def test_running_in_iterm(self):
        """Test that it returns True when running in iTerm2."""
        assert is_running_in_iterm2() is True

    @patch.dict(os.environ, {"TERM_PROGRAM": "Terminal.app"})
    def test_running_in_terminal(self):
        """Test that it returns False when running in Terminal."""
        assert is_running_in_iterm2() is False

    @patch.dict(os.environ, {}, clear=True)
    def test_no_term_program_env(self):
        """Test that it returns False when TERM_PROGRAM is not set."""
        assert is_running_in_iterm2() is False


class TestRunApplescript:
    """Test the run_applescript function."""

    @patch("issue_orchestrator.iterm2.subprocess.run")
    def test_successful_script(self, mock_run):
        """Test successful AppleScript execution."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="result output\n",
            stderr=""
        )

        success, output = run_applescript('tell application "iTerm" to activate')

        assert success is True
        assert output == "result output"
        mock_run.assert_called_once_with(
            ["osascript", "-e", 'tell application "iTerm" to activate'],
            capture_output=True,
            text=True
        )

    @patch("issue_orchestrator.iterm2.subprocess.run")
    def test_failed_script(self, mock_run):
        """Test failed AppleScript execution."""
        mock_run.return_value = MagicMock(
            returncode=1,
            stdout="",
            stderr="execution error: Error message (-1234)"
        )

        success, output = run_applescript('bad script')

        assert success is False
        assert "execution error" in output

    @patch("issue_orchestrator.iterm2.subprocess.run")
    def test_script_with_output_trimming(self, mock_run):
        """Test that output is properly trimmed."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="  result with spaces  \n",
            stderr=""
        )

        success, output = run_applescript('some script')

        assert success is True
        assert output == "result with spaces"


class TestSelectTabByName:
    """Test the select_tab_by_name function."""

    @patch("issue_orchestrator.iterm2.run_applescript")
    def test_successful_tab_selection(self, mock_run_as):
        """Test successful tab selection by name."""
        mock_run_as.return_value = (True, "true")

        result = select_tab_by_name("my-tab")

        assert result is True
        assert mock_run_as.called
        script = mock_run_as.call_args[0][0]
        assert "my-tab" in script
        assert 'tell application "iTerm"' in script

    @patch("issue_orchestrator.iterm2.run_applescript")
    def test_tab_not_found(self, mock_run_as):
        """Test when tab is not found."""
        mock_run_as.return_value = (True, "false")

        result = select_tab_by_name("nonexistent")

        assert result is False

    @patch("issue_orchestrator.iterm2.run_applescript")
    def test_applescript_failure(self, mock_run_as):
        """Test when AppleScript fails."""
        mock_run_as.return_value = (False, "error message")

        result = select_tab_by_name("my-tab")

        assert result is False

    @patch("issue_orchestrator.iterm2.run_applescript")
    def test_case_insensitive_true_check(self, mock_run_as):
        """Test that 'true' check is case-insensitive."""
        mock_run_as.return_value = (True, "TRUE")

        result = select_tab_by_name("my-tab")

        assert result is True


class TestSelectTabByIndex:
    """Test the select_tab_by_index function."""

    @patch("issue_orchestrator.iterm2.run_applescript")
    def test_successful_tab_selection(self, mock_run_as):
        """Test successful tab selection by index."""
        mock_run_as.return_value = (True, "true")

        result = select_tab_by_index(2)

        assert result is True
        script = mock_run_as.call_args[0][0]
        assert "2" in script

    @patch("issue_orchestrator.iterm2.run_applescript")
    def test_index_out_of_bounds(self, mock_run_as):
        """Test when index exceeds tab count."""
        mock_run_as.return_value = (True, "false")

        result = select_tab_by_index(99)

        assert result is False

    @patch("issue_orchestrator.iterm2.run_applescript")
    def test_applescript_failure(self, mock_run_as):
        """Test when AppleScript fails."""
        mock_run_as.return_value = (False, "error")

        result = select_tab_by_index(1)

        assert result is False


class TestGetTabCount:
    """Test the get_tab_count function."""

    @patch("issue_orchestrator.iterm2.run_applescript")
    def test_successful_count(self, mock_run_as):
        """Test successful tab count retrieval."""
        mock_run_as.return_value = (True, "5")

        result = get_tab_count()

        assert result == 5

    @patch("issue_orchestrator.iterm2.run_applescript")
    def test_applescript_failure(self, mock_run_as):
        """Test when AppleScript fails."""
        mock_run_as.return_value = (False, "error")

        result = get_tab_count()

        assert result == 0

    @patch("issue_orchestrator.iterm2.run_applescript")
    def test_invalid_number_format(self, mock_run_as):
        """Test when output is not a valid number."""
        mock_run_as.return_value = (True, "not-a-number")

        result = get_tab_count()

        assert result == 0

    @patch("issue_orchestrator.iterm2.run_applescript")
    def test_zero_tabs(self, mock_run_as):
        """Test when there are no tabs."""
        mock_run_as.return_value = (True, "0")

        result = get_tab_count()

        assert result == 0


class TestSplitPaneVertical:
    """Test the split_pane_vertical function."""

    @patch("issue_orchestrator.iterm2.run_applescript")
    def test_successful_split(self, mock_run_as):
        """Test successful vertical pane split."""
        mock_run_as.return_value = (True, "")

        result = split_pane_vertical()

        assert result is True
        script = mock_run_as.call_args[0][0]
        assert "split vertically" in script

    @patch("issue_orchestrator.iterm2.run_applescript")
    def test_failed_split(self, mock_run_as):
        """Test failed vertical pane split."""
        mock_run_as.return_value = (False, "error")

        result = split_pane_vertical()

        assert result is False


class TestSplitPaneHorizontal:
    """Test the split_pane_horizontal function."""

    @patch("issue_orchestrator.iterm2.run_applescript")
    def test_successful_split(self, mock_run_as):
        """Test successful horizontal pane split."""
        mock_run_as.return_value = (True, "")

        result = split_pane_horizontal()

        assert result is True
        script = mock_run_as.call_args[0][0]
        assert "split horizontally" in script

    @patch("issue_orchestrator.iterm2.run_applescript")
    def test_failed_split(self, mock_run_as):
        """Test failed horizontal pane split."""
        mock_run_as.return_value = (False, "error")

        result = split_pane_horizontal()

        assert result is False


class TestSendTextToSession:
    """Test the send_text_to_session function."""

    @patch("issue_orchestrator.iterm2.run_applescript")
    def test_send_text_with_newline(self, mock_run_as):
        """Test sending text with newline."""
        mock_run_as.return_value = (True, "")

        result = send_text_to_session("echo hello")

        assert result is True
        script = mock_run_as.call_args[0][0]
        assert "echo hello" in script
        assert "newline true" in script

    @patch("issue_orchestrator.iterm2.run_applescript")
    def test_send_text_without_newline(self, mock_run_as):
        """Test sending text without newline."""
        mock_run_as.return_value = (True, "")

        result = send_text_to_session("echo hello", new_line=False)

        assert result is True
        script = mock_run_as.call_args[0][0]
        assert "newline false" in script

    @patch("issue_orchestrator.iterm2.run_applescript")
    def test_send_text_with_quotes(self, mock_run_as):
        """Test sending text that contains quotes."""
        mock_run_as.return_value = (True, "")

        result = send_text_to_session('echo "hello world"')

        assert result is True
        script = mock_run_as.call_args[0][0]
        # Quotes should be escaped
        assert '\\"' in script

    @patch("issue_orchestrator.iterm2.run_applescript")
    def test_send_text_failure(self, mock_run_as):
        """Test failed text sending."""
        mock_run_as.return_value = (False, "error")

        result = send_text_to_session("echo hello")

        assert result is False


class TestCreateNewTabWithCommand:
    """Test the create_new_tab_with_command function."""

    @patch("issue_orchestrator.iterm2.run_applescript")
    def test_create_tab_without_name(self, mock_run_as):
        """Test creating tab without a name."""
        mock_run_as.return_value = (True, "")

        result = create_new_tab_with_command("ls -la")

        assert result is True
        script = mock_run_as.call_args[0][0]
        assert "ls -la" in script
        assert "create tab" in script

    @patch("issue_orchestrator.iterm2.run_applescript")
    def test_create_tab_with_name(self, mock_run_as):
        """Test creating tab with a name."""
        mock_run_as.return_value = (True, "")

        result = create_new_tab_with_command("ls -la", name="My Tab")

        assert result is True
        script = mock_run_as.call_args[0][0]
        assert "My Tab" in script
        assert 'set name to "My Tab"' in script

    @patch("issue_orchestrator.iterm2.run_applescript")
    def test_create_tab_with_quotes_in_command(self, mock_run_as):
        """Test creating tab with command containing quotes."""
        mock_run_as.return_value = (True, "")

        result = create_new_tab_with_command('echo "test"')

        assert result is True
        script = mock_run_as.call_args[0][0]
        assert '\\"' in script

    @patch("issue_orchestrator.iterm2.run_applescript")
    def test_create_tab_failure(self, mock_run_as):
        """Test failed tab creation."""
        mock_run_as.return_value = (False, "error")

        result = create_new_tab_with_command("ls")

        assert result is False


class TestAttachToTmuxCc:
    """Test the attach_to_tmux_cc function."""

    @patch("issue_orchestrator.iterm2.send_text_to_session")
    def test_attach_default_session(self, mock_send):
        """Test attaching to default tmux session."""
        mock_send.return_value = True

        result = attach_to_tmux_cc()

        assert result is True
        mock_send.assert_called_once_with("tmux -CC attach -t orchestrator")

    @patch("issue_orchestrator.iterm2.send_text_to_session")
    def test_attach_custom_session(self, mock_send):
        """Test attaching to custom tmux session."""
        mock_send.return_value = True

        result = attach_to_tmux_cc("my-session")

        assert result is True
        mock_send.assert_called_once_with("tmux -CC attach -t my-session")

    @patch("issue_orchestrator.iterm2.send_text_to_session")
    def test_attach_failure(self, mock_send):
        """Test failed tmux attachment."""
        mock_send.return_value = False

        result = attach_to_tmux_cc()

        assert result is False


class TestITermSessionManager:
    """Test the ITermSessionManager class."""

    def test_init(self):
        """Test ITermSessionManager initialization."""
        manager = ITermSessionManager()
        assert manager._sessions == {}

    @patch("issue_orchestrator.iterm2.run_applescript")
    @patch("issue_orchestrator.iterm2.subprocess.run")
    def test_create_session_basic(self, mock_subprocess, mock_run_as):
        """Test creating a basic session."""
        mock_run_as.return_value = (True, "")
        mock_subprocess.return_value = MagicMock(stdout="1638360000\n")

        manager = ITermSessionManager()
        result = manager.create_session(
            issue_number=42,
            command="claude code",
            working_dir="/tmp/test",
        )

        assert result is True
        assert 42 in manager._sessions
        assert manager._sessions[42]["tab_name"] == "#42"

    @patch("issue_orchestrator.iterm2.run_applescript")
    @patch("issue_orchestrator.iterm2.subprocess.run")
    def test_create_session_with_title(self, mock_subprocess, mock_run_as):
        """Test creating a session with a title."""
        mock_run_as.return_value = (True, "")
        mock_subprocess.return_value = MagicMock(stdout="1638360000\n")

        manager = ITermSessionManager()
        result = manager.create_session(
            issue_number=42,
            command="claude code",
            working_dir="/tmp/test",
            title="Fix authentication bug",
        )

        assert result is True
        assert 42 in manager._sessions
        # Title should be truncated to 20 chars
        assert "#42 Fix authentication " in manager._sessions[42]["tab_name"]

    @patch("issue_orchestrator.iterm2.run_applescript")
    @patch("issue_orchestrator.iterm2.subprocess.run")
    def test_create_session_long_title(self, mock_subprocess, mock_run_as):
        """Test creating a session with a very long title."""
        mock_run_as.return_value = (True, "")
        mock_subprocess.return_value = MagicMock(stdout="1638360000\n")

        manager = ITermSessionManager()
        long_title = "This is a very long title that should be truncated"
        result = manager.create_session(
            issue_number=42,
            command="claude code",
            working_dir="/tmp/test",
            title=long_title,
        )

        assert result is True
        # Title should be truncated to 20 chars
        assert len(manager._sessions[42]["tab_name"]) <= 24  # "#42 " + 20 chars

    @patch("issue_orchestrator.iterm2.run_applescript")
    @patch("issue_orchestrator.iterm2.subprocess.run")
    def test_create_session_escaping(self, mock_subprocess, mock_run_as):
        """Test that special characters are properly escaped."""
        mock_run_as.return_value = (True, "")
        mock_subprocess.return_value = MagicMock(stdout="1638360000\n")

        manager = ITermSessionManager()
        result = manager.create_session(
            issue_number=42,
            command='echo "test"',
            working_dir="/tmp/test",
            title='Test "quotes"',
        )

        assert result is True
        script = mock_run_as.call_args[0][0]
        # Check that quotes are escaped
        assert '\\"' in script

    @patch("issue_orchestrator.iterm2.run_applescript")
    @patch("issue_orchestrator.iterm2.subprocess.run")
    def test_create_session_failure(self, mock_subprocess, mock_run_as):
        """Test failed session creation."""
        mock_run_as.return_value = (False, "error message")

        manager = ITermSessionManager()
        result = manager.create_session(
            issue_number=42,
            command="claude code",
            working_dir="/tmp/test",
        )

        assert result is False
        assert 42 not in manager._sessions

    @patch("issue_orchestrator.iterm2.run_applescript")
    def test_session_exists_running(self, mock_run_as):
        """Test session_exists when session is running."""
        mock_run_as.return_value = (True, "running")

        manager = ITermSessionManager()
        manager._sessions[42] = {"tab_name": "#42"}

        assert manager.session_exists(42) is True

    @patch("issue_orchestrator.iterm2.run_applescript")
    def test_session_exists_idle(self, mock_run_as):
        """Test session_exists when session is idle."""
        mock_run_as.return_value = (True, "idle")

        manager = ITermSessionManager()
        manager._sessions[42] = {"tab_name": "#42"}

        assert manager.session_exists(42) is False

    @patch("issue_orchestrator.iterm2.run_applescript")
    def test_session_exists_not_found(self, mock_run_as):
        """Test session_exists when tab is not found."""
        mock_run_as.return_value = (True, "notfound")

        manager = ITermSessionManager()
        manager._sessions[42] = {"tab_name": "#42"}

        assert manager.session_exists(42) is False
        # Session should be cleaned up
        assert 42 not in manager._sessions

    def test_session_exists_not_tracked(self):
        """Test session_exists for untracked session."""
        manager = ITermSessionManager()

        assert manager.session_exists(42) is False

    @patch("issue_orchestrator.iterm2.run_applescript")
    def test_session_exists_applescript_failure(self, mock_run_as):
        """Test session_exists when AppleScript fails."""
        mock_run_as.return_value = (False, "error")

        manager = ITermSessionManager()
        manager._sessions[42] = {"tab_name": "#42"}

        assert manager.session_exists(42) is False
        # Session should be cleaned up
        assert 42 not in manager._sessions

    @patch("issue_orchestrator.iterm2.run_applescript")
    def test_kill_session_success(self, mock_run_as):
        """Test successful session kill."""
        mock_run_as.return_value = (True, "true")

        manager = ITermSessionManager()
        manager._sessions[42] = {"tab_name": "#42"}

        result = manager.kill_session(42)

        assert result is True
        assert 42 not in manager._sessions

    @patch("issue_orchestrator.iterm2.run_applescript")
    def test_kill_session_not_found(self, mock_run_as):
        """Test killing session that doesn't exist."""
        mock_run_as.return_value = (True, "false")

        manager = ITermSessionManager()
        manager._sessions[42] = {"tab_name": "#42"}

        result = manager.kill_session(42)

        assert result is False
        assert 42 not in manager._sessions  # Still cleaned up from tracking

    @patch("issue_orchestrator.iterm2.run_applescript")
    def test_kill_session_untracked(self, mock_run_as):
        """Test killing untracked session."""
        mock_run_as.return_value = (True, "true")

        manager = ITermSessionManager()

        result = manager.kill_session(42)

        assert result is True

    @patch("issue_orchestrator.iterm2.select_tab_by_name")
    def test_select_session_success(self, mock_select):
        """Test successful session selection."""
        mock_select.return_value = True

        manager = ITermSessionManager()
        result = manager.select_session(42)

        assert result is True
        mock_select.assert_called_once_with("#42")

    @patch("issue_orchestrator.iterm2.select_tab_by_name")
    def test_select_session_failure(self, mock_select):
        """Test failed session selection."""
        mock_select.return_value = False

        manager = ITermSessionManager()
        result = manager.select_session(42)

        assert result is False

    @patch("issue_orchestrator.iterm2.run_applescript")
    def test_send_to_session_success(self, mock_run_as):
        """Test successfully sending text to a session."""
        mock_run_as.return_value = (True, "true")

        manager = ITermSessionManager()
        result = manager.send_to_session(42, "echo hello")

        assert result is True
        script = mock_run_as.call_args[0][0]
        assert "echo hello" in script
        assert "#42" in script
        assert "com.googlecode.iterm2" in script

    @patch("issue_orchestrator.iterm2.run_applescript")
    def test_send_to_session_not_found(self, mock_run_as):
        """Test sending text when session is not found."""
        mock_run_as.return_value = (True, "false")

        manager = ITermSessionManager()
        result = manager.send_to_session(42, "echo hello")

        assert result is False

    @patch("issue_orchestrator.iterm2.run_applescript")
    def test_send_to_session_with_quotes(self, mock_run_as):
        """Test sending text with quotes."""
        mock_run_as.return_value = (True, "true")

        manager = ITermSessionManager()
        result = manager.send_to_session(42, 'echo "test"')

        assert result is True
        script = mock_run_as.call_args[0][0]
        # Quotes should be escaped
        assert '\\"' in script

    @patch("issue_orchestrator.iterm2.run_applescript")
    def test_send_to_session_applescript_failure(self, mock_run_as):
        """Test sending text when AppleScript fails."""
        mock_run_as.return_value = (False, "error")

        manager = ITermSessionManager()
        result = manager.send_to_session(42, "echo hello")

        assert result is False

    @patch("issue_orchestrator.iterm2.run_applescript")
    def test_list_sessions_empty(self, mock_run_as):
        """Test listing sessions when none exist."""
        manager = ITermSessionManager()

        result = manager.list_sessions()

        assert result == []

    @patch("issue_orchestrator.iterm2.run_applescript")
    def test_list_sessions_all_valid(self, mock_run_as):
        """Test listing sessions when all are valid."""
        mock_run_as.return_value = (True, "running")

        manager = ITermSessionManager()
        manager._sessions = {
            1: {"tab_name": "#1"},
            2: {"tab_name": "#2"},
            3: {"tab_name": "#3"},
        }

        result = manager.list_sessions()

        assert set(result) == {1, 2, 3}

    @patch("issue_orchestrator.iterm2.run_applescript")
    def test_list_sessions_some_invalid(self, mock_run_as):
        """Test listing sessions when some are no longer valid."""
        def side_effect(script):
            if "#1" in script:
                return (True, "running")
            elif "#2" in script:
                return (True, "idle")
            else:
                return (True, "notfound")

        mock_run_as.side_effect = side_effect

        manager = ITermSessionManager()
        manager._sessions = {
            1: {"tab_name": "#1"},
            2: {"tab_name": "#2"},
            3: {"tab_name": "#3"},
        }

        result = manager.list_sessions()

        # Only session 1 should be valid (running)
        assert result == [1]

    def test_get_session_count_empty(self):
        """Test getting session count when empty."""
        manager = ITermSessionManager()

        with patch.object(manager, 'list_sessions', return_value=[]):
            assert manager.get_session_count() == 0

    def test_get_session_count_with_sessions(self):
        """Test getting session count with active sessions."""
        manager = ITermSessionManager()

        with patch.object(manager, 'list_sessions', return_value=[1, 2, 3]):
            assert manager.get_session_count() == 3


class TestGetItermManager:
    """Test the get_iterm_manager singleton function."""

    def test_get_manager_creates_instance(self):
        """Test that get_iterm_manager creates an instance."""
        # Reset the global variable
        import issue_orchestrator.iterm2 as iterm2_module
        iterm2_module._iterm_manager = None

        manager = get_iterm_manager()

        assert isinstance(manager, ITermSessionManager)

    def test_get_manager_returns_same_instance(self):
        """Test that get_iterm_manager returns the same instance."""
        import issue_orchestrator.iterm2 as iterm2_module
        iterm2_module._iterm_manager = None

        manager1 = get_iterm_manager()
        manager2 = get_iterm_manager()

        assert manager1 is manager2

    def test_get_manager_preserves_state(self):
        """Test that the singleton preserves state."""
        import issue_orchestrator.iterm2 as iterm2_module
        iterm2_module._iterm_manager = None

        manager1 = get_iterm_manager()
        manager1._sessions[42] = {"tab_name": "#42"}

        manager2 = get_iterm_manager()

        assert 42 in manager2._sessions
