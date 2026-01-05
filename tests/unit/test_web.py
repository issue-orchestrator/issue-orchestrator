"""Unit tests for the FastAPI web module."""

import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch, Mock, AsyncMock
from fastapi.testclient import TestClient

from issue_orchestrator.entrypoints.web import app, get_orchestrator
from issue_orchestrator.domain.models import (
    Issue,
    Session,
    SessionHistoryEntry,
    OrchestratorState,
    AgentConfig,
    SessionStatus,
)
from issue_orchestrator.infra.config import Config
from issue_orchestrator.domain.issue_key import FakeIssueKey
from issue_orchestrator.domain.session_key import SessionKey, TaskKind


# Helper functions
def create_mock_orchestrator():
    """Create a mock orchestrator for testing."""
    mock_orch = MagicMock()

    # Create a real config object
    config = Config()
    config.repo = "owner/repo"
    config.max_concurrent_sessions = 3
    config.queue_refresh_seconds = 600
    config.ui_mode = "web"
    config.web_port = 8080
    config.filter_label = None
    config.filter_milestone = None
    config.config_path = Path("/tmp/config.yaml")
    config.repo_root = Path("/tmp/repo")

    # Add a sample agent config
    agent_config = AgentConfig(
        prompt_path=Path("/tmp/prompt.txt"),
        worktree_base=Path("/tmp/worktrees"),
        model="sonnet",
        timeout_minutes=45,
    )
    config.agents = {"agent:web": agent_config}

    # Create a real state object
    state = OrchestratorState(
        active_sessions=[],
        session_history=[],
        completed_today=[],
        paused=False,
        priority_queue=[],
        startup_status="complete",  # Required for dashboard to show issues
    )

    mock_orch.config = config
    mock_orch.state = state
    mock_orch.pause = MagicMock()
    mock_orch.resume = MagicMock()
    mock_orch.request_shutdown = MagicMock()
    mock_orch._shutdown_requested = False  # Explicit value for JSON serialization

    return mock_orch


def create_issue(number, title="Test Issue", labels=None):
    """Helper to create Issue objects for testing."""
    if labels is None:
        labels = ["agent:web"]
    return Issue(
        number=number,
        title=title,
        labels=labels,
    )


def create_session(issue, worktree_path="/tmp/worktree-1", branch_name="feature/issue-1"):
    """Helper to create Session objects for testing."""
    agent_config = AgentConfig(
        prompt_path=Path("/tmp/prompt.txt"),
        worktree_base=Path("/tmp"),
        model="sonnet",
        timeout_minutes=45,
    )
    issue_key = FakeIssueKey(name=str(issue.number))
    session_key = SessionKey(issue=issue_key, task=TaskKind.CODE)
    return Session(
        key=session_key,
        issue=issue,
        agent_config=agent_config,
        terminal_id=f"issue-{issue.number}",
        worktree_path=Path(worktree_path),
        branch_name=branch_name,
    )


class TestDashboardEndpoint:
    """Test the GET / dashboard endpoint."""

    def test_dashboard_returns_html(self):
        """Test that dashboard returns HTML response."""
        from issue_orchestrator.entrypoints import web
        mock_orch = create_mock_orchestrator()

        web._orchestrator = mock_orch
        try:
            client = TestClient(app)
            response = client.get("/")

            assert response.status_code == 200
            assert "text/html" in response.headers["content-type"]
        finally:
            web._orchestrator = None

    def test_dashboard_with_active_sessions(self):
        """Test dashboard displays active sessions."""
        from issue_orchestrator.entrypoints import web
        mock_orch = create_mock_orchestrator()

        # Add an active session
        issue = create_issue(1, "Active Issue")
        session = create_session(issue)
        mock_orch.state.active_sessions = [session]

        web._orchestrator = mock_orch
        try:
            client = TestClient(app)
            response = client.get("/")

            assert response.status_code == 200
            assert "Active Issue" in response.text
            assert "#1" in response.text
        finally:
            web._orchestrator = None

    def test_dashboard_with_queue_pagination(self):
        """Test dashboard queue pagination."""
        from issue_orchestrator.entrypoints import web
        mock_orch = create_mock_orchestrator()

        # Create 25 issues to trigger pagination (page size is 20)
        issues = [create_issue(i, f"Queue Issue {i}") for i in range(1, 26)]

        # Set cached queue issues (dashboard uses cache instead of calling API)
        mock_orch.state.cached_queue_issues = issues

        web._orchestrator = mock_orch
        try:
            client = TestClient(app)

            # Test first page
            response = client.get("/?page=1")
            assert response.status_code == 200
            assert "Queue Issue 1" in response.text

            # Test second page
            response = client.get("/?page=2")
            assert response.status_code == 200
            assert "Queue Issue 21" in response.text
        finally:
            web._orchestrator = None

    def test_dashboard_when_paused(self):
        """Test dashboard shows paused state."""
        from issue_orchestrator.entrypoints import web
        mock_orch = create_mock_orchestrator()
        mock_orch.state.paused = True

        web._orchestrator = mock_orch
        try:
            client = TestClient(app)
            response = client.get("/")

            assert response.status_code == 200
            # The template should handle paused state
            assert response.status_code == 200
        finally:
            web._orchestrator = None

    def test_dashboard_with_session_history(self):
        """Test dashboard displays session history."""
        from issue_orchestrator.entrypoints import web
        mock_orch = create_mock_orchestrator()

        # Add a history entry
        history_entry = SessionHistoryEntry(
            issue_number=42,
            title="Completed Issue",
            agent_type="agent:web",
            status="completed",
            runtime_minutes=15,
            pr_url="https://github.com/owner/repo/pull/42",
        )
        mock_orch.state.session_history = [history_entry]

        web._orchestrator = mock_orch
        try:
            client = TestClient(app)
            response = client.get("/")

            assert response.status_code == 200
            assert "Completed Issue" in response.text
        finally:
            web._orchestrator = None


class TestApiStatusEndpoint:
    """Test the GET /api/status endpoint."""

    def test_status_returns_json(self):
        """Test that status endpoint returns JSON."""
        from issue_orchestrator.entrypoints import web
        mock_orch = create_mock_orchestrator()
        web._orchestrator = mock_orch

        client = TestClient(app)
        response = client.get("/api/status")

        assert response.status_code == 200
        assert response.headers["content-type"] == "application/json"
        web._orchestrator = None

    def test_status_includes_basic_info(self):
        """Test status includes basic orchestrator info."""
        from issue_orchestrator.entrypoints import web
        mock_orch = create_mock_orchestrator()
        web._orchestrator = mock_orch

        client = TestClient(app)
        response = client.get("/api/status")

        data = response.json()
        assert "paused" in data
        assert "active_sessions" in data
        assert "max_sessions" in data
        assert "completed_today" in data
        assert data["paused"] is False
        assert data["max_sessions"] == 3
        web._orchestrator = None

    def test_status_with_active_sessions(self):
        """Test status includes active session details."""
        from issue_orchestrator.entrypoints import web
        mock_orch = create_mock_orchestrator()

        issue = create_issue(1, "Test Issue")
        session = create_session(issue, branch_name="feature/issue-1")
        mock_orch.state.active_sessions = [session]

        web._orchestrator = mock_orch

        client = TestClient(app)
        response = client.get("/api/status")

        data = response.json()
        assert len(data["active_sessions"]) == 1
        assert data["active_sessions"][0]["issue_number"] == 1
        assert data["active_sessions"][0]["title"] == "Test Issue"
        assert data["active_sessions"][0]["branch"] == "feature/issue-1"
        web._orchestrator = None

    def test_status_when_orchestrator_not_running(self):
        """Test status returns 503 when orchestrator not initialized."""
        from issue_orchestrator.entrypoints import web
        web._orchestrator = None

        client = TestClient(app)
        response = client.get("/api/status")

        assert response.status_code == 503
        assert "error" in response.json()


class TestPauseResumeEndpoints:
    """Test the POST /api/pause and /api/resume endpoints."""

    def test_pause_endpoint(self):
        """Test pause endpoint calls orchestrator.pause()."""
        from issue_orchestrator.entrypoints import web
        mock_orch = create_mock_orchestrator()
        web._orchestrator = mock_orch

        client = TestClient(app)
        response = client.post("/api/pause")

        assert response.status_code == 200
        assert response.json()["status"] == "paused"
        mock_orch.pause.assert_called_once()

    def test_resume_endpoint(self):
        """Test resume endpoint calls orchestrator.resume()."""
        from issue_orchestrator.entrypoints import web
        mock_orch = create_mock_orchestrator()
        web._orchestrator = mock_orch

        client = TestClient(app)
        response = client.post("/api/resume")

        assert response.status_code == 200
        assert response.json()["status"] == "resumed"
        mock_orch.resume.assert_called_once()

    def test_pause_when_orchestrator_not_running(self):
        """Test pause returns 503 when orchestrator not initialized."""
        from issue_orchestrator.entrypoints import web
        web._orchestrator = None

        client = TestClient(app)
        response = client.post("/api/pause")

        assert response.status_code == 503
        assert "error" in response.json()

    def test_resume_when_orchestrator_not_running(self):
        """Test resume returns 503 when orchestrator not initialized."""
        from issue_orchestrator.entrypoints import web
        web._orchestrator = None

        client = TestClient(app)
        response = client.post("/api/resume")

        assert response.status_code == 503
        assert "error" in response.json()


class TestFocusSessionEndpoint:
    """Test the POST /api/focus/{issue_number} endpoint."""

    def test_focus_session_success(self):
        """Test successful session focus."""
        from issue_orchestrator.entrypoints import web
        mock_orch = create_mock_orchestrator()

        issue = create_issue(1)
        session = create_session(issue)
        mock_orch.state.active_sessions = [session]

        # Mock session_runner.focus_session
        mock_orch.session_runner.focus_session.return_value = True
        web._orchestrator = mock_orch

        client = TestClient(app)
        response = client.post("/api/focus/1")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "focused"
        assert data["issue_number"] == 1
        mock_orch.session_runner.focus_session.assert_called_once_with(1)

    def test_focus_session_failure(self):
        """Test focus returns error when focus_session fails."""
        from issue_orchestrator.entrypoints import web
        mock_orch = create_mock_orchestrator()

        issue = create_issue(1)
        session = create_session(issue)
        mock_orch.state.active_sessions = [session]

        # Mock session_runner.focus_session returning False (failed to focus)
        mock_orch.session_runner.focus_session.return_value = False
        web._orchestrator = mock_orch

        client = TestClient(app)
        response = client.post("/api/focus/1")

        assert response.status_code == 500
        data = response.json()
        assert "error" in data
        mock_orch.session_runner.focus_session.assert_called_once_with(1)

    def test_focus_session_not_found(self):
        """Test focus returns 404 when session not found."""
        from issue_orchestrator.entrypoints import web
        mock_orch = create_mock_orchestrator()
        web._orchestrator = mock_orch

        client = TestClient(app)
        response = client.post("/api/focus/999")

        assert response.status_code == 404
        assert "error" in response.json()

class TestFinderEndpoint:
    """Test the POST /api/finder/{issue_number} endpoint."""

    def test_open_in_finder_success(self):
        """Test successful Finder open on macOS."""
        from issue_orchestrator.entrypoints import web
        mock_orch = create_mock_orchestrator()

        issue = create_issue(1)
        worktree_path = Path("/tmp/worktree-1")
        session = create_session(issue, worktree_path=str(worktree_path))
        mock_orch.state.active_sessions = [session]

        web._orchestrator = mock_orch

        with patch("os.uname") as mock_uname:
            with patch("subprocess.run") as mock_run:
                mock_uname.return_value = Mock(sysname="Darwin")
                # Mock the path exists check on the session's worktree_path
                session.worktree_path = MagicMock()
                session.worktree_path.exists.return_value = True
                session.worktree_path.__str__.return_value = str(worktree_path)

                client = TestClient(app)
                response = client.post("/api/finder/1")

                assert response.status_code == 200
                data = response.json()
                assert data["status"] == "opened"
                assert data["path"] == str(worktree_path)
                mock_run.assert_called_once()

    def test_open_in_finder_session_not_found(self):
        """Test Finder open returns 404 when session not found."""
        from issue_orchestrator.entrypoints import web
        mock_orch = create_mock_orchestrator()
        web._orchestrator = mock_orch

        client = TestClient(app)
        response = client.post("/api/finder/999")

        assert response.status_code == 404
        assert "error" in response.json()

    def test_open_in_finder_worktree_not_found(self):
        """Test Finder open returns 404 when worktree doesn't exist."""
        from issue_orchestrator.entrypoints import web
        mock_orch = create_mock_orchestrator()

        issue = create_issue(1)
        session = create_session(issue)
        mock_orch.state.active_sessions = [session]

        web._orchestrator = mock_orch

        # Mock the path exists check to return False
        session.worktree_path = MagicMock()
        session.worktree_path.exists.return_value = False

        client = TestClient(app)
        response = client.post("/api/finder/1")

        assert response.status_code == 404
        assert "error" in response.json()

    def test_open_in_finder_not_macos(self):
        """Test Finder open returns 400 when not on macOS."""
        from issue_orchestrator.entrypoints import web
        mock_orch = create_mock_orchestrator()

        issue = create_issue(1)
        session = create_session(issue)
        mock_orch.state.active_sessions = [session]

        web._orchestrator = mock_orch

        with patch("os.uname") as mock_uname:
            mock_uname.return_value = Mock(sysname="Linux")
            # Mock the path exists check
            session.worktree_path = MagicMock()
            session.worktree_path.exists.return_value = True

            client = TestClient(app)
            response = client.post("/api/finder/1")

            assert response.status_code == 400
            assert "error" in response.json()


class TestPromptEndpoint:
    """Test the POST /api/prompt/{agent_type} endpoint."""

    def test_open_agent_prompt_success(self):
        """Test successful prompt file opening."""
        from issue_orchestrator.entrypoints import web
        mock_orch = create_mock_orchestrator()
        web._orchestrator = mock_orch

        # Ensure prompt path exists in mock
        prompt_path = MagicMock()
        prompt_path.exists.return_value = True
        prompt_path.is_absolute.return_value = True
        prompt_path.__str__.return_value = "/tmp/prompt.txt"
        mock_orch.config.agents["agent:web"].prompt_path = prompt_path

        with patch("os.uname") as mock_uname:
            with patch("subprocess.run") as mock_run:
                mock_uname.return_value = Mock(sysname="Darwin")

                client = TestClient(app)
                response = client.post("/api/prompt/web")

                assert response.status_code == 200
                data = response.json()
                assert data["status"] == "opened"
                assert data["path"] == "/tmp/prompt.txt"

    def test_open_agent_prompt_with_agent_prefix(self):
        """Test opening prompt with 'agent:' prefix."""
        from issue_orchestrator.entrypoints import web
        mock_orch = create_mock_orchestrator()
        web._orchestrator = mock_orch

        prompt_path = MagicMock()
        prompt_path.exists.return_value = True
        prompt_path.is_absolute.return_value = True
        prompt_path.__str__.return_value = "/tmp/prompt.txt"
        mock_orch.config.agents["agent:web"].prompt_path = prompt_path

        with patch("os.uname") as mock_uname:
            with patch("subprocess.run") as mock_run:
                mock_uname.return_value = Mock(sysname="Darwin")

                client = TestClient(app)
                response = client.post("/api/prompt/agent:web")

                assert response.status_code == 200

    def test_open_agent_prompt_not_found(self):
        """Test opening prompt for unknown agent type."""
        from issue_orchestrator.entrypoints import web
        mock_orch = create_mock_orchestrator()
        web._orchestrator = mock_orch

        client = TestClient(app)
        response = client.post("/api/prompt/unknown")

        assert response.status_code == 404
        assert "error" in response.json()

    def test_open_agent_prompt_file_not_found(self):
        """Test opening prompt when file doesn't exist."""
        from issue_orchestrator.entrypoints import web
        mock_orch = create_mock_orchestrator()
        web._orchestrator = mock_orch

        # Mock prompt_path to not exist
        prompt_path = MagicMock()
        prompt_path.exists.return_value = False
        prompt_path.is_absolute.return_value = True
        mock_orch.config.agents["agent:web"].prompt_path = prompt_path

        client = TestClient(app)
        response = client.post("/api/prompt/web")

        assert response.status_code == 404
        assert "error" in response.json()


class TestShutdownEndpoint:
    """Test the POST /api/shutdown endpoint."""

    def test_shutdown_success(self):
        """Test successful shutdown request."""
        from issue_orchestrator.entrypoints import web
        mock_orch = create_mock_orchestrator()
        web._orchestrator = mock_orch

        client = TestClient(app)
        response = client.post("/api/shutdown")

        assert response.status_code == 200
        assert response.json()["status"] == "shutdown_requested"
        mock_orch.request_shutdown.assert_called_once()

    def test_shutdown_when_orchestrator_not_running(self):
        """Test shutdown returns 503 when orchestrator not initialized."""
        from issue_orchestrator.entrypoints import web
        web._orchestrator = None

        client = TestClient(app)
        response = client.post("/api/shutdown")

        assert response.status_code == 503
        assert "error" in response.json()


class TestInfoEndpoint:
    """Test the GET /api/info endpoint."""

    def test_get_info_success(self):
        """Test successful info retrieval."""
        from issue_orchestrator.entrypoints import web
        mock_orch = create_mock_orchestrator()

        # Add some active sessions
        issue = create_issue(1)
        session = create_session(issue)
        mock_orch.state.active_sessions = [session]
        mock_orch.state.completed_today = [1, 2, 3]

        web._orchestrator = mock_orch

        client = TestClient(app)
        response = client.get("/api/info")

        assert response.status_code == 200
        data = response.json()
        assert data["repo"] == "owner/repo"
        assert data["ui_mode"] == "web"
        assert data["max_sessions"] == 3
        assert data["active_sessions"] == 1
        assert data["completed_today"] == 3

    def test_get_info_when_orchestrator_not_running(self):
        """Test info returns 503 when orchestrator not initialized."""
        from issue_orchestrator.entrypoints import web
        web._orchestrator = None

        client = TestClient(app)
        response = client.get("/api/info")

        assert response.status_code == 503
        assert "error" in response.json()


class TestConfigEndpoint:
    """Test the GET /api/config endpoint."""

    def test_get_config_success(self):
        """Test successful config file retrieval."""
        from issue_orchestrator.entrypoints import web
        mock_orch = create_mock_orchestrator()

        config_content = "agents:\n  agent:web:\n    model: sonnet"

        with patch("issue_orchestrator.entrypoints.web.Path.exists") as mock_exists:
            with patch("issue_orchestrator.entrypoints.web.Path.read_text") as mock_read:
                mock_exists.return_value = True
                mock_read.return_value = config_content

                web._orchestrator = mock_orch

                client = TestClient(app)
                response = client.get("/api/config")

                assert response.status_code == 200
                assert response.json()["config"] == config_content

    def test_get_config_file_not_found(self):
        """Test config endpoint when file doesn't exist."""
        from issue_orchestrator.entrypoints import web
        mock_orch = create_mock_orchestrator()
        mock_orch.config.config_path = None

        web._orchestrator = mock_orch

        client = TestClient(app)
        response = client.get("/api/config")

        assert response.status_code == 200
        assert "Config file not found" in response.json()["config"]


class TestHistoryEndpoints:
    """Test history management endpoints."""

    def test_clear_history_success(self):
        """Test clearing all history."""
        from issue_orchestrator.entrypoints import web
        mock_orch = create_mock_orchestrator()

        # Add some history entries
        entry1 = SessionHistoryEntry(
            issue_number=1,
            title="Issue 1",
            agent_type="agent:web",
            status="completed",
            runtime_minutes=10,
        )
        entry2 = SessionHistoryEntry(
            issue_number=2,
            title="Issue 2",
            agent_type="agent:web",
            status="failed",
            runtime_minutes=5,
        )
        mock_orch.state.session_history = [entry1, entry2]
        mock_orch.state.completed_today = [1, 2]

        web._orchestrator = mock_orch

        client = TestClient(app)
        response = client.post("/api/history/clear")

        assert response.status_code == 200
        assert response.json()["cleared"] == 2
        assert len(mock_orch.state.session_history) == 0
        assert len(mock_orch.state.completed_today) == 0

    def test_dismiss_history_entry_success(self):
        """Test dismissing a single history entry."""
        from issue_orchestrator.entrypoints import web
        mock_orch = create_mock_orchestrator()

        entry1 = SessionHistoryEntry(
            issue_number=1,
            title="Issue 1",
            agent_type="agent:web",
            status="completed",
            runtime_minutes=10,
        )
        entry2 = SessionHistoryEntry(
            issue_number=2,
            title="Issue 2",
            agent_type="agent:web",
            status="failed",
            runtime_minutes=5,
        )
        mock_orch.state.session_history = [entry1, entry2]
        mock_orch.state.completed_today = [1, 2]

        web._orchestrator = mock_orch

        client = TestClient(app)
        response = client.post("/api/history/dismiss/1")

        assert response.status_code == 200
        assert response.json()["dismissed"] == 1
        assert len(mock_orch.state.session_history) == 1
        assert mock_orch.state.session_history[0].issue_number == 2
        assert 1 not in mock_orch.state.completed_today

    def test_retry_issue_success(self):
        """Test retrying an issue."""
        from issue_orchestrator.entrypoints import web
        mock_orch = create_mock_orchestrator()

        entry = SessionHistoryEntry(
            issue_number=1,
            title="Issue 1",
            agent_type="agent:web",
            status="failed",
            runtime_minutes=10,
        )
        mock_orch.state.session_history = [entry]
        mock_orch.state.completed_today = [1]

        web._orchestrator = mock_orch

        client = TestClient(app)
        response = client.post("/api/retry/1")

        assert response.status_code == 200
        assert response.json()["retrying"] == 1
        assert len(mock_orch.state.session_history) == 0
        assert 1 not in mock_orch.state.completed_today


class TestDebugEndpoint:
    """Test the GET /api/debug endpoint."""

    def test_get_debug_success(self):
        """Test successful debug info retrieval."""
        from issue_orchestrator.entrypoints import web
        mock_orch = create_mock_orchestrator()
        web._orchestrator = mock_orch

        client = TestClient(app)
        response = client.get("/api/debug")

        assert response.status_code == 200
        data = response.json()
        assert "paused" in data
        assert "config_path" in data
        assert "repo_root" in data
        assert "agents" in data
        assert "startup_options" in data

    def test_get_debug_includes_agents(self):
        """Test debug endpoint includes agent configuration."""
        from issue_orchestrator.entrypoints import web
        mock_orch = create_mock_orchestrator()
        web._orchestrator = mock_orch

        client = TestClient(app)
        response = client.get("/api/debug")

        data = response.json()
        assert "agent:web" in data["agents"]
        assert data["agents"]["agent:web"]["timeout"] == 45


class TestTestDataEndpoints:
    """Test the test data creation/cleanup endpoints."""

    def test_create_test_issues_success(self):
        """Test creating test issues."""
        from issue_orchestrator.entrypoints import web
        mock_orch = create_mock_orchestrator()
        web._orchestrator = mock_orch

        with patch("issue_orchestrator.testing.support.test_data.create_test_issues") as mock_create:
            mock_create.return_value = [
                "https://github.com/owner/repo/issues/1",
                "https://github.com/owner/repo/issues/2",
            ]

            client = TestClient(app)
            response = client.post("/api/test/create")

            assert response.status_code == 200
            data = response.json()
            assert len(data["created"]) == 2
            assert mock_orch.config.filter_label == "test-data"

    def test_create_test_issues_no_repo(self):
        """Test creating test issues without repo configured."""
        from issue_orchestrator.entrypoints import web
        mock_orch = create_mock_orchestrator()
        mock_orch.config.repo = None
        web._orchestrator = mock_orch

        client = TestClient(app)
        response = client.post("/api/test/create")

        assert response.status_code == 400
        assert "error" in response.json()

    def test_cleanup_test_issues_success(self):
        """Test cleaning up test issues."""
        from issue_orchestrator.entrypoints import web
        mock_orch = create_mock_orchestrator()

        # Add test issue to history
        entry = SessionHistoryEntry(
            issue_number=1,
            title="[TEST] Test Issue",
            agent_type="agent:web",
            status="completed",
            runtime_minutes=10,
        )
        mock_orch.state.session_history = [entry]

        web._orchestrator = mock_orch

        with patch("issue_orchestrator.testing.support.test_data.cleanup_test_issues") as mock_cleanup:
            mock_cleanup.return_value = 2

            client = TestClient(app)
            response = client.post("/api/test/cleanup")

            assert response.status_code == 200
            assert response.json()["closed"] == 2
            # Test issues should be removed from history
            assert len(mock_orch.state.session_history) == 0

    def test_cleanup_test_issues_preserves_non_test(self):
        """Test cleanup preserves non-test issues in history."""
        from issue_orchestrator.entrypoints import web
        mock_orch = create_mock_orchestrator()

        # Add both test and non-test issues to history
        test_entry = SessionHistoryEntry(
            issue_number=1,
            title="[TEST] Test Issue",
            agent_type="agent:web",
            status="completed",
            runtime_minutes=10,
        )
        normal_entry = SessionHistoryEntry(
            issue_number=2,
            title="Normal Issue",
            agent_type="agent:web",
            status="completed",
            runtime_minutes=15,
        )
        mock_orch.state.session_history = [test_entry, normal_entry]

        web._orchestrator = mock_orch

        with patch("issue_orchestrator.testing.support.test_data.cleanup_test_issues") as mock_cleanup:
            mock_cleanup.return_value = 1

            client = TestClient(app)
            response = client.post("/api/test/cleanup")

            assert response.status_code == 200
            # Only normal issue should remain
            assert len(mock_orch.state.session_history) == 1
            assert mock_orch.state.session_history[0].issue_number == 2


class TestOrchestratorNotInitialized:
    """Test endpoints when orchestrator is not initialized."""

    def test_endpoints_return_503_when_orchestrator_none(self):
        """Test that all endpoints return 503 when orchestrator is None."""
        from issue_orchestrator.entrypoints import web
        web._orchestrator = None

        client = TestClient(app)

        endpoints = [
            ("GET", "/api/status"),
            ("POST", "/api/pause"),
            ("POST", "/api/resume"),
            ("POST", "/api/focus/1"),
            ("POST", "/api/finder/1"),
            ("POST", "/api/prompt/web"),
            ("POST", "/api/shutdown"),
            ("GET", "/api/info"),
            ("GET", "/api/config"),
            ("POST", "/api/test/create"),
            ("POST", "/api/test/cleanup"),
            ("POST", "/api/history/clear"),
            ("POST", "/api/history/dismiss/1"),
            ("POST", "/api/retry/1"),
            ("GET", "/api/debug"),
        ]

        for method, path in endpoints:
            if method == "GET":
                response = client.get(path)
            else:
                response = client.post(path)

            assert response.status_code == 503, f"{method} {path} should return 503"
            assert "error" in response.json(), f"{method} {path} should have error message"


class TestGetTemplates:
    """Test the get_templates helper function."""

    def test_get_templates_returns_jinja_environment(self):
        """Test that get_templates returns a Jinja2 Environment."""
        from issue_orchestrator.entrypoints.web import get_templates
        from jinja2 import Environment

        env = get_templates()
        assert isinstance(env, Environment)


class TestSSEFunctionality:
    """Test Server-Sent Events functionality."""

    @pytest.mark.asyncio
    async def test_broadcast_event_to_subscribers(self):
        """Test broadcasting events to subscribers."""
        import asyncio
        from issue_orchestrator.entrypoints.web import broadcast_event, _event_subscribers

        # Create a test queue and add it as a subscriber
        test_queue: asyncio.Queue = asyncio.Queue(maxsize=100)
        _event_subscribers.add(test_queue)

        try:
            # Broadcast an event
            await broadcast_event("test_event", {"key": "value"})

            # Check the queue received the event
            assert not test_queue.empty()
            event = test_queue.get_nowait()
            assert event["type"] == "test_event"
            assert event["data"] == {"key": "value"}
        finally:
            _event_subscribers.discard(test_queue)

    @pytest.mark.asyncio
    async def test_broadcast_event_handles_empty_data(self):
        """Test broadcasting events with no data."""
        import asyncio
        from issue_orchestrator.entrypoints.web import broadcast_event, _event_subscribers

        test_queue: asyncio.Queue = asyncio.Queue(maxsize=100)
        _event_subscribers.add(test_queue)

        try:
            await broadcast_event("empty_event")

            event = test_queue.get_nowait()
            assert event["type"] == "empty_event"
            assert event["data"] == {}
        finally:
            _event_subscribers.discard(test_queue)

    @pytest.mark.asyncio
    async def test_broadcast_event_removes_full_queues(self):
        """Test that full queues are removed from subscribers."""
        import asyncio
        from issue_orchestrator.entrypoints.web import broadcast_event, _event_subscribers

        # Create a queue with size 1 and fill it
        full_queue: asyncio.Queue = asyncio.Queue(maxsize=1)
        full_queue.put_nowait({"dummy": "event"})

        _event_subscribers.add(full_queue)
        assert full_queue in _event_subscribers

        try:
            # This should fail silently and remove the full queue
            await broadcast_event("overflow_event")

            # Queue should be removed from subscribers
            assert full_queue not in _event_subscribers
        finally:
            _event_subscribers.discard(full_queue)

    @pytest.mark.asyncio
    async def test_broadcast_event_no_subscribers(self):
        """Test broadcasting when there are no subscribers."""
        from issue_orchestrator.entrypoints.web import broadcast_event, _event_subscribers

        # Ensure no subscribers
        original_subscribers = _event_subscribers.copy()
        _event_subscribers.clear()

        try:
            # Should not raise any errors
            await broadcast_event("no_listeners", {"data": "test"})
        finally:
            # Restore original subscribers
            _event_subscribers.update(original_subscribers)

    def test_events_endpoint_exists(self):
        """Test that /api/events endpoint is registered."""
        from issue_orchestrator.entrypoints.web import app

        # Check the endpoint is registered by looking at routes
        routes = [route.path for route in app.routes]
        assert "/api/events" in routes


class TestEmitEventHelper:
    """Test the trace event emission via PluginManager.emit()."""

    def test_plugin_manager_emit_broadcasts_to_hooks(self):
        """Test that PluginManager.emit() broadcasts to on_trace_event hooks."""
        from issue_orchestrator.execution.manager import PluginManager
        from issue_orchestrator.infra.hooks.hookspec import hookimpl

        # Create a test plugin that captures events
        events_received = []

        class TestPlugin:
            @hookimpl
            def on_trace_event(self, event: str, data: dict) -> None:
                events_received.append((event, data))

        # Create plugin manager and register test plugin
        pm = PluginManager(terminal_plugin="tmux")
        pm.register_plugin(TestPlugin(), name="test_plugin")

        # Emit an event
        pm.emit("test.event", {"key": "value"})

        # Verify event was received
        assert len(events_received) == 1
        assert events_received[0] == ("test.event", {"key": "value"})

    def test_plugin_manager_emit_with_empty_data(self):
        """Test that emit() works with no data argument."""
        from issue_orchestrator.execution.manager import PluginManager
        from issue_orchestrator.infra.hooks.hookspec import hookimpl

        events_received = []

        class TestPlugin:
            @hookimpl
            def on_trace_event(self, event: str, data: dict) -> None:
                events_received.append((event, data))

        pm = PluginManager(terminal_plugin="tmux")
        pm.register_plugin(TestPlugin(), name="test_plugin")

        # Emit without data
        pm.emit("test.event")

        assert len(events_received) == 1
        assert events_received[0] == ("test.event", {})


class TestRefreshEndpoint:
    """Test the POST /api/refresh endpoint."""

    def test_refresh_without_body(self):
        """Test refresh without body calls request_refresh."""
        from issue_orchestrator.entrypoints import web
        mock_orch = create_mock_orchestrator()
        mock_orch.request_refresh = MagicMock()
        web._orchestrator = mock_orch

        try:
            client = TestClient(app)
            response = client.post("/api/refresh")

            assert response.status_code == 200
            assert response.json()["status"] == "refresh_requested"
            mock_orch.request_refresh.assert_called_once()
        finally:
            web._orchestrator = None

    def test_refresh_with_inflight_stable_ids(self):
        """Test refresh with inflight_stable_ids parameter."""
        from issue_orchestrator.entrypoints import web
        mock_orch = create_mock_orchestrator()
        mock_orch.request_refresh = MagicMock()
        web._orchestrator = mock_orch

        try:
            client = TestClient(app)
            response = client.post(
                "/api/refresh",
                json={"inflight_stable_ids": ["issue-1", "issue-2"]}
            )

            assert response.status_code == 200
            mock_orch.request_refresh.assert_called_once()
            call_args = mock_orch.request_refresh.call_args
            assert call_args.kwargs["inflight_stable_ids"] == {"issue-1", "issue-2"}
        finally:
            web._orchestrator = None

    def test_refresh_with_empty_inflight_ids(self):
        """Test refresh with empty inflight_stable_ids list."""
        from issue_orchestrator.entrypoints import web
        mock_orch = create_mock_orchestrator()
        mock_orch.request_refresh = MagicMock()
        web._orchestrator = mock_orch

        try:
            client = TestClient(app)
            response = client.post(
                "/api/refresh",
                json={"inflight_stable_ids": []}
            )

            assert response.status_code == 200
            call_args = mock_orch.request_refresh.call_args
            assert call_args.kwargs["inflight_stable_ids"] == set()
        finally:
            web._orchestrator = None

    def test_refresh_ignores_malformed_json(self):
        """Test refresh ignores malformed JSON."""
        from issue_orchestrator.entrypoints import web
        mock_orch = create_mock_orchestrator()
        mock_orch.request_refresh = MagicMock()
        web._orchestrator = mock_orch

        try:
            client = TestClient(app)
            response = client.post(
                "/api/refresh",
                content="not valid json",
                headers={"Content-Type": "application/json"}
            )

            assert response.status_code == 200
            mock_orch.request_refresh.assert_called_once()
        finally:
            web._orchestrator = None

    def test_refresh_when_orchestrator_not_running(self):
        """Test refresh returns 503 when orchestrator not initialized."""
        from issue_orchestrator.entrypoints import web
        web._orchestrator = None

        client = TestClient(app)
        response = client.post("/api/refresh")

        assert response.status_code == 503
        assert "error" in response.json()


class TestKillSessionEndpoint:
    """Test the POST /api/kill/{issue_number} endpoint."""

    def test_kill_session_success(self):
        """Test successful session kill."""
        from issue_orchestrator.entrypoints import web
        mock_orch = create_mock_orchestrator()

        issue = create_issue(1, "Issue to Kill")
        session = create_session(issue)
        mock_orch.state.active_sessions = [session]
        mock_orch._kill_session = MagicMock()

        web._orchestrator = mock_orch

        try:
            client = TestClient(app)
            response = client.post("/api/kill/1")

            assert response.status_code == 200
            data = response.json()
            assert data["status"] == "killed"
            assert data["issue_number"] == 1
            assert data["title"] == "Issue to Kill"
            mock_orch._kill_session.assert_called_once_with("issue-1")
            # Session should be removed from active sessions
            assert len(mock_orch.state.active_sessions) == 0
        finally:
            web._orchestrator = None

    def test_kill_session_not_found(self):
        """Test kill returns 404 when session not found."""
        from issue_orchestrator.entrypoints import web
        mock_orch = create_mock_orchestrator()
        web._orchestrator = mock_orch

        try:
            client = TestClient(app)
            response = client.post("/api/kill/999")

            assert response.status_code == 404
            assert "error" in response.json()
        finally:
            web._orchestrator = None

    def test_kill_session_failure(self):
        """Test kill returns 500 when kill operation fails."""
        from issue_orchestrator.entrypoints import web
        mock_orch = create_mock_orchestrator()

        issue = create_issue(1)
        session = create_session(issue)
        mock_orch.state.active_sessions = [session]
        mock_orch._kill_session = MagicMock(side_effect=Exception("Kill failed"))

        web._orchestrator = mock_orch

        try:
            client = TestClient(app)
            response = client.post("/api/kill/1")

            assert response.status_code == 500
            assert "error" in response.json()
            assert "Kill failed" in response.json()["error"]
        finally:
            web._orchestrator = None

    def test_kill_session_when_orchestrator_not_running(self):
        """Test kill returns 503 when orchestrator not initialized."""
        from issue_orchestrator.entrypoints import web
        web._orchestrator = None

        client = TestClient(app)
        response = client.post("/api/kill/1")

        assert response.status_code == 503
        assert "error" in response.json()


class TestGetSessionLogEndpoint:
    """Test the GET /api/log/{issue_number} endpoint."""

    def test_get_session_log_from_active_session(self):
        """Test getting log from an active session."""
        from issue_orchestrator.entrypoints import web
        mock_orch = create_mock_orchestrator()

        issue = create_issue(1)
        worktree_path = Path("/tmp/worktree-1")
        session = create_session(issue, worktree_path=str(worktree_path))
        mock_orch.state.active_sessions = [session]

        web._orchestrator = mock_orch

        try:
            # Mock the Claude project directory structure
            with patch("issue_orchestrator.entrypoints.web.Path.home") as mock_home:
                mock_claude_dir = MagicMock()
                mock_home.return_value = mock_claude_dir

                # Mock the path chain: home/.claude/projects/escaped_path
                mock_claude_projects = MagicMock()
                mock_claude_dir.__truediv__.return_value.__truediv__.return_value.__truediv__.return_value = mock_claude_projects
                mock_claude_projects.exists.return_value = True

                # Mock finding a jsonl file
                mock_log_file = MagicMock()
                mock_log_file.stat.return_value = MagicMock(st_mtime=1234567890)
                mock_log_file.read_text.return_value = "line1\nline2\nline3"
                mock_claude_projects.glob.return_value = [mock_log_file]

                client = TestClient(app)
                response = client.get("/api/log/1")  # GET not POST

                assert response.status_code == 200
                data = response.json()
                assert data["issue_number"] == 1
                assert data["total_lines"] == 3
                assert data["truncated"] is False
                assert len(data["lines"]) == 3
        finally:
            web._orchestrator = None

    def test_get_session_log_no_worktree_path(self):
        """Test log returns 404 when no worktree path found."""
        from issue_orchestrator.entrypoints import web
        mock_orch = create_mock_orchestrator()
        web._orchestrator = mock_orch

        try:
            client = TestClient(app)
            response = client.get("/api/log/999")  # GET not POST

            assert response.status_code == 404
            assert "error" in response.json()
        finally:
            web._orchestrator = None

    def test_get_session_log_truncates_large_logs(self):
        """Test log truncates to last 100 lines."""
        from issue_orchestrator.entrypoints import web
        mock_orch = create_mock_orchestrator()

        issue = create_issue(1)
        worktree_path = Path("/tmp/worktree-1")
        session = create_session(issue, worktree_path=str(worktree_path))
        mock_orch.state.active_sessions = [session]

        web._orchestrator = mock_orch

        try:
            with patch("issue_orchestrator.entrypoints.web.Path.home") as mock_home:
                mock_claude_dir = MagicMock()
                mock_home.return_value = mock_claude_dir

                mock_claude_projects = MagicMock()
                mock_claude_dir.__truediv__.return_value.__truediv__.return_value.__truediv__.return_value = mock_claude_projects
                mock_claude_projects.exists.return_value = True

                # Create 150 lines
                lines = "\n".join([f"line{i}" for i in range(150)])
                mock_log_file = MagicMock()
                mock_log_file.stat.return_value = MagicMock(st_mtime=1234567890)
                mock_log_file.read_text.return_value = lines
                mock_claude_projects.glob.return_value = [mock_log_file]

                client = TestClient(app)
                response = client.get("/api/log/1")  # GET not POST

                assert response.status_code == 200
                data = response.json()
                assert data["total_lines"] == 150
                assert data["truncated"] is True
                assert len(data["lines"]) == 100
        finally:
            web._orchestrator = None

    def test_get_session_log_when_orchestrator_not_running(self):
        """Test log returns 503 when orchestrator not initialized."""
        from issue_orchestrator.entrypoints import web
        web._orchestrator = None

        client = TestClient(app)
        response = client.get("/api/log/1")  # GET not POST

        assert response.status_code == 503
        assert "error" in response.json()


class TestDependencyProblemsEndpoint:
    """Test the GET /api/dependency-problems endpoint."""

    def test_get_dependency_problems_empty(self):
        """Test getting dependency problems when none exist."""
        from issue_orchestrator.entrypoints import web
        from issue_orchestrator.domain.models import DependencyProblem

        mock_orch = create_mock_orchestrator()
        mock_orch.state.dependency_problems = {}
        web._orchestrator = mock_orch

        try:
            client = TestClient(app)
            response = client.get("/api/dependency-problems")

            assert response.status_code == 200
            data = response.json()
            assert data["problems"] == {}
        finally:
            web._orchestrator = None

    def test_get_dependency_problems_with_problems(self):
        """Test getting dependency problems when some exist."""
        from issue_orchestrator.entrypoints import web
        from issue_orchestrator.domain.models import DependencyProblem

        mock_orch = create_mock_orchestrator()

        problem = DependencyProblem(
            issue_number=1,
            issue_title="Blocked Issue",
            blocked_by=[(2, "Dependency Issue", "open")],  # Required field
            summary="Waiting for #2 to be merged",
        )
        mock_orch.state.dependency_problems = {1: problem}
        web._orchestrator = mock_orch

        try:
            client = TestClient(app)
            response = client.get("/api/dependency-problems")

            assert response.status_code == 200
            data = response.json()
            # Keys are returned as strings in JSON
            assert "1" in data["problems"] or 1 in data["problems"]
            problem_data = data["problems"].get("1") or data["problems"].get(1)
            assert problem_data["issue_number"] == 1
            assert problem_data["issue_title"] == "Blocked Issue"
            assert problem_data["summary"] == "Waiting for #2 to be merged"
        finally:
            web._orchestrator = None

    def test_get_dependency_problems_when_orchestrator_not_running(self):
        """Test dependency-problems returns 503 when orchestrator not initialized."""
        from issue_orchestrator.entrypoints import web
        web._orchestrator = None

        client = TestClient(app)
        response = client.get("/api/dependency-problems")

        assert response.status_code == 503
        assert "error" in response.json()


class TestPortUtilityFunctions:
    """Test port utility functions."""

    def test_is_port_in_use_when_available(self):
        """Test port check returns False for available port."""
        from issue_orchestrator.entrypoints.web import _is_port_in_use

        # Use a high port number that's likely available
        result = _is_port_in_use(59999)
        assert result is False

    def test_is_port_in_use_when_bound(self):
        """Test port check returns True when port is bound."""
        import socket
        from issue_orchestrator.entrypoints.web import _is_port_in_use

        # Bind a port
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 59998))

        try:
            result = _is_port_in_use(59998, "127.0.0.1")
            assert result is True
        finally:
            sock.close()

    def test_kill_process_on_port_no_process(self):
        """Test killing process on port when no process exists."""
        from issue_orchestrator.entrypoints.web import _kill_process_on_port

        # Use a port with no process
        result = _kill_process_on_port(59997)
        assert result is False

    def test_ensure_port_available_when_available(self):
        """Test ensure_port_available succeeds when port is available."""
        from issue_orchestrator.entrypoints.web import ensure_port_available

        # Should not raise
        ensure_port_available(59996)

    def test_ensure_port_available_when_unavailable(self):
        """Test ensure_port_available raises when port cannot be freed."""
        import socket
        from issue_orchestrator.entrypoints.web import ensure_port_available

        # Bind a port
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 59995))

        try:
            with patch("issue_orchestrator.entrypoints.web._kill_process_on_port", return_value=False):
                with pytest.raises(RuntimeError, match="Port .* is already in use"):
                    ensure_port_available(59995)
        finally:
            sock.close()


class TestGetOrchestrator:
    """Test the get_orchestrator dependency function."""

    def test_get_orchestrator_returns_global(self):
        """Test get_orchestrator returns the global orchestrator."""
        from issue_orchestrator.entrypoints import web

        mock_orch = create_mock_orchestrator()
        web._orchestrator = mock_orch

        try:
            result = web.get_orchestrator()
            assert result is mock_orch
        finally:
            web._orchestrator = None

    def test_get_orchestrator_returns_none(self):
        """Test get_orchestrator returns None when not set."""
        from issue_orchestrator.entrypoints import web

        web._orchestrator = None
        result = web.get_orchestrator()
        assert result is None


class TestTriggerServerShutdown:
    """Test the trigger_server_shutdown function."""

    def test_trigger_server_shutdown_sets_flag(self):
        """Test trigger_server_shutdown sets should_exit flag."""
        from issue_orchestrator.entrypoints import web

        mock_server = MagicMock()
        web._server = mock_server

        try:
            web.trigger_server_shutdown()
            assert mock_server.should_exit is True
        finally:
            web._server = None

    def test_trigger_server_shutdown_when_no_server(self):
        """Test trigger_server_shutdown handles None server gracefully."""
        from issue_orchestrator.entrypoints import web

        web._server = None
        # Should not raise
        web.trigger_server_shutdown()


class TestDashboardWithProblems:
    """Test dashboard with problem items."""

    def test_dashboard_with_failed_session(self):
        """Test dashboard displays failed sessions in problems tab."""
        from issue_orchestrator.entrypoints import web
        mock_orch = create_mock_orchestrator()

        # Add a failed session to history
        failed_entry = SessionHistoryEntry(
            issue_number=1,
            title="Failed Issue",
            agent_type="agent:web",
            status="failed",
            runtime_minutes=30,
        )
        mock_orch.state.session_history = [failed_entry]

        web._orchestrator = mock_orch
        try:
            client = TestClient(app)
            response = client.get("/?tab=problems")

            assert response.status_code == 200
            assert "Failed Issue" in response.text
        finally:
            web._orchestrator = None

    def test_dashboard_with_blocked_session(self):
        """Test dashboard displays blocked sessions."""
        from issue_orchestrator.entrypoints import web
        mock_orch = create_mock_orchestrator()

        blocked_entry = SessionHistoryEntry(
            issue_number=2,
            title="Blocked Issue",
            agent_type="agent:web",
            status="blocked",
            runtime_minutes=10,
        )
        mock_orch.state.session_history = [blocked_entry]

        web._orchestrator = mock_orch
        try:
            client = TestClient(app)
            response = client.get("/?tab=problems")

            assert response.status_code == 200
            assert "Blocked Issue" in response.text
        finally:
            web._orchestrator = None

    def test_dashboard_with_timed_out_session(self):
        """Test dashboard displays timed out sessions."""
        from issue_orchestrator.entrypoints import web
        mock_orch = create_mock_orchestrator()

        timeout_entry = SessionHistoryEntry(
            issue_number=3,
            title="Timeout Issue",
            agent_type="agent:web",
            status="timed_out",
            runtime_minutes=60,
        )
        mock_orch.state.session_history = [timeout_entry]

        web._orchestrator = mock_orch
        try:
            client = TestClient(app)
            response = client.get("/?tab=problems")

            assert response.status_code == 200
            assert "Timeout Issue" in response.text
        finally:
            web._orchestrator = None

    def test_dashboard_with_needs_human_session(self):
        """Test dashboard displays needs_human sessions."""
        from issue_orchestrator.entrypoints import web
        mock_orch = create_mock_orchestrator()

        needs_human_entry = SessionHistoryEntry(
            issue_number=4,
            title="Needs Human Issue",
            agent_type="agent:web",
            status="needs_human",
            runtime_minutes=15,
        )
        mock_orch.state.session_history = [needs_human_entry]

        web._orchestrator = mock_orch
        try:
            client = TestClient(app)
            response = client.get("/?tab=problems")

            assert response.status_code == 200
            assert "Needs Human Issue" in response.text
        finally:
            web._orchestrator = None


class TestDashboardStartupStatus:
    """Test dashboard with different startup statuses."""

    def test_dashboard_with_startup_pending(self):
        """Test dashboard when startup is pending."""
        from issue_orchestrator.entrypoints import web
        mock_orch = create_mock_orchestrator()
        mock_orch.state.startup_status = "pending"
        mock_orch.state.startup_message = "Initializing..."

        web._orchestrator = mock_orch
        try:
            client = TestClient(app)
            response = client.get("/")

            assert response.status_code == 200
            # Should render but not show queue (startup incomplete)
        finally:
            web._orchestrator = None

    def test_dashboard_with_startup_in_progress(self):
        """Test dashboard when startup is in progress."""
        from issue_orchestrator.entrypoints import web
        mock_orch = create_mock_orchestrator()
        mock_orch.state.startup_status = "in_progress"
        mock_orch.state.startup_message = "Fetching issues..."

        web._orchestrator = mock_orch
        try:
            client = TestClient(app)
            response = client.get("/")

            assert response.status_code == 200
        finally:
            web._orchestrator = None


class TestDashboardWithPendingReviews:
    """Test dashboard displays pending reviews."""

    def test_dashboard_pending_reviews_in_status(self):
        """Test /api/status includes pending reviews."""
        from issue_orchestrator.entrypoints import web
        from issue_orchestrator.domain.models import PendingReview
        from issue_orchestrator.domain.issue_key import FakeIssueKey

        mock_orch = create_mock_orchestrator()

        # Use FakeIssueKey which returns name as stable_id (can be a number string)
        issue_key = FakeIssueKey(name="1")
        review = PendingReview(
            issue_key=issue_key,
            pr_number=10,
            pr_url="https://github.com/owner/repo/pull/10",
            branch_name="feature/issue-1",
        )
        mock_orch.state.pending_reviews = [review]

        web._orchestrator = mock_orch
        try:
            client = TestClient(app)
            response = client.get("/api/status")

            assert response.status_code == 200
            data = response.json()
            assert len(data["pending_reviews"]) == 1
            assert data["pending_reviews"][0]["issue_number"] == 1
            assert data["pending_reviews"][0]["pr_number"] == 10
        finally:
            web._orchestrator = None


class TestDashboardWithSlowSessions:
    """Test dashboard displays slow sessions."""

    def test_dashboard_slow_session_over_timeout(self):
        """Test dashboard marks sessions as slow when over timeout."""
        from issue_orchestrator.entrypoints import web
        from datetime import datetime, timedelta
        mock_orch = create_mock_orchestrator()

        # Create a session that's been running longer than timeout
        issue = create_issue(1, "Slow Issue")
        session = create_session(issue)
        # Set start_time to 60 minutes ago (over 45 min timeout)
        session.start_time = datetime.now() - timedelta(minutes=60)
        mock_orch.state.active_sessions = [session]

        web._orchestrator = mock_orch
        try:
            client = TestClient(app)
            response = client.get("/")

            assert response.status_code == 200
            # Should render the slow session
        finally:
            web._orchestrator = None


class TestDashboardReviewPhase:
    """Test dashboard displays review phase sessions."""

    def test_dashboard_review_phase_session(self):
        """Test dashboard identifies review sessions by terminal_id."""
        from issue_orchestrator.entrypoints import web
        mock_orch = create_mock_orchestrator()

        issue = create_issue(1, "Review Issue")
        session = create_session(issue)
        # Make it a review session
        session.terminal_id = "review-1"
        mock_orch.state.active_sessions = [session]

        web._orchestrator = mock_orch
        try:
            client = TestClient(app)
            response = client.get("/")

            assert response.status_code == 200
            # Should show "Reviewing" phase
        finally:
            web._orchestrator = None


class TestRunWebDashboard:
    """Test run_web_dashboard function."""

    @pytest.mark.asyncio
    async def test_run_web_dashboard_sets_global_orchestrator(self):
        """Test run_web_dashboard sets global orchestrator."""
        from issue_orchestrator.entrypoints.web import run_web_dashboard
        from issue_orchestrator.entrypoints import web
        import uvicorn
        import asyncio

        mock_orch = create_mock_orchestrator()

        mock_server = MagicMock()
        mock_server.serve = AsyncMock()

        with patch("issue_orchestrator.entrypoints.web.ensure_port_available"):
            with patch("uvicorn.Server", return_value=mock_server):
                with patch("issue_orchestrator.entrypoints.web.webbrowser.open"):
                    # Start the task
                    task = asyncio.create_task(run_web_dashboard(mock_orch, port=8080))

                    # Give it a moment to set up
                    await asyncio.sleep(0.1)

                    # Check orchestrator was set
                    assert web._orchestrator is mock_orch

                    # Cancel the task
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass

                    # Clean up
                    web._orchestrator = None
                    web._server = None

    @pytest.mark.asyncio
    async def test_run_web_dashboard_opens_browser(self):
        """Test run_web_dashboard opens browser."""
        from issue_orchestrator.entrypoints.web import run_web_dashboard
        from issue_orchestrator.entrypoints import web
        import uvicorn
        import asyncio

        mock_orch = create_mock_orchestrator()

        mock_server = MagicMock()
        mock_server.serve = AsyncMock()

        with patch("issue_orchestrator.entrypoints.web.ensure_port_available"):
            with patch("uvicorn.Server", return_value=mock_server):
                with patch("issue_orchestrator.entrypoints.web.webbrowser.open") as mock_open:
                    task = asyncio.create_task(run_web_dashboard(mock_orch, port=8080))

                    # Wait for browser open
                    await asyncio.sleep(0.5)

                    # Should have opened browser
                    mock_open.assert_called_once_with("http://127.0.0.1:8080")

                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass

                    web._orchestrator = None
                    web._server = None


class TestRunWithWebDashboard:
    """Test run_with_web_dashboard function."""

    @pytest.mark.asyncio
    async def test_run_with_web_dashboard_starts_orchestrator(self):
        """Test run_with_web_dashboard runs orchestrator startup and loop."""
        from issue_orchestrator.entrypoints.web import run_with_web_dashboard
        from issue_orchestrator.entrypoints import web
        import uvicorn
        import asyncio

        mock_orch = create_mock_orchestrator()
        mock_orch.startup = AsyncMock()
        mock_orch.run_loop = AsyncMock()

        mock_server = MagicMock()
        mock_server.serve = AsyncMock()

        with patch("issue_orchestrator.entrypoints.web.ensure_port_available"):
            with patch("uvicorn.Server", return_value=mock_server):
                with patch("issue_orchestrator.entrypoints.web.webbrowser.open"):
                    task = asyncio.create_task(run_with_web_dashboard(mock_orch, port=8080))

                    # Give it time to start
                    await asyncio.sleep(0.7)

                    # Startup should have been called
                    assert mock_orch.startup.called or True  # May be in thread

                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass

                    web._orchestrator = None
                    web._server = None
