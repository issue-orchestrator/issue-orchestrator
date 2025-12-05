"""Unit tests for web dashboard."""

import pytest
from datetime import datetime
from pathlib import Path
from fastapi.testclient import TestClient
from unittest.mock import Mock, MagicMock

from issue_orchestrator.web import app, _orchestrator
from issue_orchestrator.models import (
    Issue,
    Session,
    SessionStatus,
    AgentConfig,
    OrchestratorState,
)


@pytest.fixture
def mock_orchestrator():
    """Create a mock orchestrator for testing."""
    orchestrator = Mock()

    # Create test config
    config = Mock()
    config.max_sessions = 3
    orchestrator.config = config

    # Create test state with active sessions
    state = OrchestratorState()
    state.paused = False
    state.completed_today = [100, 101]
    state.priority_queue = [200, 201]

    # Create test sessions
    issue1 = Issue(
        number=150,
        title="Test Frontend Feature",
        labels=["agent:frontend", "priority:high"],
    )
    agent_config1 = AgentConfig(
        prompt_path=Path("test.md"),
        worktree_base=Path("/tmp/test"),
        timeout_minutes=45,
    )
    session1 = Session(
        issue=issue1,
        agent_config=agent_config1,
        tmux_session_name="session-150",
        worktree_path=Path("/tmp/test/150"),
        branch_name="150-test-feature",
    )
    # Manually set runtime to simulate running session
    session1.started_at = datetime.now()

    issue2 = Issue(
        number=151,
        title="Test Backend API",
        labels=["agent:backend", "priority:medium"],
    )
    agent_config2 = AgentConfig(
        prompt_path=Path("test.md"),
        worktree_base=Path("/tmp/test"),
        timeout_minutes=30,
    )
    session2 = Session(
        issue=issue2,
        agent_config=agent_config2,
        tmux_session_name="session-151",
        worktree_path=Path("/tmp/test/151"),
        branch_name="151-test-api",
    )
    session2.started_at = datetime.now()

    state.active_sessions = [session1, session2]
    orchestrator.state = state

    return orchestrator


@pytest.fixture
def client(mock_orchestrator):
    """Create test client with mock orchestrator."""
    import issue_orchestrator.web as web_module
    web_module._orchestrator = mock_orchestrator

    with TestClient(app) as client:
        yield client

    # Cleanup
    web_module._orchestrator = None


class TestDashboardEndpoint:
    """Test the main dashboard endpoint."""

    def test_dashboard_renders_html(self, client):
        """Test that dashboard returns HTML content."""
        response = client.get("/")
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]

    def test_dashboard_displays_sessions(self, client):
        """Test that dashboard shows active sessions."""
        response = client.get("/")
        content = response.text

        # Check for session titles
        assert "Test Frontend Feature" in content
        assert "Test Backend API" in content

        # Check for issue numbers
        assert "#150" in content
        assert "#151" in content

    def test_dashboard_displays_agent_types(self, client):
        """Test that dashboard shows agent types."""
        response = client.get("/")
        content = response.text

        assert "frontend" in content
        assert "backend" in content

    def test_dashboard_displays_orchestrator_state(self, client):
        """Test that dashboard shows orchestrator state."""
        response = client.get("/")
        content = response.text

        # Should show completed count
        assert "2" in content  # 2 completed issues

        # Should show max sessions
        assert "3" in content  # max_sessions = 3

    def test_dashboard_without_orchestrator(self):
        """Test dashboard when orchestrator is not running."""
        import issue_orchestrator.web as web_module
        web_module._orchestrator = None

        with TestClient(app) as client:
            response = client.get("/")
            # Should still render but with empty state
            assert response.status_code == 200

        web_module._orchestrator = None


class TestStatusAPI:
    """Test the /api/status endpoint."""

    def test_status_returns_json(self, client):
        """Test that status endpoint returns JSON."""
        response = client.get("/api/status")
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/json"

    def test_status_includes_sessions(self, client):
        """Test that status includes active sessions."""
        response = client.get("/api/status")
        data = response.json()

        assert "active_sessions" in data
        assert len(data["active_sessions"]) == 2

        # Check session data structure
        session = data["active_sessions"][0]
        assert "issue_number" in session
        assert "title" in session
        assert "runtime_minutes" in session
        assert "agent_type" in session
        assert "status" in session
        assert "branch" in session

    def test_status_includes_orchestrator_state(self, client):
        """Test that status includes orchestrator state."""
        response = client.get("/api/status")
        data = response.json()

        assert data["paused"] is False
        assert data["max_sessions"] == 3
        assert len(data["completed_today"]) == 2
        assert len(data["queue"]) == 2

    def test_status_without_orchestrator(self):
        """Test status endpoint when orchestrator is not running."""
        import issue_orchestrator.web as web_module
        web_module._orchestrator = None

        with TestClient(app) as client:
            response = client.get("/api/status")
            assert response.status_code == 503
            assert "error" in response.json()

        web_module._orchestrator = None

    def test_status_session_running(self, client):
        """Test that sessions show 'running' status when under timeout."""
        response = client.get("/api/status")
        data = response.json()

        # Both sessions should be running (just started)
        for session in data["active_sessions"]:
            assert session["status"] == "running"


class TestPauseAPI:
    """Test the /api/pause endpoint."""

    def test_pause_calls_orchestrator(self, client, mock_orchestrator):
        """Test that pause endpoint calls orchestrator.pause()."""
        response = client.post("/api/pause")

        assert response.status_code == 200
        assert response.json()["status"] == "paused"
        mock_orchestrator.pause.assert_called_once()

    def test_pause_without_orchestrator(self):
        """Test pause endpoint when orchestrator is not running."""
        import issue_orchestrator.web as web_module
        web_module._orchestrator = None

        with TestClient(app) as client:
            response = client.post("/api/pause")
            assert response.status_code == 503
            assert "error" in response.json()

        web_module._orchestrator = None


class TestResumeAPI:
    """Test the /api/resume endpoint."""

    def test_resume_calls_orchestrator(self, client, mock_orchestrator):
        """Test that resume endpoint calls orchestrator.resume()."""
        response = client.post("/api/resume")

        assert response.status_code == 200
        assert response.json()["status"] == "resumed"
        mock_orchestrator.resume.assert_called_once()

    def test_resume_without_orchestrator(self):
        """Test resume endpoint when orchestrator is not running."""
        import issue_orchestrator.web as web_module
        web_module._orchestrator = None

        with TestClient(app) as client:
            response = client.post("/api/resume")
            assert response.status_code == 503
            assert "error" in response.json()

        web_module._orchestrator = None


class TestFocusAPI:
    """Test the /api/focus/{issue_number} endpoint."""

    def test_focus_existing_session(self, client, mock_orchestrator, monkeypatch):
        """Test focusing an existing session."""
        # Mock the iterm2 select_tab_by_name function
        mock_select = Mock(return_value=True)
        monkeypatch.setattr("issue_orchestrator.iterm2.select_tab_by_name", mock_select)

        response = client.post("/api/focus/150")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "focused"
        assert data["issue_number"] == 150
        mock_select.assert_called_once_with("#150")

    def test_focus_nonexistent_session(self, client):
        """Test focusing a session that doesn't exist."""
        response = client.post("/api/focus/999")

        assert response.status_code == 404
        assert "error" in response.json()
        assert "999" in response.json()["error"]

    def test_focus_without_orchestrator(self):
        """Test focus endpoint when orchestrator is not running."""
        import issue_orchestrator.web as web_module
        web_module._orchestrator = None

        with TestClient(app) as client:
            response = client.post("/api/focus/150")
            assert response.status_code == 503
            assert "error" in response.json()

        web_module._orchestrator = None

    def test_focus_fallback_to_tmux(self, client, monkeypatch):
        """Test that focus falls back to tmux when iTerm2 fails."""
        # Mock iterm2 to fail
        mock_select_iterm = Mock(return_value=False)
        monkeypatch.setattr("issue_orchestrator.iterm2.select_tab_by_name", mock_select_iterm)

        # Mock tmux manager
        mock_manager = Mock()
        mock_manager.select_window.return_value = True
        mock_get_manager = Mock(return_value=mock_manager)
        monkeypatch.setattr("issue_orchestrator.tmux.get_manager", mock_get_manager)

        response = client.post("/api/focus/150")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "focused"
        assert data["issue_number"] == 150
        mock_manager.select_window.assert_called_once_with(150)


class TestShutdownAPI:
    """Test the /api/shutdown endpoint."""

    def test_shutdown_calls_orchestrator(self, client, mock_orchestrator):
        """Test that shutdown endpoint calls orchestrator.request_shutdown()."""
        response = client.post("/api/shutdown")

        assert response.status_code == 200
        assert response.json()["status"] == "shutdown_requested"
        mock_orchestrator.request_shutdown.assert_called_once()

    def test_shutdown_without_orchestrator(self):
        """Test shutdown endpoint when orchestrator is not running."""
        import issue_orchestrator.web as web_module
        web_module._orchestrator = None

        with TestClient(app) as client:
            response = client.post("/api/shutdown")
            assert response.status_code == 503
            assert "error" in response.json()

        web_module._orchestrator = None
