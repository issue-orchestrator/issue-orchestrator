"""Unit tests for dashboard UI components."""

import pytest
from unittest.mock import Mock
from issue_orchestrator.dashboard import StatusBar
from issue_orchestrator.models import OrchestratorState
from issue_orchestrator.config import Config


class TestStatusBar:
    """Test the StatusBar component."""

    def test_status_bar_shows_failed_count(self):
        """Test that the status bar displays failed sessions count."""
        # Create mock orchestrator with state
        mock_orchestrator = Mock()
        mock_orchestrator.config = Mock(spec=Config)
        mock_orchestrator.config.max_sessions = 3

        # Create state with failed sessions
        state = Mock()
        state.paused = False
        state.active_sessions = []
        state.completed_today = []
        state.failed_today = [Mock(), Mock()]  # 2 failed sessions

        mock_orchestrator.state = state

        # Create StatusBar and render
        status_bar = StatusBar(mock_orchestrator)
        content = status_bar._render_content()

        # Verify failed count is shown
        text = content.plain
        assert "Failed: 2" in text

    def test_status_bar_hides_failed_when_zero(self):
        """Test that failed count is hidden when there are no failures."""
        mock_orchestrator = Mock()
        mock_orchestrator.config = Mock(spec=Config)
        mock_orchestrator.config.max_sessions = 3

        state = Mock()
        state.paused = False
        state.active_sessions = []
        state.completed_today = []
        state.failed_today = []  # No failed sessions

        mock_orchestrator.state = state

        status_bar = StatusBar(mock_orchestrator)
        content = status_bar._render_content()

        text = content.plain
        assert "Failed" not in text


    def test_status_bar_shows_running_status(self):
        """Test that status bar shows RUNNING when not paused."""
        mock_orchestrator = Mock()
        mock_orchestrator.config = Mock(spec=Config)
        mock_orchestrator.config.max_sessions = 3

        state = Mock()
        state.paused = False
        state.active_sessions = []
        state.completed_today = []
        state.failed_today = []

        mock_orchestrator.state = state

        status_bar = StatusBar(mock_orchestrator)
        content = status_bar._render_content()

        assert "RUNNING" in content.plain

    def test_status_bar_shows_paused_status(self):
        """Test that status bar shows PAUSED when paused."""
        mock_orchestrator = Mock()
        mock_orchestrator.config = Mock(spec=Config)
        mock_orchestrator.config.max_sessions = 3

        state = Mock()
        state.paused = True
        state.active_sessions = []
        state.completed_today = []
        state.failed_today = []

        mock_orchestrator.state = state

        status_bar = StatusBar(mock_orchestrator)
        content = status_bar._render_content()

        assert "PAUSED" in content.plain
