"""Tests for the web dashboard."""

import pytest
from pathlib import Path
from fastapi.testclient import TestClient
from unittest.mock import Mock, MagicMock

from issue_orchestrator.web import app, _orchestrator
from issue_orchestrator.config import Config
from issue_orchestrator.models import OrchestratorState, Issue, Session, AgentConfig


@pytest.fixture
def mock_orchestrator(tmp_path):
    """Create a mock orchestrator."""
    orchestrator = Mock()

    # Create mock agent config
    prompt_file = tmp_path / "test.md"
    prompt_file.write_text("Test prompt")

    agent_config = AgentConfig(
        prompt_path=prompt_file,
        worktree_base=tmp_path,
        timeout_minutes=30
    )

    # Create mock config
    orchestrator.config = Config()
    orchestrator.config.repo = "test/repo"
    orchestrator.config.max_sessions = 3
    orchestrator.config.agents["agent:web"] = agent_config

    # Create mock state
    orchestrator.state = OrchestratorState()
    orchestrator.state.paused = False
    orchestrator.state.completed_today = ["1", "2"]
    orchestrator.state.active_sessions = []

    return orchestrator


class TestWebDashboard:
    """Tests for the web dashboard endpoints."""

    def test_dashboard_renders_with_timestamp_element(self, mock_orchestrator):
        """Verify the dashboard HTML includes the last-updated element."""
        # Set the global orchestrator
        import issue_orchestrator.web as web_module
        web_module._orchestrator = mock_orchestrator

        # Clear any template caches to ensure we get the latest version
        from issue_orchestrator.web import get_templates
        templates = get_templates()

        client = TestClient(app)
        response = client.get("/")

        assert response.status_code == 200
        # Check for the timestamp element
        content_str = response.content.decode()
        assert 'id="last-updated"' in content_str
        assert 'updateTimestamp()' in content_str

    def test_dashboard_shows_status_info(self, mock_orchestrator):
        """Verify the dashboard shows status information."""
        import issue_orchestrator.web as web_module
        web_module._orchestrator = mock_orchestrator

        client = TestClient(app)
        response = client.get("/")

        assert response.status_code == 200
        # Should show active sessions count
        assert b'Active:' in response.content
        # Should show completed count
        assert b'Completed:' in response.content

    def test_dashboard_with_active_sessions(self, mock_orchestrator, tmp_path):
        """Verify the dashboard displays active sessions."""
        # Add an active session
        issue = Issue(
            number=123,
            title="Test Issue",
            labels=["agent:web"]  # agent_type is derived from labels
        )

        agent_config = mock_orchestrator.config.agents["agent:web"]

        session = Session(
            issue=issue,
            branch_name="123-test-issue",
            agent_config=agent_config,
            tmux_session_name="test-123",
            worktree_path=tmp_path / "worktree"
        )

        mock_orchestrator.state.active_sessions = [session]

        import issue_orchestrator.web as web_module
        web_module._orchestrator = mock_orchestrator

        client = TestClient(app)
        response = client.get("/")

        assert response.status_code == 200
        assert b'#123' in response.content
        assert b'Test Issue' in response.content
