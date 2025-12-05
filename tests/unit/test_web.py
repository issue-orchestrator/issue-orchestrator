"""Tests for web dashboard module."""

from unittest.mock import Mock
import pytest
from fastapi.testclient import TestClient

from issue_orchestrator.web import app
from issue_orchestrator import web


class TestWebDashboard:
    """Tests for the web dashboard API endpoints."""

    @pytest.fixture
    def mock_orchestrator(self):
        """Create a mock orchestrator for testing."""
        orchestrator = Mock()
        orchestrator.state = Mock()
        orchestrator.config = Mock()
        orchestrator.state.paused = False
        orchestrator.state.active_sessions = []
        orchestrator.state.completed_today = []
        orchestrator.state.priority_queue = []
        orchestrator.config.max_sessions = 5
        return orchestrator

    @pytest.fixture
    def client(self, mock_orchestrator):
        """Create a test client with mocked orchestrator."""
        web._orchestrator = mock_orchestrator
        return TestClient(app)

    def test_api_status_returns_sessions(self, client, mock_orchestrator):
        """Verify that /api/status returns active sessions."""
        mock_session = Mock()
        mock_session.issue = Mock()
        mock_session.issue.number = 456
        mock_session.issue.title = "Add Feature"
        mock_session.issue.agent_type = "frontend"
        mock_session.runtime_minutes = 15
        mock_session.agent_config = Mock()
        mock_session.agent_config.timeout_minutes = 60
        mock_session.branch_name = "456-add-feature"

        mock_orchestrator.state.active_sessions = [mock_session]

        response = client.get("/api/status")
        assert response.status_code == 200
        data = response.json()
        assert not data["paused"]
        assert len(data["active_sessions"]) == 1
        assert data["active_sessions"][0]["issue_number"] == 456
        assert data["active_sessions"][0]["title"] == "Add Feature"

    def test_pause_endpoint(self, client, mock_orchestrator):
        """Verify that /api/pause calls orchestrator.pause()."""
        mock_orchestrator.pause = Mock()

        response = client.post("/api/pause")
        assert response.status_code == 200
        assert response.json() == {"status": "paused"}
        mock_orchestrator.pause.assert_called_once()

    def test_resume_endpoint(self, client, mock_orchestrator):
        """Verify that /api/resume calls orchestrator.resume()."""
        mock_orchestrator.resume = Mock()

        response = client.post("/api/resume")
        assert response.status_code == 200
        assert response.json() == {"status": "resumed"}
        mock_orchestrator.resume.assert_called_once()

    def test_shutdown_endpoint(self, client, mock_orchestrator):
        """Verify that /api/shutdown requests shutdown."""
        mock_orchestrator.request_shutdown = Mock()

        response = client.post("/api/shutdown")
        assert response.status_code == 200
        assert response.json() == {"status": "shutdown_requested"}
        mock_orchestrator.request_shutdown.assert_called_once()

    def test_api_status_no_orchestrator(self):
        """Verify proper error when orchestrator is not running."""
        web._orchestrator = None
        client = TestClient(app)

        response = client.get("/api/status")
        assert response.status_code == 503
        assert "error" in response.json()
