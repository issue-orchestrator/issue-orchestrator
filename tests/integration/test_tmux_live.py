"""Integration tests for tmux operations against a real tmux server.

These tests verify that our tmux code works with actual tmux, catching issues
like libtmux quirks that unit tests with mocks don't detect.

Example: libtmux doesn't populate pane.pane_title as an attribute - you must
use pane.cmd("display-message", "-p", "#{pane_title}") to get it.

These tests require tmux to be installed and will create/destroy test sessions.
"""

import subprocess
import time
from pathlib import Path

import libtmux
import pytest

# Skip all tests if tmux is not available
pytestmark = pytest.mark.skipif(
    subprocess.run(["which", "tmux"], capture_output=True).returncode != 0,
    reason="tmux not installed"
)


def _get_server() -> libtmux.Server:
    """Get a libtmux Server instance."""
    return libtmux.Server()


def _kill_session_if_exists(server: libtmux.Server, session_name: str) -> None:
    """Kill a session if it exists, using libtmux API."""
    sessions = server.sessions.filter(session_name=session_name)
    if sessions:
        sessions[0].kill()


def _set_pane_title(pane: libtmux.Pane, title: str) -> None:
    """Set pane title using libtmux cmd()."""
    pane.cmd("select-pane", "-T", title)


def _get_pane_title(pane: libtmux.Pane) -> str:
    """Get pane title using libtmux cmd() - the reliable way."""
    result = pane.cmd("display-message", "-p", "#{pane_title}")
    return result.stdout[0] if result.stdout else ""


class TestLibtmuxBehavior:
    """Tests that document and verify libtmux behavior quirks.

    These tests exist to:
    1. Prove that certain libtmux behaviors exist (not assumptions)
    2. Detect if libtmux changes behavior in future versions
    3. Serve as documentation for why we do things certain ways
    """

    TEST_SESSION = "test-libtmux-behavior"

    @pytest.fixture(autouse=True)
    def cleanup_session(self):
        """Clean up test session using libtmux API."""
        server = _get_server()
        _kill_session_if_exists(server, self.TEST_SESSION)
        yield
        _kill_session_if_exists(server, self.TEST_SESSION)

    def test_pane_title_attribute_is_none(self):
        """PROVE: libtmux pane.pane_title attribute is None even when title is set.

        This is the root cause of many issues. libtmux does NOT automatically
        populate the pane_title attribute. You MUST use cmd() to get it.
        """
        server = _get_server()

        # Create session using libtmux
        session = server.new_session(
            session_name=self.TEST_SESSION,
            window_name="test-window",
        )

        # Set pane title using cmd
        test_title = "PROVEN-TITLE-12345"
        pane = session.windows[0].active_pane
        _set_pane_title(pane, test_title)

        # Verify the title is set using cmd
        actual_title = _get_pane_title(pane)
        assert actual_title == test_title, f"Title should be set, got '{actual_title}'"

        # THE QUIRK: pane_title attribute is None!
        raw_attr = getattr(pane, "pane_title", "ATTR_MISSING")
        # This assertion documents the behavior
        assert raw_attr is None or raw_attr == "ATTR_MISSING", (
            f"UNEXPECTED: pane.pane_title is '{raw_attr}' - "
            "libtmux may have changed behavior!"
        )

        # But cmd() works (already verified above)

    def test_sessions_filter_returns_list(self):
        """PROVE: sessions.filter() returns a list, not raises exception."""
        server = _get_server()

        # filter() for non-existent session returns empty list
        result = server.sessions.filter(session_name="definitely-does-not-exist-xyz")
        assert result == [], f"filter() should return [], got {result}"

        # NOT like sessions.get() which might raise


class TestTmuxLiveOperations:
    """Test tmux operations against a real tmux server."""

    TEST_SESSION = "test-orchestrator-integration"

    @pytest.fixture(autouse=True)
    def cleanup_session(self):
        """Clean up any existing test session before and after tests."""
        server = _get_server()
        _kill_session_if_exists(server, self.TEST_SESSION)
        yield
        _kill_session_if_exists(server, self.TEST_SESSION)

    def test_pane_title_retrieval(self):
        """Verify we can get pane titles correctly.

        This test catches the libtmux quirk where pane.pane_title is None
        and you must use cmd() to get the actual title.
        """
        from issue_orchestrator.adapters.terminal._tmux import TmuxManager

        server = _get_server()

        # Create session using libtmux
        session = server.new_session(
            session_name=self.TEST_SESSION,
            window_name="test-window",
        )

        # Set pane title using cmd
        test_title = "test-pane-title-12345"
        pane = session.windows[0].active_pane
        _set_pane_title(pane, test_title)

        # Use TmuxManager to find the pane by title
        manager = TmuxManager(session_name=self.TEST_SESSION)

        # Force session lookup
        session = manager.session
        assert session is not None, "Should find test session"

        # Get the window
        windows = session.windows
        assert len(windows) > 0, "Should have at least one window"

        window = windows[0]
        pane = window.active_pane
        assert pane is not None, "Should have active pane"

        # This is the critical test - pane.pane_title will be None!
        raw_title = getattr(pane, "pane_title", None)
        # The raw attribute may or may not be populated - libtmux is inconsistent

        # But our helper should always work
        actual_title = manager._get_pane_title(pane)
        assert actual_title == test_title, (
            f"_get_pane_title should return '{test_title}', "
            f"got '{actual_title}' (raw attr was '{raw_title}')"
        )

    def test_find_pane_by_title(self):
        """Verify _find_pane_by_title works with real tmux."""
        from issue_orchestrator.adapters.terminal._tmux import TmuxManager, AGENTS_WINDOW

        server = _get_server()

        # Create session using libtmux
        session = server.new_session(
            session_name=self.TEST_SESSION,
            window_name="dashboard",
        )

        # Create agents window using libtmux
        agents_window = session.new_window(window_name=AGENTS_WINDOW)

        # Set a specific pane title
        test_title = "#42-test-issue"
        pane = agents_window.active_pane
        _set_pane_title(pane, test_title)

        # Use TmuxManager to find the pane
        manager = TmuxManager(session_name=self.TEST_SESSION)
        _ = manager.session  # Force session lookup

        # Test _find_pane_by_title
        found_pane = manager._find_pane_by_title(test_title)
        assert found_pane is not None, f"Should find pane with title '{test_title}'"

        # Test _find_issue_session (should find by issue number)
        issue_pane = manager._find_issue_session(42)
        assert issue_pane is not None, "Should find issue 42 by number"

    def test_list_issue_windows(self):
        """Verify list_issue_windows extracts issue numbers correctly."""
        from issue_orchestrator.adapters.terminal._tmux import TmuxManager, AGENTS_WINDOW

        server = _get_server()

        # Create session using libtmux
        session = server.new_session(
            session_name=self.TEST_SESSION,
            window_name="dashboard",
        )

        # Create agents window
        agents_window = session.new_window(window_name=AGENTS_WINDOW)

        # Create multiple panes with issue titles
        pane0 = agents_window.active_pane
        _set_pane_title(pane0, "#100-first-issue")

        # Split to create second pane
        pane1 = agents_window.split()
        _set_pane_title(pane1, "#200-second-issue")

        # Use TmuxManager to list issues
        manager = TmuxManager(session_name=self.TEST_SESSION)
        _ = manager.session

        issues = manager.list_issue_windows()
        assert sorted(issues) == [100, 200], f"Should find issues [100, 200], got {issues}"

    def test_window_exists_by_name(self):
        """Verify window_exists_by_name works with real pane titles."""
        from issue_orchestrator.adapters.terminal._tmux import TmuxManager, AGENTS_WINDOW

        server = _get_server()

        # Create session using libtmux
        session = server.new_session(
            session_name=self.TEST_SESSION,
            window_name="dashboard",
        )

        # Create agents window
        agents_window = session.new_window(window_name=AGENTS_WINDOW)

        # Set pane title
        test_title = "review-999"
        pane = agents_window.active_pane
        _set_pane_title(pane, test_title)

        manager = TmuxManager(session_name=self.TEST_SESSION)
        _ = manager.session

        # Should find existing pane
        assert manager.window_exists_by_name(test_title) is True

        # Should not find non-existent pane
        assert manager.window_exists_by_name("nonexistent-pane") is False

    def test_select_window_by_issue(self):
        """Verify select_window can focus a pane by issue number."""
        from issue_orchestrator.adapters.terminal._tmux import TmuxManager, AGENTS_WINDOW

        server = _get_server()

        # Create session using libtmux
        session = server.new_session(
            session_name=self.TEST_SESSION,
            window_name="dashboard",
        )

        # Create agents window
        agents_window = session.new_window(window_name=AGENTS_WINDOW)

        # Set pane title
        pane = agents_window.active_pane
        _set_pane_title(pane, "#777-test-select")

        manager = TmuxManager(session_name=self.TEST_SESSION)
        _ = manager.session

        # Should successfully select
        result = manager.select_window(777)
        assert result is True, "select_window should return True for existing issue"

        # Should fail for non-existent
        result = manager.select_window(999999)
        assert result is False, "select_window should return False for non-existent issue"


class TestTmuxSessionLifecycle:
    """Test full session lifecycle operations."""

    TEST_SESSION = "test-orchestrator-lifecycle"

    @pytest.fixture(autouse=True)
    def cleanup_session(self):
        """Clean up any existing test session."""
        server = _get_server()
        _kill_session_if_exists(server, self.TEST_SESSION)
        yield
        _kill_session_if_exists(server, self.TEST_SESSION)

    def test_create_and_find_issue_window(self, tmp_path):
        """Test creating an issue window and finding it again."""
        from issue_orchestrator.adapters.terminal._tmux import TmuxManager

        manager = TmuxManager(session_name=self.TEST_SESSION)

        # Create the session first
        session = manager.ensure_session()
        assert session is not None

        # Create an issue window
        working_dir = tmp_path
        pane = manager.create_issue_window(
            issue_number=123,
            command="echo 'test'",
            working_dir=working_dir,
            title="Test Issue",
        )
        assert pane is not None

        # Should be able to find it
        found = manager._find_issue_session(123)
        assert found is not None, "Should find issue 123 after creation"

        # Should show in list
        issues = manager.list_issue_windows()
        assert 123 in issues, f"Issue 123 should be in list, got {issues}"

        # Should be able to check existence
        assert manager.window_exists(123) is True

    def test_kill_window_removes_pane(self, tmp_path):
        """Test that kill_window actually removes the pane."""
        from issue_orchestrator.adapters.terminal._tmux import TmuxManager

        manager = TmuxManager(session_name=self.TEST_SESSION)
        session = manager.ensure_session()

        # Create an issue window
        manager.create_issue_window(
            issue_number=456,
            command="sleep 100",
            working_dir=tmp_path,
        )

        # Verify it exists
        assert manager.window_exists(456) is True

        # Kill it
        manager.kill_window(456)

        # Small delay for tmux to process
        time.sleep(0.1)

        # Should no longer exist
        # Note: Need to clear any cached state
        manager._session = None  # Clear cache to force re-lookup
        assert manager.window_exists(456) is False, "Window should not exist after kill"


class TestPaneIdentificationWithClaudeCode:
    """Test that pane identification works even when Claude Code changes the title.

    Claude Code modifies the pane title to show its current task (e.g., "✳ Unit tests").
    Our session ID option (@orchestrator-session-id) must persist and be used for lookup.
    """

    TEST_SESSION = "test-claude-title-change"

    @pytest.fixture(autouse=True)
    def cleanup_session(self):
        """Clean up any existing test session."""
        server = _get_server()
        _kill_session_if_exists(server, self.TEST_SESSION)
        yield
        _kill_session_if_exists(server, self.TEST_SESSION)

    def test_pane_found_after_title_overwrite(self, tmp_path):
        """CRITICAL: Pane must be findable even after application changes title.

        This tests the real-world scenario where:
        1. We create a pane with session_id "#999-test"
        2. Claude Code changes pane_title to something else
        3. We must still be able to find the pane by session_id
        """
        from issue_orchestrator.adapters.terminal._tmux import (
            TmuxManager,
            PANE_SESSION_ID_OPTION,
        )

        manager = TmuxManager(session_name=self.TEST_SESSION)
        session = manager.ensure_session()

        # Create an issue window - this sets both title and session_id option
        pane = manager.create_issue_window(
            issue_number=999,
            command="echo 'test'",
            working_dir=tmp_path,
            title="Test Issue For Title Overwrite",
        )
        assert pane is not None

        # Verify we can find it initially
        found = manager._find_issue_session(999)
        assert found is not None, "Should find issue 999 initially"

        # Get the session_id that was set (now always uses issue-{number} format)
        session_id = manager._get_pane_session_id(pane)
        assert session_id == "issue-999", f"Session ID should be 'issue-999', got {session_id}"

        # SIMULATE: Claude Code changes the pane title (like it does in production)
        # This is what breaks lookup when relying on pane_title
        pane.cmd("select-pane", "-T", "✳ Working on something completely different")

        # Verify the pane title WAS changed
        new_title = _get_pane_title(pane)
        assert "Working on something" in new_title, "Title should have been changed"
        assert "#999" not in new_title, "Title should NOT contain our session ID anymore"

        # CRITICAL: We must STILL be able to find the pane by issue number
        # This is what was broken before - lookup relied on pane_title
        manager._session = None  # Clear any cache
        found_after = manager._find_issue_session(999)
        assert found_after is not None, (
            "CRITICAL: Must find issue 999 even after title was changed! "
            "The @orchestrator-session-id option should persist."
        )

        # Should still show in list
        issues = manager.list_issue_windows()
        assert 999 in issues, f"Issue 999 should still be in list after title change, got {issues}"

        # Should be able to focus it
        assert manager.select_window(999) is True, "Should be able to focus issue 999"

    def test_session_id_option_not_affected_by_application(self, tmp_path):
        """Verify the @orchestrator-session-id option persists through title changes."""
        from issue_orchestrator.adapters.terminal._tmux import (
            TmuxManager,
            PANE_SESSION_ID_OPTION,
        )

        manager = TmuxManager(session_name=self.TEST_SESSION)
        session = manager.ensure_session()

        pane = manager.create_issue_window(
            issue_number=888,
            command="sleep 1",
            working_dir=tmp_path,
            title="Persistence Test",
        )

        # Record the session ID (now always uses issue-{number} format)
        original_session_id = manager._get_pane_session_id(pane)
        assert original_session_id == "issue-888", f"Got: {original_session_id}"

        # Change title multiple times (simulating Claude Code updates)
        for title in ["✳ First task", "✳ Second task", "Something else entirely"]:
            pane.cmd("select-pane", "-T", title)
            time.sleep(0.05)

        # Session ID option must NOT have changed
        final_session_id = manager._get_pane_session_id(pane)
        assert final_session_id == original_session_id, (
            f"Session ID option should not change! "
            f"Original: {original_session_id}, Final: {final_session_id}"
        )


# NOTE: Real Claude Code e2e test is in tests/e2e/test_terminal_adapter.py
# The test_terminal_adapter_session_launch test verifies pane identification
# works with real Claude Code running in a real worktree.
