"""Unit tests for the FastAPI web module."""

import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch, Mock
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

        web._orchestrator = mock_orch

        with patch("issue_orchestrator.adapters.terminal._iterm2.select_tab_by_name") as mock_select:
            mock_select.return_value = True

            client = TestClient(app)
            response = client.post("/api/focus/1")

            assert response.status_code == 200
            data = response.json()
            assert data["status"] == "focused"
            assert data["issue_number"] == 1
            mock_select.assert_called_once_with("#1")

    def test_focus_session_falls_back_to_tmux(self):
        """Test focus falls back to tmux if iTerm2 fails."""
        from issue_orchestrator.entrypoints import web
        mock_orch = create_mock_orchestrator()

        issue = create_issue(1)
        session = create_session(issue)
        mock_orch.state.active_sessions = [session]

        web._orchestrator = mock_orch

        with patch("issue_orchestrator.adapters.terminal._iterm2.select_tab_by_name") as mock_select:
            with patch("issue_orchestrator.adapters.terminal._tmux.get_manager") as mock_get_manager:
                mock_select.return_value = False
                mock_manager = MagicMock()
                mock_manager.select_window.return_value = True
                mock_get_manager.return_value = mock_manager

                client = TestClient(app)
                response = client.post("/api/focus/1")

                assert response.status_code == 200
                data = response.json()
                assert data["status"] == "focused"
                mock_manager.select_window.assert_called_once_with(1)

    def test_focus_session_not_found(self):
        """Test focus returns 404 when session not found."""
        from issue_orchestrator.entrypoints import web
        mock_orch = create_mock_orchestrator()
        web._orchestrator = mock_orch

        client = TestClient(app)
        response = client.post("/api/focus/999")

        assert response.status_code == 404
        assert "error" in response.json()

    def test_focus_session_fails(self):
        """Test focus returns 500 when both iTerm2 and tmux fail."""
        from issue_orchestrator.entrypoints import web
        mock_orch = create_mock_orchestrator()

        issue = create_issue(1)
        session = create_session(issue)
        mock_orch.state.active_sessions = [session]

        web._orchestrator = mock_orch

        with patch("issue_orchestrator.adapters.terminal._iterm2.select_tab_by_name") as mock_select:
            with patch("issue_orchestrator.adapters.terminal._tmux.get_manager") as mock_get_manager:
                mock_select.return_value = False
                mock_manager = MagicMock()
                mock_manager.select_window.return_value = False
                mock_get_manager.return_value = mock_manager

                client = TestClient(app)
                response = client.post("/api/focus/1")

                assert response.status_code == 500
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
