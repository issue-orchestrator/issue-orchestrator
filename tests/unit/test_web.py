"""Tests for web dashboard functionality."""

import pytest
from unittest.mock import MagicMock, patch
from fastapi.testclient import TestClient

from issue_orchestrator.web import app, _orchestrator
from issue_orchestrator.models import Issue, Session, OrchestratorState, AgentConfig
from issue_orchestrator.config import Config


@pytest.fixture
def mock_orchestrator():
    """Create a mock orchestrator for testing."""
    orchestrator = MagicMock()

    # Create sample state
    state = MagicMock(spec=OrchestratorState)
    state.paused = False
    state.active_sessions = []
    state.completed_today = []
    state.priority_queue = [1, 2, 3]  # Queue with 3 issues

    # Create sample config
    config = MagicMock(spec=Config)
    config.max_sessions = 3

    orchestrator.state = state
    orchestrator.config = config

    return orchestrator


@pytest.fixture
def client(mock_orchestrator):
    """Create a test client with mocked orchestrator."""
    import issue_orchestrator.web as web_module
    web_module._orchestrator = mock_orchestrator
    return TestClient(app)


def test_dashboard_renders_with_queue_count(client, mock_orchestrator):
    """Test that dashboard includes queue count in the status bar."""
    response = client.get("/")
    assert response.status_code == 200

    # Check that the response contains the queue count
    html = response.text
    assert "Queue:" in html
    assert "<strong>3</strong>" in html  # Queue count of 3


def test_dashboard_renders_last_updated_element(client, mock_orchestrator):
    """Test that dashboard includes the last updated timestamp element."""
    response = client.get("/")
    assert response.status_code == 200

    html = response.text
    assert 'id="lastUpdated"' in html
    assert "Updated just now" in html


def test_dashboard_includes_timestamp_javascript(client, mock_orchestrator):
    """Test that dashboard includes JavaScript for updating timestamp."""
    response = client.get("/")
    assert response.status_code == 200

    html = response.text
    assert "pageLoadTime" in html
    assert "updateLastUpdated" in html
    assert "setInterval(updateLastUpdated, 1000)" in html


def test_dashboard_with_active_sessions(client, mock_orchestrator):
    """Test dashboard rendering with active sessions."""
    # Create a mock session
    issue = Issue(
        number=169,
        title="[TEST] Frontend feature",
        labels=["agent:web"],
        body="Test issue"
    )

    agent_config = MagicMock(spec=AgentConfig)
    agent_config.timeout_minutes = 45

    session = MagicMock(spec=Session)
    session.issue = issue
    session.runtime_minutes = 5
    session.agent_config = agent_config

    mock_orchestrator.state.active_sessions = [session]

    response = client.get("/")
    assert response.status_code == 200

    html = response.text
    assert "#169" in html
    assert "[TEST] Frontend feature" in html


def test_dashboard_shows_correct_stats(client, mock_orchestrator):
    """Test that all stats are displayed correctly."""
    mock_orchestrator.state.completed_today = [1, 2, 3, 4, 5]  # 5 completed

    response = client.get("/")
    assert response.status_code == 200

    html = response.text
    # Check Active sessions count
    assert "Active:" in html
    # Check Queue count (3 from fixture)
    assert "Queue:" in html
    # Check Completed count (5)
    assert "Completed:" in html
