"""Tests for web dashboard module."""

from unittest.mock import Mock, patch
import pytest
from fastapi.testclient import TestClient

from issue_orchestrator import web


class TestWebDataEndpoints:
    """Tests for web dashboard data endpoints."""

    def test_status_endpoint_includes_branch(self):
        """Verify that API status endpoint includes branch field."""
        # Create mock orchestrator
        mock_orchestrator = Mock()
        mock_state = Mock()
        mock_config = Mock()
        mock_session = Mock()
        mock_issue = Mock()
        mock_agent_config = Mock()

        # Setup mock data
        mock_issue.number = 456
        mock_issue.title = "API Test Issue"
        mock_issue.agent_type = "agent:backend"

        mock_session.issue = mock_issue
        mock_session.runtime_minutes = 10
        mock_session.branch_name = "456-api-test-branch"
        mock_session.agent_config = mock_agent_config
        mock_agent_config.timeout_minutes = 30

        mock_state.active_sessions = [mock_session]
        mock_state.paused = False
        mock_state.completed_today = []
        mock_state.priority_queue = []

        mock_config.max_sessions = 3

        mock_orchestrator.state = mock_state
        mock_orchestrator.config = mock_config

        # Patch the global orchestrator
        with patch.object(web, '_orchestrator', mock_orchestrator):
            # Create test client and make request
            client = TestClient(web.app)
            response = client.get("/api/status")

            # Verify response
            assert response.status_code == 200
            data = response.json()
            assert len(data["active_sessions"]) == 1
            assert data["active_sessions"][0]["branch"] == "456-api-test-branch"
            assert data["active_sessions"][0]["issue_number"] == 456
            assert data["active_sessions"][0]["title"] == "API Test Issue"
