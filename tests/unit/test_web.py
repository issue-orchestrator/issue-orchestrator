"""Unit tests for the web module."""

import pytest
from unittest.mock import MagicMock, patch
from fastapi.testclient import TestClient
from issue_orchestrator.web import app, _orchestrator
from issue_orchestrator.models import (
    Issue,
    Session,
    AgentConfig,
    OrchestratorState,
)
from pathlib import Path


# Test client
@pytest.fixture
def client():
    """Create a test client for the FastAPI app."""
    return TestClient(app)


@pytest.fixture
def mock_orchestrator():
    """Create a mock orchestrator for testing."""
    mock = MagicMock()
    mock.state = OrchestratorState()
    mock.config = MagicMock()
    mock.config.max_sessions = 3
    return mock


@pytest.fixture
def sample_session():
    """Create a sample session for testing."""
    issue = Issue(
        number=123,
        title="Test Issue",
        labels=["agent:backend", "priority:high"],
        body="Test body",
    )
    agent_config = AgentConfig(
        prompt_path=Path("/tmp/prompt.txt"),
        worktree_base=Path("/tmp"),
        model="sonnet",
        timeout_minutes=45,
    )
    return Session(
        issue=issue,
        agent_config=agent_config,
        tmux_session_name="issue-123",
        worktree_path=Path("/tmp/worktree"),
        branch_name="123-test-issue",
    )


class TestStatusEndpoint:
    """Test the /api/status endpoint."""

    def test_status_without_orchestrator(self, client):
        """Test status endpoint when orchestrator is not running."""
        # Import and set global orchestrator to None
        from issue_orchestrator import web
        web._orchestrator = None

        response = client.get("/api/status")

        assert response.status_code == 503
        assert response.json() == {"error": "Orchestrator not running"}

    def test_status_with_no_sessions(self, client, mock_orchestrator):
        """Test status endpoint with no active sessions."""
        from issue_orchestrator import web
        web._orchestrator = mock_orchestrator

        response = client.get("/api/status")

        assert response.status_code == 200
        data = response.json()
        assert data["paused"] is False
        assert data["active_sessions"] == []
        assert data["max_sessions"] == 3
        assert data["completed_today"] == []
        assert data["queue"] == []

    def test_status_with_active_sessions(self, client, mock_orchestrator, sample_session):
        """Test status endpoint with active sessions."""
        from issue_orchestrator import web
        mock_orchestrator.state.active_sessions = [sample_session]
        web._orchestrator = mock_orchestrator

        response = client.get("/api/status")

        assert response.status_code == 200
        data = response.json()
        assert len(data["active_sessions"]) == 1
        session_data = data["active_sessions"][0]
        assert session_data["issue_number"] == 123
        assert session_data["title"] == "Test Issue"
        assert session_data["agent_type"] == "agent:backend"
        assert session_data["branch"] == "123-test-issue"
        assert "status" in session_data
        assert "runtime_minutes" in session_data

    def test_status_with_paused_state(self, client, mock_orchestrator):
        """Test status endpoint when orchestrator is paused."""
        from issue_orchestrator import web
        mock_orchestrator.state.paused = True
        web._orchestrator = mock_orchestrator

        response = client.get("/api/status")

        assert response.status_code == 200
        data = response.json()
        assert data["paused"] is True


class TestPauseEndpoint:
    """Test the /api/pause endpoint."""

    def test_pause_without_orchestrator(self, client):
        """Test pause endpoint when orchestrator is not running."""
        from issue_orchestrator import web
        web._orchestrator = None

        response = client.post("/api/pause")

        assert response.status_code == 503
        assert response.json() == {"error": "Orchestrator not running"}

    def test_pause_with_orchestrator(self, client, mock_orchestrator):
        """Test pause endpoint calls orchestrator.pause()."""
        from issue_orchestrator import web
        web._orchestrator = mock_orchestrator

        response = client.post("/api/pause")

        assert response.status_code == 200
        assert response.json() == {"status": "paused"}
        mock_orchestrator.pause.assert_called_once()


class TestResumeEndpoint:
    """Test the /api/resume endpoint."""

    def test_resume_without_orchestrator(self, client):
        """Test resume endpoint when orchestrator is not running."""
        from issue_orchestrator import web
        web._orchestrator = None

        response = client.post("/api/resume")

        assert response.status_code == 503
        assert response.json() == {"error": "Orchestrator not running"}

    def test_resume_with_orchestrator(self, client, mock_orchestrator):
        """Test resume endpoint calls orchestrator.resume()."""
        from issue_orchestrator import web
        web._orchestrator = mock_orchestrator

        response = client.post("/api/resume")

        assert response.status_code == 200
        assert response.json() == {"status": "resumed"}
        mock_orchestrator.resume.assert_called_once()


class TestFocusEndpoint:
    """Test the /api/focus/{issue_number} endpoint."""

    def test_focus_without_orchestrator(self, client):
        """Test focus endpoint when orchestrator is not running."""
        from issue_orchestrator import web
        web._orchestrator = None

        response = client.post("/api/focus/123")

        assert response.status_code == 503
        assert response.json() == {"error": "Orchestrator not running"}

    def test_focus_session_not_found(self, client, mock_orchestrator):
        """Test focus endpoint when session is not found."""
        from issue_orchestrator import web
        web._orchestrator = mock_orchestrator

        response = client.post("/api/focus/999")

        assert response.status_code == 404
        assert response.json() == {"error": "Session #999 not found"}

    @patch("issue_orchestrator.iterm2.select_tab_by_name")
    def test_focus_session_success_iterm2(self, mock_select_tab, client, mock_orchestrator, sample_session):
        """Test focus endpoint successfully focuses with iTerm2."""
        from issue_orchestrator import web
        mock_orchestrator.state.active_sessions = [sample_session]
        web._orchestrator = mock_orchestrator
        mock_select_tab.return_value = True

        response = client.post("/api/focus/123")

        assert response.status_code == 200
        assert response.json() == {"status": "focused", "issue_number": 123}
        mock_select_tab.assert_called_once_with("#123")

    @patch("issue_orchestrator.tmux.get_manager")
    @patch("issue_orchestrator.iterm2.select_tab_by_name")
    def test_focus_session_fallback_to_tmux(
        self, mock_select_tab, mock_get_manager, client, mock_orchestrator, sample_session
    ):
        """Test focus endpoint falls back to tmux when iTerm2 fails."""
        from issue_orchestrator import web
        mock_orchestrator.state.active_sessions = [sample_session]
        web._orchestrator = mock_orchestrator
        mock_select_tab.return_value = False

        mock_manager = MagicMock()
        mock_manager.select_window.return_value = True
        mock_get_manager.return_value = mock_manager

        response = client.post("/api/focus/123")

        assert response.status_code == 200
        assert response.json() == {"status": "focused", "issue_number": 123}
        mock_select_tab.assert_called_once_with("#123")
        mock_manager.select_window.assert_called_once_with(123)

    @patch("issue_orchestrator.tmux.get_manager")
    @patch("issue_orchestrator.iterm2.select_tab_by_name")
    def test_focus_session_both_fail(
        self, mock_select_tab, mock_get_manager, client, mock_orchestrator, sample_session
    ):
        """Test focus endpoint when both iTerm2 and tmux fail."""
        from issue_orchestrator import web
        mock_orchestrator.state.active_sessions = [sample_session]
        web._orchestrator = mock_orchestrator
        mock_select_tab.return_value = False

        mock_manager = MagicMock()
        mock_manager.select_window.return_value = False
        mock_get_manager.return_value = mock_manager

        response = client.post("/api/focus/123")

        assert response.status_code == 500
        assert response.json() == {"error": "Could not focus session #123"}


class TestShutdownEndpoint:
    """Test the /api/shutdown endpoint."""

    def test_shutdown_without_orchestrator(self, client):
        """Test shutdown endpoint when orchestrator is not running."""
        from issue_orchestrator import web
        web._orchestrator = None

        response = client.post("/api/shutdown")

        assert response.status_code == 503
        assert response.json() == {"error": "Orchestrator not running"}

    def test_shutdown_with_orchestrator(self, client, mock_orchestrator):
        """Test shutdown endpoint calls orchestrator.request_shutdown()."""
        from issue_orchestrator import web
        web._orchestrator = mock_orchestrator

        response = client.post("/api/shutdown")

        assert response.status_code == 200
        assert response.json() == {"status": "shutdown_requested"}
        mock_orchestrator.request_shutdown.assert_called_once()
