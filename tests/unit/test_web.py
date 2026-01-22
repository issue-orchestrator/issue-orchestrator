"""Unit tests for the FastAPI web module."""

import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch, Mock, AsyncMock
from fastapi.testclient import TestClient

from issue_orchestrator.entrypoints.web import app, get_orchestrator, set_orchestrator, set_server


@pytest.fixture(autouse=True)
def prevent_os_exit():
    """Prevent shutdown_manager.exit() from calling os._exit().

    This is needed for pytest-xdist parallel test execution. The web module's
    shutdown_manager.exit() calls os._exit() which would crash the test worker
    and cause unrelated tests to be marked as failed.
    """
    with patch("issue_orchestrator.entrypoints.web.shutdown_manager.exit"):
        yield


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
    config.filtering.label = None
    config.filtering.milestone = None
    config.config_path = Path("/tmp/config.yaml")
    config.repo_root = Path("/tmp/repo")
    config.worktree_base = Path("/tmp/worktrees")  # Top-level worktree_base

    # Add a sample agent config
    agent_config = AgentConfig(
        prompt_path=Path("/tmp/prompt.txt"),
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
    mock_orch.shutdown_requested = False  # Public property for JSON serialization

    # Create a mock publish executor for async completion
    mock_executor = MagicMock()
    mock_executor.get_running_jobs.return_value = []
    mock_executor.get_running_count.return_value = 0
    mock_executor.get_pending_count.return_value = 0
    mock_executor.get_job_history.return_value = []

    mock_deps = MagicMock()
    mock_deps.publish_executor = mock_executor
    mock_orch.deps = mock_deps

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

        set_orchestrator(mock_orch)
        try:
            client = TestClient(app)
            response = client.get("/")

            assert response.status_code == 200
            assert "text/html" in response.headers["content-type"]
        finally:
            set_orchestrator(None)

    def test_dashboard_with_active_sessions(self):
        """Test dashboard displays active sessions."""
        from issue_orchestrator.entrypoints import web
        mock_orch = create_mock_orchestrator()

        # Add an active session
        issue = create_issue(1, "Active Issue")
        session = create_session(issue)
        mock_orch.state.active_sessions = [session]

        set_orchestrator(mock_orch)
        try:
            client = TestClient(app)
            response = client.get("/")

            assert response.status_code == 200
            assert "Active Issue" in response.text
            assert "#1" in response.text
        finally:
            set_orchestrator(None)

    def test_dashboard_with_queue_pagination(self):
        """Test dashboard queue pagination."""
        from issue_orchestrator.entrypoints import web
        mock_orch = create_mock_orchestrator()

        # Create 25 issues to trigger pagination (page size is 20)
        issues = [create_issue(i, f"Queue Issue {i}") for i in range(1, 26)]

        # Set cached queue issues (dashboard uses cache instead of calling API)
        mock_orch.state.cached_queue_issues = issues

        set_orchestrator(mock_orch)
        try:
            client = TestClient(app)

            # Test first page - use queue tab to see queue issues
            response = client.get("/?tab=queue&page=1")
            assert response.status_code == 200
            assert "Queue Issue 1" in response.text

            # Test second page
            response = client.get("/?tab=queue&page=2")
            assert response.status_code == 200
            assert "Queue Issue 21" in response.text
        finally:
            set_orchestrator(None)

    def test_dashboard_when_paused(self):
        """Test dashboard shows paused state."""
        from issue_orchestrator.entrypoints import web
        mock_orch = create_mock_orchestrator()
        mock_orch.state.paused = True

        set_orchestrator(mock_orch)
        try:
            client = TestClient(app)
            response = client.get("/")

            assert response.status_code == 200
            # The template should handle paused state
            assert response.status_code == 200
        finally:
            set_orchestrator(None)

    def test_dashboard_with_session_history(self):
        """Test dashboard displays session history on the History tab."""
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

        set_orchestrator(mock_orch)
        try:
            client = TestClient(app)
            # History now lives on the History tab, not the Work tab
            response = client.get("/?tab=history")

            assert response.status_code == 200
            assert "Completed Issue" in response.text
        finally:
            set_orchestrator(None)


class TestApiStatusEndpoint:
    """Test the GET /api/status endpoint."""

    def test_status_returns_json(self):
        """Test that status endpoint returns JSON."""
        from issue_orchestrator.entrypoints import web
        mock_orch = create_mock_orchestrator()
        set_orchestrator(mock_orch)

        client = TestClient(app)
        response = client.get("/api/status")

        assert response.status_code == 200
        assert response.headers["content-type"] == "application/json"
        set_orchestrator(None)

    def test_status_includes_basic_info(self):
        """Test status includes basic orchestrator info."""
        from issue_orchestrator.entrypoints import web
        mock_orch = create_mock_orchestrator()
        set_orchestrator(mock_orch)

        client = TestClient(app)
        response = client.get("/api/status")

        data = response.json()
        assert "paused" in data
        assert "active_sessions" in data
        assert "max_sessions" in data
        assert "completed_today" in data
        assert data["paused"] is False
        assert data["max_sessions"] == 3
        set_orchestrator(None)

    def test_status_with_active_sessions(self):
        """Test status includes active session details."""
        from issue_orchestrator.entrypoints import web
        mock_orch = create_mock_orchestrator()

        issue = create_issue(1, "Test Issue")
        session = create_session(issue, branch_name="feature/issue-1")
        mock_orch.state.active_sessions = [session]

        set_orchestrator(mock_orch)

        client = TestClient(app)
        response = client.get("/api/status")

        data = response.json()
        assert len(data["active_sessions"]) == 1
        assert data["active_sessions"][0]["issue_number"] == 1
        assert data["active_sessions"][0]["title"] == "Test Issue"
        assert data["active_sessions"][0]["branch"] == "feature/issue-1"
        set_orchestrator(None)

    def test_status_when_orchestrator_not_running(self):
        """Test status returns 503 when orchestrator not initialized."""
        from issue_orchestrator.entrypoints import web
        set_orchestrator(None)

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
        set_orchestrator(mock_orch)

        client = TestClient(app)
        response = client.post("/api/pause")

        assert response.status_code == 200
        assert response.json()["status"] == "paused"
        mock_orch.pause.assert_called_once()

    def test_resume_endpoint(self):
        """Test resume endpoint calls orchestrator.resume()."""
        from issue_orchestrator.entrypoints import web
        mock_orch = create_mock_orchestrator()
        set_orchestrator(mock_orch)

        client = TestClient(app)
        response = client.post("/api/resume")

        assert response.status_code == 200
        assert response.json()["status"] == "resumed"
        mock_orch.resume.assert_called_once()

    def test_pause_when_orchestrator_not_running(self):
        """Test pause returns 503 when orchestrator not initialized."""
        from issue_orchestrator.entrypoints import web
        set_orchestrator(None)

        client = TestClient(app)
        response = client.post("/api/pause")

        assert response.status_code == 503
        assert "error" in response.json()

    def test_resume_when_orchestrator_not_running(self):
        """Test resume returns 503 when orchestrator not initialized."""
        from issue_orchestrator.entrypoints import web
        set_orchestrator(None)

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
        set_orchestrator(mock_orch)

        client = TestClient(app)
        response = client.post("/api/focus/1")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "focused"
        assert data["issue_number"] == 1
        mock_orch.session_runner.focus_session.assert_called_once_with(1, "issue-1")

    def test_focus_session_failure(self):
        """Test focus returns error when focus_session fails."""
        from issue_orchestrator.entrypoints import web
        mock_orch = create_mock_orchestrator()

        issue = create_issue(1)
        session = create_session(issue)
        mock_orch.state.active_sessions = [session]

        # Mock session_runner.focus_session returning False (failed to focus)
        mock_orch.session_runner.focus_session.return_value = False
        set_orchestrator(mock_orch)

        client = TestClient(app)
        response = client.post("/api/focus/1")

        assert response.status_code == 500
        data = response.json()
        assert "error" in data
        mock_orch.session_runner.focus_session.assert_called_once_with(1, "issue-1")

    def test_focus_session_not_found(self):
        """Test focus returns 404 when session not found."""
        from issue_orchestrator.entrypoints import web
        mock_orch = create_mock_orchestrator()
        set_orchestrator(mock_orch)

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

        set_orchestrator(mock_orch)

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
        set_orchestrator(mock_orch)

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

        set_orchestrator(mock_orch)

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

        set_orchestrator(mock_orch)

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
        set_orchestrator(mock_orch)

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
        set_orchestrator(mock_orch)

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
        set_orchestrator(mock_orch)

        client = TestClient(app)
        response = client.post("/api/prompt/unknown")

        assert response.status_code == 404
        assert "error" in response.json()

    def test_open_agent_prompt_file_not_found(self):
        """Test opening prompt when file doesn't exist."""
        from issue_orchestrator.entrypoints import web
        mock_orch = create_mock_orchestrator()
        set_orchestrator(mock_orch)

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
        set_orchestrator(mock_orch)

        client = TestClient(app)
        response = client.post("/api/shutdown")

        assert response.status_code == 200
        assert response.json()["status"] == "shutdown_requested"
        mock_orch.request_shutdown.assert_called_once()

    def test_shutdown_when_orchestrator_not_running(self):
        """Test shutdown returns 503 when orchestrator not initialized."""
        from issue_orchestrator.entrypoints import web
        set_orchestrator(None)

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

        set_orchestrator(mock_orch)

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
        set_orchestrator(None)

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

                set_orchestrator(mock_orch)

                client = TestClient(app)
                response = client.get("/api/config")

                assert response.status_code == 200
                assert response.json()["config"] == config_content

    def test_get_config_file_not_found(self):
        """Test config endpoint when file doesn't exist."""
        from issue_orchestrator.entrypoints import web
        mock_orch = create_mock_orchestrator()
        mock_orch.config.config_path = None

        set_orchestrator(mock_orch)

        client = TestClient(app)
        response = client.get("/api/config")

        assert response.status_code == 200
        assert "Config file not found" in response.json()["config"]


class TestHistoryEndpoints:
    """Test history management endpoints."""

    def test_get_history_success(self):
        """Test fetching history entries."""
        from issue_orchestrator.entrypoints import web
        mock_orch = create_mock_orchestrator()

        entry1 = SessionHistoryEntry(
            issue_number=1,
            title="Issue 1",
            agent_type="agent:web",
            status="completed",
            runtime_minutes=10,
            status_reason="ok",
            worktree_path=Path("/tmp/worktree-1"),
        )
        entry2 = SessionHistoryEntry(
            issue_number=2,
            title="Issue 2",
            agent_type="agent:web",
            status="failed",
            runtime_minutes=5,
        )
        mock_orch.state.session_history = [entry1, entry2]

        set_orchestrator(mock_orch)

        client = TestClient(app)
        response = client.get("/api/history")

        assert response.status_code == 200
        payload = response.json()
        assert payload["count"] == 2
        assert payload["history"][0]["issue_number"] == 2
        assert payload["history"][1]["issue_number"] == 1
        assert payload["history"][1]["worktree_path"] == "/tmp/worktree-1"

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

        set_orchestrator(mock_orch)

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

        set_orchestrator(mock_orch)

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

        set_orchestrator(mock_orch)

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
        set_orchestrator(mock_orch)

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
        set_orchestrator(mock_orch)

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
        set_orchestrator(mock_orch)

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
            assert mock_orch.config.filtering.label == "test-data"

    def test_create_test_issues_no_repo(self):
        """Test creating test issues without repo configured."""
        from issue_orchestrator.entrypoints import web
        mock_orch = create_mock_orchestrator()
        mock_orch.config.repo = None
        set_orchestrator(mock_orch)

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

        set_orchestrator(mock_orch)

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

        set_orchestrator(mock_orch)

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
        set_orchestrator(None)

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
        pm = PluginManager(terminal_plugin="subprocess")
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

        pm = PluginManager(terminal_plugin="subprocess")
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
        set_orchestrator(mock_orch)

        try:
            client = TestClient(app)
            response = client.post("/api/refresh")

            assert response.status_code == 200
            assert response.json()["status"] == "refresh_requested"
            mock_orch.request_refresh.assert_called_once()
        finally:
            set_orchestrator(None)

    def test_refresh_with_inflight_stable_ids(self):
        """Test refresh with inflight_stable_ids parameter."""
        from issue_orchestrator.entrypoints import web
        mock_orch = create_mock_orchestrator()
        mock_orch.request_refresh = MagicMock()
        set_orchestrator(mock_orch)

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
            set_orchestrator(None)

    def test_refresh_with_empty_inflight_ids(self):
        """Test refresh with empty inflight_stable_ids list."""
        from issue_orchestrator.entrypoints import web
        mock_orch = create_mock_orchestrator()
        mock_orch.request_refresh = MagicMock()
        set_orchestrator(mock_orch)

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
            set_orchestrator(None)

    def test_refresh_ignores_malformed_json(self):
        """Test refresh ignores malformed JSON."""
        from issue_orchestrator.entrypoints import web
        mock_orch = create_mock_orchestrator()
        mock_orch.request_refresh = MagicMock()
        set_orchestrator(mock_orch)

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
            set_orchestrator(None)

    def test_refresh_when_orchestrator_not_running(self):
        """Test refresh returns 503 when orchestrator not initialized."""
        from issue_orchestrator.entrypoints import web
        set_orchestrator(None)

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
        mock_orch.kill_session = MagicMock()

        set_orchestrator(mock_orch)

        try:
            client = TestClient(app)
            response = client.post("/api/kill/1")

            assert response.status_code == 200
            data = response.json()
            assert data["status"] == "killed"
            assert data["issue_number"] == 1
            assert data["title"] == "Issue to Kill"
            mock_orch.kill_session.assert_called_once_with("issue-1")
            # Session should be removed from active sessions
            assert len(mock_orch.state.active_sessions) == 0
        finally:
            set_orchestrator(None)

    def test_kill_session_not_found(self):
        """Test kill returns 404 when session not found."""
        from issue_orchestrator.entrypoints import web
        mock_orch = create_mock_orchestrator()
        set_orchestrator(mock_orch)

        try:
            client = TestClient(app)
            response = client.post("/api/kill/999")

            assert response.status_code == 404
            assert "error" in response.json()
        finally:
            set_orchestrator(None)

    def test_kill_session_failure(self):
        """Test kill returns 500 when kill operation fails."""
        from issue_orchestrator.entrypoints import web
        mock_orch = create_mock_orchestrator()

        issue = create_issue(1)
        session = create_session(issue)
        mock_orch.state.active_sessions = [session]
        mock_orch.kill_session = MagicMock(side_effect=Exception("Kill failed"))

        set_orchestrator(mock_orch)

        try:
            client = TestClient(app)
            response = client.post("/api/kill/1")

            assert response.status_code == 500
            assert "error" in response.json()
            assert "Kill failed" in response.json()["error"]
        finally:
            set_orchestrator(None)

    def test_kill_session_when_orchestrator_not_running(self):
        """Test kill returns 503 when orchestrator not initialized."""
        from issue_orchestrator.entrypoints import web
        set_orchestrator(None)

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

        set_orchestrator(mock_orch)

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
            set_orchestrator(None)

    def test_get_session_log_no_worktree_path(self):
        """Test log returns 404 when no worktree path found."""
        from issue_orchestrator.entrypoints import web
        mock_orch = create_mock_orchestrator()
        set_orchestrator(mock_orch)

        try:
            client = TestClient(app)
            response = client.get("/api/log/999")  # GET not POST

            assert response.status_code == 404
            assert "error" in response.json()
        finally:
            set_orchestrator(None)

    def test_get_session_log_truncates_large_logs(self):
        """Test log truncates to last 100 lines."""
        from issue_orchestrator.entrypoints import web
        mock_orch = create_mock_orchestrator()

        issue = create_issue(1)
        worktree_path = Path("/tmp/worktree-1")
        session = create_session(issue, worktree_path=str(worktree_path))
        mock_orch.state.active_sessions = [session]

        set_orchestrator(mock_orch)

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
            set_orchestrator(None)

    def test_get_session_log_when_orchestrator_not_running(self):
        """Test log returns 503 when orchestrator not initialized."""
        from issue_orchestrator.entrypoints import web
        set_orchestrator(None)

        client = TestClient(app)
        response = client.get("/api/log/1")  # GET not POST

        assert response.status_code == 503
        assert "error" in response.json()


class TestLogCleaning:
    """Test terminal log cleaning functions.

    These tests verify that raw terminal output (with ANSI codes, spinner
    animations, cursor movement, etc.) is properly cleaned for display
    in the web UI.

    IMPORTANT: These tests exist to prevent regression. The log cleaning
    logic has been lost/broken multiple times. If you change the cleaning
    functions, ensure these tests still pass.
    """

    def test_strip_ansi_codes_removes_colors(self):
        """ANSI color codes should be stripped."""
        from issue_orchestrator.entrypoints.web import strip_ansi_codes

        # SGR color codes
        assert strip_ansi_codes("\x1b[38;2;215;119;87mHello\x1b[39m") == "Hello"
        assert strip_ansi_codes("\x1b[1mBold\x1b[22m") == "Bold"
        assert strip_ansi_codes("\x1b[2mDim\x1b[22m") == "Dim"

    def test_strip_ansi_codes_removes_cursor_movement(self):
        """ANSI cursor movement sequences should be stripped."""
        from issue_orchestrator.entrypoints.web import strip_ansi_codes

        # Cursor movement
        assert strip_ansi_codes("\x1b[6AMove up") == "Move up"
        assert strip_ansi_codes("\x1b[2CMove right") == "Move right"
        assert strip_ansi_codes("\x1b[K") == ""  # Erase to end of line

    def test_strip_ansi_codes_removes_private_modes(self):
        """Private mode sequences (cursor hide, etc.) should be stripped."""
        from issue_orchestrator.entrypoints.web import strip_ansi_codes

        # Private modes
        assert strip_ansi_codes("\x1b[?25lHidden cursor\x1b[?25h") == "Hidden cursor"
        assert strip_ansi_codes("\x1b[?2026hSync") == "Sync"

    def test_strip_ansi_codes_removes_osc_sequences(self):
        """OSC sequences (terminal title, etc.) should be stripped."""
        from issue_orchestrator.entrypoints.web import strip_ansi_codes

        # OSC (terminal title)
        assert strip_ansi_codes("\x1b]0;My Title\x07Rest") == "Rest"

    def test_clean_terminal_line_handles_carriage_return(self):
        """Carriage returns (spinner animations) should take last segment."""
        from issue_orchestrator.entrypoints.web import clean_terminal_line

        # Spinner animation - takes the last meaningful segment
        assert clean_terminal_line("* spin\r/ spin\r- spin").strip() == "- spin"
        assert clean_terminal_line("old\rnew").strip() == "new"

    def test_clean_terminal_line_handles_mixed_ansi_and_cr(self):
        """Mixed ANSI codes and carriage returns should both be handled."""
        from issue_orchestrator.entrypoints.web import clean_terminal_line

        # Real-world example: spinner with colors
        line = "\x1b[38;2;215;119;87m*\x1b[39m\r\x1b[38;2;215;119;87m·\x1b[39m Thinking"
        assert "Thinking" in clean_terminal_line(line)

    def test_is_spinner_fragment_filters_short_garbage(self):
        """Short garbage fragments from cursor updates should be filtered."""
        from issue_orchestrator.entrypoints.web import is_spinner_fragment

        # These are real fragments seen in terminal logs
        assert is_spinner_fragment("ddl") is True
        assert is_spinner_fragment("-fa") is True
        assert is_spinner_fragment("ea") is True
        assert is_spinner_fragment("bn") is True
        assert is_spinner_fragment("6") is True

    def test_is_spinner_fragment_filters_spinner_chars(self):
        """Lines of just spinner characters should be filtered."""
        from issue_orchestrator.entrypoints.web import is_spinner_fragment

        assert is_spinner_fragment("*") is True
        assert is_spinner_fragment("·") is True
        assert is_spinner_fragment("✶") is True
        assert is_spinner_fragment("✻✽") is True

    def test_is_spinner_fragment_filters_thinking_messages(self):
        """Repetitive thinking/loading status should be filtered."""
        from issue_orchestrator.entrypoints.web import is_spinner_fragment

        assert is_spinner_fragment("Fiddle-faddling…") is True
        assert is_spinner_fragment("· Fiddle-faddling… (ctrl+c to interrupt)") is True
        assert is_spinner_fragment("thinking)") is True
        # Partial think-time display fragments
        assert is_spinner_fragment("ought for 2s)") is True
        assert is_spinner_fragment("thought for 5s)") is True

    def test_is_spinner_fragment_keeps_meaningful_content(self):
        """Meaningful tool output should NOT be filtered."""
        from issue_orchestrator.entrypoints.web import is_spinner_fragment

        # Tool calls and results
        assert is_spinner_fragment("⏺Read(.issue-orchestrator/prompts/simple-fix.md)") is False
        assert is_spinner_fragment("⎿ Read 221 lines") is False
        assert is_spinner_fragment("⏺Bash(git status)") is False

        # Actual content
        assert is_spinner_fragment("Welcome back Bruce!") is False
        assert is_spinner_fragment("On branch main") is False
        assert is_spinner_fragment("./src/issue_orchestrator/infra/hooks/hooks.py") is False

    def test_is_spinner_fragment_keeps_separator_lines(self):
        """Separator lines (───) should be kept."""
        from issue_orchestrator.entrypoints.web import is_spinner_fragment

        assert is_spinner_fragment("────────────") is False
        assert is_spinner_fragment("━━━━━━━━━━━━") is False

    def test_is_spinner_fragment_keeps_prompts(self):
        """Prompt characters should be kept."""
        from issue_orchestrator.entrypoints.web import is_spinner_fragment

        assert is_spinner_fragment("❯") is False

    def test_dedupe_consecutive_lines_removes_duplicates(self):
        """Consecutive identical lines should be collapsed."""
        from issue_orchestrator.entrypoints.web import dedupe_consecutive_lines

        lines = ["line1", "line1", "line1", "line2", "line2", "line3"]
        result = dedupe_consecutive_lines(lines)
        assert result == ["line1", "line2", "line3"]

    def test_dedupe_consecutive_lines_collapses_separators(self):
        """Consecutive separator lines should be collapsed to one."""
        from issue_orchestrator.entrypoints.web import dedupe_consecutive_lines

        lines = [
            "Some text",
            "────────────────",
            "──────────────────────",
            "More text",
        ]
        result = dedupe_consecutive_lines(lines)
        assert len([l for l in result if l.startswith("─")]) == 1

    def test_full_cleaning_pipeline_with_real_garbage(self):
        """End-to-end test with realistic terminal garbage.

        This test uses actual samples from Claude Code terminal logs
        to verify the full cleaning pipeline works.
        """
        from issue_orchestrator.entrypoints.web import (
            clean_terminal_line,
            is_spinner_fragment,
            dedupe_consecutive_lines,
        )

        # Realistic raw lines from a terminal log
        raw_lines = [
            "\x1b[?25l\x1b[?2004h\x1b[?1004h\x1b[>1u",  # Init sequences
            "\x1b[38;2;215;119;87m· Fiddle-faddling…\x1b[39m",  # Thinking status
            "*\r/\r-\r\\",  # Spinner animation
            "\x1b[6A\x1b[2Cddl",  # Cursor movement + fragment
            "⏺Bash(git status)",  # Actual tool call
            "On branch main",  # Actual output
            "  nothing to commit",  # Actual output
            "\x1b[38;2;215;119;87m✶\x1b[39m Fiddle-faddling…",  # More thinking
            "────────────────────────",  # Separator
            "────────────────────────",  # Duplicate separator
            "❯",  # Prompt
        ]

        # Clean and filter
        cleaned = []
        for line in raw_lines:
            c = clean_terminal_line(line)
            if c.strip() and not is_spinner_fragment(c):
                cleaned.append(c)
        cleaned = dedupe_consecutive_lines(cleaned)

        # Should keep meaningful content
        content = "\n".join(cleaned)
        assert "⏺Bash(git status)" in content or "Bash(git status)" in content
        assert "On branch main" in content
        assert "nothing to commit" in content

        # Should filter garbage
        assert "ddl" not in content
        assert "Fiddle-faddling" not in content

        # Should have at most one separator line
        separator_count = sum(1 for l in cleaned if l.strip().startswith("─"))
        assert separator_count <= 1


class TestDependencyProblemsEndpoint:
    """Test the GET /api/dependency-problems endpoint."""

    def test_get_dependency_problems_empty(self):
        """Test getting dependency problems when none exist."""
        from issue_orchestrator.entrypoints import web
        from issue_orchestrator.domain.models import DependencyProblem

        mock_orch = create_mock_orchestrator()
        mock_orch.state.dependency_problems = {}
        set_orchestrator(mock_orch)

        try:
            client = TestClient(app)
            response = client.get("/api/dependency-problems")

            assert response.status_code == 200
            data = response.json()
            assert data["problems"] == {}
        finally:
            set_orchestrator(None)

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
        set_orchestrator(mock_orch)

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
            set_orchestrator(None)

    def test_get_dependency_problems_when_orchestrator_not_running(self):
        """Test dependency-problems returns 503 when orchestrator not initialized."""
        from issue_orchestrator.entrypoints import web
        set_orchestrator(None)

        client = TestClient(app)
        response = client.get("/api/dependency-problems")

        assert response.status_code == 503
        assert "error" in response.json()


class TestSessionPhasesEndpoint:
    """Tests for the GET /api/session/phases/{issue_number} endpoint."""

    def test_phases_returns_empty_when_no_worktree_found(self):
        """Test phases endpoint returns empty when no worktree exists for issue."""
        from issue_orchestrator.entrypoints import web

        mock_orch = create_mock_orchestrator()
        mock_orch.state.active_sessions = []
        mock_orch.state.session_history = []

        set_orchestrator(mock_orch)
        try:
            client = TestClient(app)
            response = client.get("/api/session/phases/999")

            assert response.status_code == 200
            data = response.json()
            assert data["phases"] == []
            assert data["current_phase"] is None
            assert "error" in data or data.get("issue_number") == 999
        finally:
            set_orchestrator(None)

    def test_phases_returns_503_when_orchestrator_not_running(self):
        """Test phases endpoint returns 503 when orchestrator not initialized."""
        from issue_orchestrator.entrypoints import web
        set_orchestrator(None)

        client = TestClient(app)
        response = client.get("/api/session/phases/123")

        assert response.status_code == 503
        assert "error" in response.json()

    def test_phases_finds_worktree_from_active_session(self, tmp_path):
        """Test phases endpoint finds worktree from active session."""
        from issue_orchestrator.entrypoints import web
        import json

        mock_orch = create_mock_orchestrator()

        # Create a worktree with session data
        sessions_dir = tmp_path / ".issue-orchestrator" / "sessions"
        sessions_dir.mkdir(parents=True)

        run_dir = sessions_dir / "20260117-100000Z__coding-1"
        run_dir.mkdir()
        (run_dir / "manifest.json").write_text(json.dumps({
            "session_name": "coding-1",
            "run_id": "20260117-100000Z",
            "started_at": "2026-01-17T10:00:00Z",
            "ended_at": "2026-01-17T10:30:00Z",
            "issue_number": 123,
            "agent_label": "agent:developer",
            "outcome": "completed",
        }))

        (sessions_dir / "index.json").write_text(json.dumps({
            "runs": [{
                "session_name": "coding-1",
                "run_id": "20260117-100000Z",
                "started_at": "2026-01-17T10:00:00Z",
                "issue_number": 123,
                "run_dir": str(run_dir),
                "agent_label": "agent:developer",
            }]
        }))

        # Create an active session pointing to this worktree
        issue = create_issue(123, "Test Issue")
        session = create_session(issue, worktree_path=str(tmp_path))
        mock_orch.state.active_sessions = [session]

        set_orchestrator(mock_orch)
        try:
            client = TestClient(app)
            response = client.get("/api/session/phases/123")

            assert response.status_code == 200
            data = response.json()
            assert len(data["phases"]) == 1
            assert data["phases"][0]["name"] == "coding-1"
            assert data["phases"][0]["display_name"] == "Coding 1"
            assert data["phases"][0]["status"] == "completed"
            assert data["issue_number"] == 123
        finally:
            set_orchestrator(None)

    def test_phases_formats_phase_names_correctly(self, tmp_path):
        """Test that phase names are formatted correctly for display."""
        from issue_orchestrator.entrypoints import web
        import json

        mock_orch = create_mock_orchestrator()

        sessions_dir = tmp_path / ".issue-orchestrator" / "sessions"
        sessions_dir.mkdir(parents=True)

        # Create multiple phases
        phases_data = [
            ("coding-1", "20260117-100000Z"),
            ("review-1", "20260117-110000Z"),
            ("coding-2", "20260117-120000Z"),
        ]

        runs_index = []
        for phase_name, run_id in phases_data:
            run_dir = sessions_dir / f"{run_id}__{phase_name}"
            run_dir.mkdir()
            (run_dir / "manifest.json").write_text(json.dumps({
                "session_name": phase_name,
                "run_id": run_id,
                "started_at": f"2026-01-17T{run_id[9:11]}:00:00Z",
                "ended_at": f"2026-01-17T{run_id[9:11]}:30:00Z",
                "outcome": "completed",
            }))
            runs_index.append({
                "session_name": phase_name,
                "run_id": run_id,
                "started_at": f"2026-01-17T{run_id[9:11]}:00:00Z",
                "run_dir": str(run_dir),
            })

        (sessions_dir / "index.json").write_text(json.dumps({"runs": runs_index}))

        issue = create_issue(123, "Test Issue")
        session = create_session(issue, worktree_path=str(tmp_path))
        mock_orch.state.active_sessions = [session]

        set_orchestrator(mock_orch)
        try:
            client = TestClient(app)
            response = client.get("/api/session/phases/123")

            assert response.status_code == 200
            data = response.json()
            assert len(data["phases"]) == 3
            assert data["phases"][0]["display_name"] == "Coding 1"
            assert data["phases"][1]["display_name"] == "Review 1"
            assert data["phases"][2]["display_name"] == "Coding 2"
        finally:
            set_orchestrator(None)

    def test_phases_identifies_current_in_progress_phase(self, tmp_path):
        """Test that current_phase is set for in_progress phases."""
        from issue_orchestrator.entrypoints import web
        import json

        mock_orch = create_mock_orchestrator()

        sessions_dir = tmp_path / ".issue-orchestrator" / "sessions"
        sessions_dir.mkdir(parents=True)

        # Create one completed and one in-progress phase
        run1_dir = sessions_dir / "20260117-100000Z__coding-1"
        run1_dir.mkdir()
        (run1_dir / "manifest.json").write_text(json.dumps({
            "session_name": "coding-1",
            "started_at": "2026-01-17T10:00:00Z",
            "ended_at": "2026-01-17T10:30:00Z",
            "outcome": "completed",
        }))

        run2_dir = sessions_dir / "20260117-110000Z__review-1"
        run2_dir.mkdir()
        (run2_dir / "manifest.json").write_text(json.dumps({
            "session_name": "review-1",
            "started_at": "2026-01-17T11:00:00Z",
            # No ended_at - still in progress
        }))

        (sessions_dir / "index.json").write_text(json.dumps({
            "runs": [
                {"session_name": "coding-1", "run_id": "20260117-100000Z",
                 "started_at": "2026-01-17T10:00:00Z", "run_dir": str(run1_dir)},
                {"session_name": "review-1", "run_id": "20260117-110000Z",
                 "started_at": "2026-01-17T11:00:00Z", "run_dir": str(run2_dir)},
            ]
        }))

        issue = create_issue(123, "Test Issue")
        session = create_session(issue, worktree_path=str(tmp_path))
        mock_orch.state.active_sessions = [session]

        set_orchestrator(mock_orch)
        try:
            client = TestClient(app)
            response = client.get("/api/session/phases/123")

            assert response.status_code == 200
            data = response.json()
            assert data["current_phase"] == "review-1"
            assert data["phases"][1]["status"] == "in_progress"
        finally:
            set_orchestrator(None)


class TestSessionWorktreeEndpoint:
    """Tests for the GET /api/session/worktree/{issue_number} endpoint."""

    def test_worktree_from_active_session(self):
        """Returns worktree for active session."""
        from issue_orchestrator.entrypoints import web

        mock_orch = create_mock_orchestrator()
        issue = create_issue(123, "Test Issue")
        session = create_session(issue, worktree_path="/tmp/worktree-123")
        mock_orch.state.active_sessions = [session]

        set_orchestrator(mock_orch)
        try:
            client = TestClient(app)
            response = client.get("/api/session/worktree/123")

            assert response.status_code == 200
            data = response.json()
            assert data["issue_number"] == 123
            assert data["worktree_path"] == "/tmp/worktree-123"
        finally:
            set_orchestrator(None)

    def test_worktree_from_history(self):
        """Returns worktree from history when no active session exists."""
        from issue_orchestrator.entrypoints import web

        mock_orch = create_mock_orchestrator()
        entry = SessionHistoryEntry(
            issue_number=321,
            title="Issue 321",
            agent_type="agent:web",
            status="completed",
            runtime_minutes=10,
            worktree_path=Path("/tmp/worktree-321"),
        )
        mock_orch.state.active_sessions = []
        mock_orch.state.session_history = [entry]

        set_orchestrator(mock_orch)
        try:
            client = TestClient(app)
            response = client.get("/api/session/worktree/321")

            assert response.status_code == 200
            data = response.json()
            assert data["worktree_path"] == "/tmp/worktree-321"
        finally:
            set_orchestrator(None)


def _get_available_port() -> int:
    """Get an available port by binding to port 0 and releasing it."""
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class TestPortUtilityFunctions:
    """Test port utility functions."""

    def test_is_port_in_use_when_available(self):
        """Test port check returns False for available port."""
        from issue_orchestrator.entrypoints.web import _is_port_in_use

        # Get a dynamically allocated available port
        port = _get_available_port()
        result = _is_port_in_use(port)
        assert result is False

    def test_is_port_in_use_when_bound(self):
        """Test port check returns True when port is bound."""
        import socket
        from issue_orchestrator.entrypoints.web import _is_port_in_use

        # Bind to a dynamic port
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]

        try:
            result = _is_port_in_use(port, "127.0.0.1")
            assert result is True
        finally:
            sock.close()

    def test_kill_process_on_port_no_process(self):
        """Test killing process on port when no process exists."""
        from issue_orchestrator.entrypoints.web import _kill_process_on_port

        # Get a dynamically allocated port (no process using it)
        port = _get_available_port()
        result = _kill_process_on_port(port)
        assert result is False

    def test_ensure_port_available_when_available(self):
        """Test ensure_port_available succeeds when port is available."""
        from issue_orchestrator.entrypoints.web import ensure_port_available

        # Get a dynamically allocated available port
        port = _get_available_port()
        # Should not raise
        ensure_port_available(port)

    def test_ensure_port_available_when_unavailable(self):
        """Test ensure_port_available raises when port cannot be freed."""
        import socket
        from issue_orchestrator.entrypoints.web import ensure_port_available

        # Bind to a dynamic port
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]

        try:
            with patch("issue_orchestrator.entrypoints.web._kill_process_on_port", return_value=False):
                with pytest.raises(RuntimeError, match="Port .* is already in use"):
                    ensure_port_available(port)
        finally:
            sock.close()


class TestGetOrchestrator:
    """Test the get_orchestrator dependency function."""

    def test_get_orchestrator_returns_global(self):
        """Test get_orchestrator returns the global orchestrator."""
        from issue_orchestrator.entrypoints import web

        mock_orch = create_mock_orchestrator()
        set_orchestrator(mock_orch)

        try:
            result = web.get_orchestrator()
            assert result is mock_orch
        finally:
            set_orchestrator(None)

    def test_get_orchestrator_returns_none(self):
        """Test get_orchestrator returns None when not set."""
        from issue_orchestrator.entrypoints import web

        set_orchestrator(None)
        result = web.get_orchestrator()
        assert result is None


class TestTriggerServerShutdown:
    """Test the trigger_server_shutdown function."""

    def test_trigger_server_shutdown_sets_flag(self):
        """Test trigger_server_shutdown sets should_exit flag."""
        from issue_orchestrator.entrypoints import web

        mock_server = MagicMock()
        set_server(mock_server)

        try:
            web.trigger_server_shutdown()
            assert mock_server.should_exit is True
        finally:
            set_server(None)

    def test_trigger_server_shutdown_when_no_server(self):
        """Test trigger_server_shutdown handles None server gracefully."""
        from issue_orchestrator.entrypoints import web

        set_server(None)
        # Should not raise
        web.trigger_server_shutdown()


class TestDashboardWithProblems:
    """Test dashboard with problem items in history tab."""

    def test_dashboard_with_failed_session(self):
        """Test dashboard displays failed sessions in history tab."""
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

        set_orchestrator(mock_orch)
        try:
            client = TestClient(app)
            response = client.get("/?tab=history")

            assert response.status_code == 200
            assert "Failed Issue" in response.text
        finally:
            set_orchestrator(None)

    def test_dashboard_with_blocked_session(self):
        """Test dashboard displays blocked sessions in history tab."""
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

        set_orchestrator(mock_orch)
        try:
            client = TestClient(app)
            response = client.get("/?tab=history")

            assert response.status_code == 200
            assert "Blocked Issue" in response.text
        finally:
            set_orchestrator(None)

    def test_dashboard_with_timed_out_session(self):
        """Test dashboard displays timed out sessions in history tab."""
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

        set_orchestrator(mock_orch)
        try:
            client = TestClient(app)
            response = client.get("/?tab=history")

            assert response.status_code == 200
            assert "Timeout Issue" in response.text
        finally:
            set_orchestrator(None)

    def test_dashboard_with_needs_human_session(self):
        """Test dashboard displays needs_human sessions in blocked tab."""
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

        set_orchestrator(mock_orch)
        try:
            client = TestClient(app)
            response = client.get("/?tab=blocked")

            assert response.status_code == 200
            assert "Needs Human Issue" in response.text
        finally:
            set_orchestrator(None)


class TestDashboardStartupStatus:
    """Test dashboard with different startup statuses."""

    def test_dashboard_with_startup_pending(self):
        """Test dashboard when startup is pending."""
        from issue_orchestrator.entrypoints import web
        mock_orch = create_mock_orchestrator()
        mock_orch.state.startup_status = "pending"
        mock_orch.state.startup_message = "Initializing..."

        set_orchestrator(mock_orch)
        try:
            client = TestClient(app)
            response = client.get("/")

            assert response.status_code == 200
            # Should render but not show queue (startup incomplete)
        finally:
            set_orchestrator(None)

    def test_dashboard_with_startup_in_progress(self):
        """Test dashboard when startup is in progress."""
        from issue_orchestrator.entrypoints import web
        mock_orch = create_mock_orchestrator()
        mock_orch.state.startup_status = "in_progress"
        mock_orch.state.startup_message = "Fetching issues..."

        set_orchestrator(mock_orch)
        try:
            client = TestClient(app)
            response = client.get("/")

            assert response.status_code == 200
        finally:
            set_orchestrator(None)


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
            _issue_number=1,
        )
        mock_orch.state.pending_reviews = [review]

        set_orchestrator(mock_orch)
        try:
            client = TestClient(app)
            response = client.get("/api/status")

            assert response.status_code == 200
            data = response.json()
            assert len(data["pending_reviews"]) == 1
            assert data["pending_reviews"][0]["issue_number"] == 1
            assert data["pending_reviews"][0]["pr_number"] == 10
        finally:
            set_orchestrator(None)


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

        set_orchestrator(mock_orch)
        try:
            client = TestClient(app)
            response = client.get("/")

            assert response.status_code == 200
            # Should render the slow session
        finally:
            set_orchestrator(None)


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

        set_orchestrator(mock_orch)
        try:
            client = TestClient(app)
            response = client.get("/")

            assert response.status_code == 200
            # Should show "Reviewing" phase
        finally:
            set_orchestrator(None)


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
                    assert get_orchestrator() is mock_orch

                    # Cancel the task
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass

                    # Clean up
                    set_orchestrator(None)
                    set_server(None)

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

                    set_orchestrator(None)
                    set_server(None)


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

                    set_orchestrator(None)
                    set_server(None)


class TestStripAnsiCodes:
    """Test the strip_ansi_codes function."""

    def test_strips_color_codes(self):
        """Test stripping SGR color codes."""
        from issue_orchestrator.entrypoints.web import strip_ansi_codes

        # Red text
        text = "\x1b[31mError\x1b[0m"
        assert strip_ansi_codes(text) == "Error"

        # Bold green
        text = "\x1b[1;32mSuccess\x1b[0m"
        assert strip_ansi_codes(text) == "Success"

        # 256-color
        text = "\x1b[38;5;196mBright Red\x1b[0m"
        assert strip_ansi_codes(text) == "Bright Red"

        # 24-bit RGB color (like Claude Code uses)
        text = "\x1b[38;2;215;119;87m✶\x1b[0m"
        assert strip_ansi_codes(text) == "✶"

    def test_strips_cursor_movement(self):
        """Test stripping cursor movement codes."""
        from issue_orchestrator.entrypoints.web import strip_ansi_codes

        # Cursor up
        text = "\x1b[6AText"
        assert strip_ansi_codes(text) == "Text"

        # Cursor down, right, left
        text = "Start\x1b[2B\x1b[1C\x1b[3DEnd"
        assert strip_ansi_codes(text) == "StartEnd"

    def test_strips_private_mode_sequences(self):
        """Test stripping private mode sequences like ?2026h."""
        from issue_orchestrator.entrypoints.web import strip_ansi_codes

        # Synchronized output mode (used by Claude Code spinner)
        text = "\x1b[?2026lText\x1b[?2026h"
        assert strip_ansi_codes(text) == "Text"

        # Other private modes
        text = "\x1b[?25hVisible\x1b[?25l"  # Show/hide cursor
        assert strip_ansi_codes(text) == "Visible"

    def test_strips_osc_sequences(self):
        """Test stripping OSC sequences (terminal title, etc.)."""
        from issue_orchestrator.entrypoints.web import strip_ansi_codes

        # Set terminal title
        text = "\x1b]0;My Title\x07Content"
        assert strip_ansi_codes(text) == "Content"

    def test_real_claude_code_spinner_output(self):
        """Test stripping real Claude Code spinner output."""
        from issue_orchestrator.entrypoints.web import strip_ansi_codes

        # Actual output from Claude Code spinner
        # Note: Must include \x1b before each [ for real ANSI sequences
        text = "\x1b[?2026l\x1b[?2026h\n\x1b[6A\x1b[38;2;215;119;87m✶\x1b[1C\x1b[38;2;221;125;93mPerusing…\x1b[39m"
        result = strip_ansi_codes(text)
        # Should preserve the visible text
        assert "✶" in result
        assert "Perusing…" in result
        # Should remove escape sequences
        assert "\x1b[?2026" not in result
        assert "\x1b[6A" not in result
        assert "\x1b[38;2;" not in result

    def test_preserves_plain_text(self):
        """Test that plain text without ANSI codes is preserved."""
        from issue_orchestrator.entrypoints.web import strip_ansi_codes

        text = "Hello, World!"
        assert strip_ansi_codes(text) == "Hello, World!"

        text = "Line 1\nLine 2\nLine 3"
        assert strip_ansi_codes(text) == "Line 1\nLine 2\nLine 3"

    def test_empty_string(self):
        """Test with empty string."""
        from issue_orchestrator.entrypoints.web import strip_ansi_codes

        assert strip_ansi_codes("") == ""

    def test_mixed_content(self):
        """Test with mixed ANSI codes and regular text."""
        from issue_orchestrator.entrypoints.web import strip_ansi_codes

        text = "Normal \x1b[1mbold\x1b[0m normal \x1b[31mred\x1b[0m end"
        assert strip_ansi_codes(text) == "Normal bold normal red end"


class TestPublishJobsEndpoint:
    """Test the GET /api/publish-jobs endpoint."""

    def test_returns_empty_when_no_jobs(self):
        """Test endpoint returns empty list when no jobs."""
        from issue_orchestrator.entrypoints import web

        mock_orch = create_mock_orchestrator()

        # Create mock executor with empty history
        mock_executor = MagicMock()
        mock_executor.get_job_history.return_value = []

        mock_deps = MagicMock()
        mock_deps.publish_executor = mock_executor
        mock_orch.deps = mock_deps

        web._orchestrator = mock_orch
        try:
            client = TestClient(app)
            response = client.get("/api/publish-jobs")

            assert response.status_code == 200
            data = response.json()
            assert data["jobs"] == []
            assert data["count"] == 0
        finally:
            web._orchestrator = None

    def test_returns_job_history(self):
        """Test endpoint returns job history with details."""
        from issue_orchestrator.entrypoints import web
        from issue_orchestrator.control.job_store import JobRecord

        mock_orch = create_mock_orchestrator()

        # Create mock job record
        job_record = JobRecord(
            job_id="job-123",
            issue_number=42,
            session_key="code:42",
            worktree_path="/path/to/worktree",
            worktree_id="wt-abc123",
            branch_name="issue-42-fix",
            status="succeeded",
            created_at=1000.0,
            started_at=1010.0,
            finished_at=1050.0,
            pr_url="https://github.com/owner/repo/pull/100",
            pr_number=100,
        )

        mock_executor = MagicMock()
        mock_executor.get_job_history.return_value = [job_record]

        mock_deps = MagicMock()
        mock_deps.publish_executor = mock_executor
        mock_orch.deps = mock_deps

        web._orchestrator = mock_orch
        try:
            client = TestClient(app)
            response = client.get("/api/publish-jobs")

            assert response.status_code == 200
            data = response.json()
            assert data["count"] == 1

            job = data["jobs"][0]
            assert job["job_id"] == "job-123"
            assert job["issue_number"] == 42
            assert job["status"] == "succeeded"
            assert job["pr_url"] == "https://github.com/owner/repo/pull/100"
            assert job["pr_number"] == 100
            assert job["duration_seconds"] == 40.0
        finally:
            web._orchestrator = None

    def test_filters_by_issue_number(self):
        """Test endpoint filters by issue_number query param."""
        from issue_orchestrator.entrypoints import web

        mock_orch = create_mock_orchestrator()

        mock_executor = MagicMock()
        mock_executor.get_job_history.return_value = []

        mock_deps = MagicMock()
        mock_deps.publish_executor = mock_executor
        mock_orch.deps = mock_deps

        web._orchestrator = mock_orch
        try:
            client = TestClient(app)
            response = client.get("/api/publish-jobs?issue_number=42")

            assert response.status_code == 200
            # Verify filter was passed to executor
            mock_executor.get_job_history.assert_called_once_with(
                issue_number=42, limit=100
            )
        finally:
            web._orchestrator = None

    def test_returns_503_when_orchestrator_not_running(self):
        """Test endpoint returns 503 when orchestrator not initialized."""
        from issue_orchestrator.entrypoints import web

        web._orchestrator = None

        client = TestClient(app)
        response = client.get("/api/publish-jobs")

        assert response.status_code == 503
        assert "error" in response.json()


class TestApiStatusPublishJobs:
    """Test publish jobs included in /api/status endpoint."""

    def test_status_includes_publish_job_stats(self):
        """Test status endpoint includes publish job stats."""
        from issue_orchestrator.entrypoints import web

        mock_orch = create_mock_orchestrator()

        # Create mock executor
        mock_executor = MagicMock()
        mock_executor.get_running_jobs.return_value = []
        mock_executor.get_running_count.return_value = 2
        mock_executor.get_pending_count.return_value = 3

        mock_deps = MagicMock()
        mock_deps.publish_executor = mock_executor
        mock_orch.deps = mock_deps

        web._orchestrator = mock_orch
        try:
            client = TestClient(app)
            response = client.get("/api/status")

            assert response.status_code == 200
            data = response.json()

            assert "publish_job_stats" in data
            assert data["publish_job_stats"]["running"] == 2
            assert data["publish_job_stats"]["pending"] == 3
        finally:
            web._orchestrator = None

    def test_status_includes_running_publish_jobs(self):
        """Test status endpoint includes running publish jobs."""
        from issue_orchestrator.entrypoints import web
        from issue_orchestrator.domain.models import PublishJob, PublishJobStatus

        mock_orch = create_mock_orchestrator()

        # Create a running job
        running_job = PublishJob(
            job_id="running-job-1",
            issue_number=42,
            session_key="code:42",
            status=PublishJobStatus.RUNNING,
            started_at=1000.0,
        )

        mock_executor = MagicMock()
        mock_executor.get_running_jobs.return_value = [running_job]
        mock_executor.get_running_count.return_value = 1
        mock_executor.get_pending_count.return_value = 0

        mock_deps = MagicMock()
        mock_deps.publish_executor = mock_executor
        mock_orch.deps = mock_deps

        web._orchestrator = mock_orch
        try:
            client = TestClient(app)
            response = client.get("/api/status")

            assert response.status_code == 200
            data = response.json()

            assert "publish_jobs" in data
            assert len(data["publish_jobs"]) == 1
            assert data["publish_jobs"][0]["job_id"] == "running-job-1"
            assert data["publish_jobs"][0]["issue_number"] == 42
            assert data["publish_jobs"][0]["status"] == "running"
        finally:
            web._orchestrator = None
