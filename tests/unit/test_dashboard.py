"""Unit tests for dashboard components."""

import pytest
from unittest.mock import MagicMock
from issue_orchestrator.dashboard import StatusBar
from issue_orchestrator.models import OrchestratorState, Session
from pathlib import Path


class TestStatusBar:
    """Test the StatusBar component."""

    def test_render_content_basic(self, sample_agent_config, sample_issues):
        """Test basic status bar rendering."""
        # Create a mock orchestrator
        mock_orchestrator = MagicMock()
        mock_orchestrator.state = OrchestratorState(
            active_sessions=[],
            completed_today=[],
            paused=False,
            priority_queue=[],
        )
        mock_orchestrator.config.max_sessions = 3

        status_bar = StatusBar(mock_orchestrator)
        content = status_bar._render_content()

        assert "[bold]issue-orchestrator[/bold]" in content
        assert "[green]RUNNING[/green]" in content
        assert "Active: 0/3" in content
        assert "Queue: 0" in content
        assert "Completed: 0" in content

    def test_render_content_paused(self, sample_agent_config, sample_issues):
        """Test status bar when orchestrator is paused."""
        mock_orchestrator = MagicMock()
        mock_orchestrator.state = OrchestratorState(
            active_sessions=[],
            completed_today=[],
            paused=True,
            priority_queue=[],
        )
        mock_orchestrator.config.max_sessions = 3

        status_bar = StatusBar(mock_orchestrator)
        content = status_bar._render_content()

        assert "[yellow]PAUSED[/yellow]" in content
        assert "[green]RUNNING[/green]" not in content

    def test_render_content_with_sessions(self, sample_agent_config, sample_issues):
        """Test status bar with active sessions."""
        session1 = Session(
            issue=sample_issues[0],
            agent_config=sample_agent_config,
            tmux_session_name="session-1",
            worktree_path=Path("/tmp/work1"),
            branch_name="feature/1",
        )
        session2 = Session(
            issue=sample_issues[1],
            agent_config=sample_agent_config,
            tmux_session_name="session-2",
            worktree_path=Path("/tmp/work2"),
            branch_name="feature/2",
        )

        mock_orchestrator = MagicMock()
        mock_orchestrator.state = OrchestratorState(
            active_sessions=[session1, session2],
            completed_today=[3, 4, 5],
            paused=False,
            priority_queue=[6, 7, 8],
        )
        mock_orchestrator.config.max_sessions = 3

        status_bar = StatusBar(mock_orchestrator)
        content = status_bar._render_content()

        assert "Active: 2/3" in content
        assert "Queue: 3" in content
        assert "Completed: 3" in content

    def test_render_content_excludes_active_from_queue(self, sample_agent_config, sample_issues):
        """Test that queue count excludes currently active issues."""
        session1 = Session(
            issue=sample_issues[0],  # Issue #1
            agent_config=sample_agent_config,
            tmux_session_name="session-1",
            worktree_path=Path("/tmp/work1"),
            branch_name="feature/1",
        )

        mock_orchestrator = MagicMock()
        mock_orchestrator.state = OrchestratorState(
            active_sessions=[session1],
            completed_today=[],
            paused=False,
            priority_queue=[1, 2, 3],  # Issue #1 is both active and in queue
        )
        mock_orchestrator.config.max_sessions = 3

        status_bar = StatusBar(mock_orchestrator)
        content = status_bar._render_content()

        # Queue should only show 2 (issues 2 and 3), not 3
        assert "Queue: 2" in content
        assert "Active: 1/3" in content

    def test_render_content_full_capacity(self, sample_agent_config, sample_issues):
        """Test status bar when at full capacity."""
        sessions = [
            Session(
                issue=sample_issues[i],
                agent_config=sample_agent_config,
                tmux_session_name=f"session-{i}",
                worktree_path=Path(f"/tmp/work{i}"),
                branch_name=f"feature/{i}",
            )
            for i in range(3)
        ]

        mock_orchestrator = MagicMock()
        mock_orchestrator.state = OrchestratorState(
            active_sessions=sessions,
            completed_today=[10, 11],
            paused=False,
            priority_queue=[1, 2, 3, 4, 5],
        )
        mock_orchestrator.config.max_sessions = 3

        status_bar = StatusBar(mock_orchestrator)
        content = status_bar._render_content()

        assert "Active: 3/3" in content
        assert "Queue: 2" in content  # Only 2 issues not in active sessions
        assert "Completed: 2" in content
