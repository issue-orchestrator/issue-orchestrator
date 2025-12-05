"""Tests for the web dashboard API."""

import pytest
from datetime import datetime
from unittest.mock import MagicMock
from fastapi.testclient import TestClient

from issue_orchestrator.web import app, _orchestrator
from issue_orchestrator.models import (
    Session,
    Issue,
    AgentConfig,
    OrchestratorState,
    SessionStatus,
)
from issue_orchestrator.config import Config


@pytest.fixture
def mock_orchestrator(tmp_path):
    """Create a mock orchestrator with sample state."""
    # Create sample agent config
    prompt_file = tmp_path / "prompt.txt"
    prompt_file.write_text("Test prompt")

    agent_config = AgentConfig(
        prompt_path=prompt_file,
        worktree_base=tmp_path,
        model="sonnet",
        timeout_minutes=45,
    )

    # Create sample issues
    issue1 = Issue(
        number=1,
        title="Test issue 1",
        labels=["agent:web", "priority:high"],
    )
    issue2 = Issue(
        number=2,
        title="Test issue 2",
        labels=["agent:web", "priority:medium"],
    )
    issue3 = Issue(
        number=3,
        title="Slow issue",
        labels=["agent:web", "priority:low"],
    )

    # Create sessions with different runtime
    session1 = Session(
        issue=issue1,
        agent_config=agent_config,
        tmux_session_name="test-1",
        worktree_path=tmp_path / "worktree-1",
        branch_name="1-test-issue-1",
        started_at=datetime.now(),
        status=SessionStatus.RUNNING,
    )

    session2 = Session(
        issue=issue2,
        agent_config=agent_config,
        tmux_session_name="test-2",
        worktree_path=tmp_path / "worktree-2",
        branch_name="2-test-issue-2",
        started_at=datetime.now(),
        status=SessionStatus.RUNNING,
    )

    # Create a slow session (override runtime_minutes)
    session3 = Session(
        issue=issue3,
        agent_config=agent_config,
        tmux_session_name="test-3",
        worktree_path=tmp_path / "worktree-3",
        branch_name="3-slow-issue",
        started_at=datetime.now(),
        status=SessionStatus.RUNNING,
    )
    # Simulate a session that has been running for 50 minutes (over timeout)
    from datetime import timedelta
    session3.started_at = datetime.now() - timedelta(minutes=50)

    # Create orchestrator state
    state = OrchestratorState(
        active_sessions=[session1, session2, session3],
        completed_today=[10, 11, 12],
        paused=False,
        priority_queue=[4, 5],
    )

    # Create config
    config = Config()
    config.max_sessions = 3

    # Create mock orchestrator
    mock_orch = MagicMock()
    mock_orch.state = state
    mock_orch.config = config

    return mock_orch


@pytest.fixture
def client(mock_orchestrator):
    """Create a test client with mocked orchestrator."""
    import issue_orchestrator.web as web_module
    web_module._orchestrator = mock_orchestrator

    client = TestClient(app)
    yield client

    # Cleanup
    web_module._orchestrator = None


def test_get_stats_success(client, mock_orchestrator):
    """Test /api/stats returns correct statistics."""
    response = client.get("/api/stats")

    assert response.status_code == 200
    data = response.json()

    # Verify all expected fields are present
    assert "total_active" in data
    assert "running" in data
    assert "slow" in data
    assert "queued" in data
    assert "completed_today" in data
    assert "paused" in data

    # Verify correct values
    assert data["total_active"] == 3
    assert data["running"] == 2  # 2 sessions under timeout
    assert data["slow"] == 1  # 1 session over timeout
    assert data["queued"] == 2
    assert data["completed_today"] == 3
    assert data["paused"] is False


def test_get_stats_paused_state(client, mock_orchestrator):
    """Test /api/stats returns correct paused state."""
    # Modify state to be paused
    mock_orchestrator.state.paused = True

    response = client.get("/api/stats")

    assert response.status_code == 200
    data = response.json()
    assert data["paused"] is True


def test_get_stats_no_sessions(client, mock_orchestrator):
    """Test /api/stats with no active sessions."""
    # Clear all sessions
    mock_orchestrator.state.active_sessions = []

    response = client.get("/api/stats")

    assert response.status_code == 200
    data = response.json()
    assert data["total_active"] == 0
    assert data["running"] == 0
    assert data["slow"] == 0


def test_get_stats_no_orchestrator():
    """Test /api/stats returns error when orchestrator not running."""
    import issue_orchestrator.web as web_module
    web_module._orchestrator = None

    client = TestClient(app)
    response = client.get("/api/stats")

    assert response.status_code == 503
    data = response.json()
    assert "error" in data
    assert data["error"] == "Orchestrator not running"


def test_get_stats_all_slow_sessions(client, mock_orchestrator):
    """Test /api/stats when all sessions are slow."""
    # Make all sessions slow by setting their start time to 60 minutes ago
    from datetime import timedelta
    for session in mock_orchestrator.state.active_sessions:
        session.started_at = datetime.now() - timedelta(minutes=60)

    response = client.get("/api/stats")

    assert response.status_code == 200
    data = response.json()
    assert data["running"] == 0
    assert data["slow"] == 3


def test_get_stats_empty_queue_and_completed(client, mock_orchestrator):
    """Test /api/stats with empty queue and no completed tasks."""
    mock_orchestrator.state.priority_queue = []
    mock_orchestrator.state.completed_today = []

    response = client.get("/api/stats")

    assert response.status_code == 200
    data = response.json()
    assert data["queued"] == 0
    assert data["completed_today"] == 0


def test_get_stats_response_format(client):
    """Test /api/stats returns valid JSON with correct types."""
    response = client.get("/api/stats")

    assert response.status_code == 200
    data = response.json()

    # Verify data types
    assert isinstance(data["total_active"], int)
    assert isinstance(data["running"], int)
    assert isinstance(data["slow"], int)
    assert isinstance(data["queued"], int)
    assert isinstance(data["completed_today"], int)
    assert isinstance(data["paused"], bool)


def test_get_stats_running_and_slow_sum_to_total(client):
    """Test that running + slow equals total_active."""
    response = client.get("/api/stats")

    assert response.status_code == 200
    data = response.json()

    # running + slow should equal total_active
    assert data["running"] + data["slow"] == data["total_active"]
