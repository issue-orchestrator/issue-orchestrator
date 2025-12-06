"""Unit tests for the web dashboard module."""

import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch, AsyncMock
from fastapi.testclient import TestClient
from datetime import datetime

from issue_orchestrator.models import (
    Issue,
    Session,
    AgentConfig,
    OrchestratorState,
    SessionHistoryEntry,
)
from issue_orchestrator.config import Config
from issue_orchestrator import web


@pytest.fixture
def sample_agent_config(tmp_path):
    """Create a sample agent config for testing."""
    prompt_file = tmp_path / "prompt.txt"
    prompt_file.write_text("Test prompt")

    return AgentConfig(
        prompt_path=prompt_file,
        worktree_base=tmp_path,
        model="sonnet",
        timeout_minutes=45,
    )


@pytest.fixture
def sample_config(sample_agent_config, tmp_path):
    """Create a sample Config object for testing."""
    config = Config()
    config.agents["agent:web"] = sample_agent_config
    config.agents["agent:backend"] = sample_agent_config
    config.repo = "owner/repo"
    config.repo_root = tmp_path
    config.max_sessions = 3
    config.web_port = 8080
    config.ui_mode = "web"
    config.config_path = tmp_path / ".issue-orchestrator.yaml"
    return config


@pytest.fixture
def sample_issue():
    """Create a sample issue for testing."""
    return Issue(
        number=123,
        title="Test issue",
        labels=["agent:web", "priority:high"],
    )


@pytest.fixture
def sample_session(sample_issue, sample_agent_config, tmp_path):
    """Create a sample session for testing."""
    return Session(
        issue=sample_issue,
        agent_config=sample_agent_config,
        tmux_session_name="issue-123",
        worktree_path=tmp_path / "worktree",
        branch_name="feature/test-123",
    )


@pytest.fixture
def sample_orchestrator(sample_config, sample_session):
    """Create a mock orchestrator for testing."""
    orchestrator = MagicMock()
    orchestrator.config = sample_config
    orchestrator.state = OrchestratorState()
    orchestrator.state.active_sessions = [sample_session]
    orchestrator.state.paused = False
    orchestrator.pause = MagicMock()
    orchestrator.resume = MagicMock()
    orchestrator.request_shutdown = MagicMock()
    return orchestrator


@pytest.fixture
def client(sample_orchestrator):
    """Create a test client with a mock orchestrator."""
    web._orchestrator = sample_orchestrator
    return TestClient(web.app)


class TestDashboardEndpoint:
    """Tests for the main dashboard HTML endpoint."""

    def test_dashboard_renders_html(self, client, sample_orchestrator):
        """Test that the dashboard renders HTML."""
        response = client.get("/")
        assert response.status_code == 200
        assert response.headers["content-type"] == "text/html; charset=utf-8"
        assert "<html" in response.text.lower() or "<!doctype" in response.text.lower()

    def test_dashboard_returns_html_response(self, client):
        """Test that dashboard returns proper HTML response."""
        response = client.get("/")
        assert response.status_code == 200
        # Response should be HTML content
        assert response.headers["content-type"] == "text/html; charset=utf-8"
        # Check for HTML structure
        assert "<html" in response.text.lower() or "<!doctype" in response.text.lower()

    def test_dashboard_pagination_page_1(self, client):
        """Test dashboard with default page (page 1)."""
        response = client.get("/")
        assert response.status_code == 200

    def test_dashboard_pagination_specific_page(self, client):
        """Test dashboard with specific page number."""
        response = client.get("/?page=2")
        assert response.status_code == 200

    def test_dashboard_pagination_invalid_page(self, client):
        """Test dashboard with invalid page number (should default to page 1)."""
        response = client.get("/?page=0")
        assert response.status_code == 200

    def test_dashboard_includes_active_sessions(self, client, sample_orchestrator):
        """Test that dashboard includes active sessions."""
        response = client.get("/")
        assert response.status_code == 200
        assert "Test issue" in response.text
        assert "123" in response.text


class TestStatusEndpoint:
    """Tests for the /api/status endpoint."""

    def test_get_status_success(self, client, sample_orchestrator):
        """Test getting orchestrator status."""
        response = client.get("/api/status")
        assert response.status_code == 200
        data = response.json()
        assert "paused" in data
        assert "active_sessions" in data
        assert "max_sessions" in data
        assert "completed_today" in data
        assert "queue" in data

    def test_get_status_without_orchestrator(self):
        """Test /api/status when orchestrator is not running."""
        web._orchestrator = None
        client = TestClient(web.app)
        response = client.get("/api/status")
        assert response.status_code == 503
        assert "not running" in response.json().get("error", "").lower()

    def test_get_status_active_sessions(self, client, sample_session):
        """Test that status includes active sessions with correct data."""
        response = client.get("/api/status")
        assert response.status_code == 200
        data = response.json()
        assert len(data["active_sessions"]) == 1
        session_data = data["active_sessions"][0]
        assert session_data["issue_number"] == 123
        assert session_data["title"] == "Test issue"
        assert "agent_type" in session_data
        assert "status" in session_data

    def test_get_status_paused_state(self, client, sample_orchestrator):
        """Test that status reflects paused state."""
        sample_orchestrator.state.paused = True
        response = client.get("/api/status")
        assert response.status_code == 200
        assert response.json()["paused"] is True


class TestPauseResumeEndpoints:
    """Tests for pause and resume control endpoints."""

    def test_pause_endpoint(self, client, sample_orchestrator):
        """Test pausing the orchestrator."""
        response = client.post("/api/pause")
        assert response.status_code == 200
        assert response.json()["status"] == "paused"
        sample_orchestrator.pause.assert_called_once()

    def test_resume_endpoint(self, client, sample_orchestrator):
        """Test resuming the orchestrator."""
        response = client.post("/api/resume")
        assert response.status_code == 200
        assert response.json()["status"] == "resumed"
        sample_orchestrator.resume.assert_called_once()

    def test_pause_without_orchestrator(self):
        """Test pause when orchestrator is not running."""
        web._orchestrator = None
        client = TestClient(web.app)
        response = client.post("/api/pause")
        assert response.status_code == 503

    def test_resume_without_orchestrator(self):
        """Test resume when orchestrator is not running."""
        web._orchestrator = None
        client = TestClient(web.app)
        response = client.post("/api/resume")
        assert response.status_code == 503


class TestFocusSessionEndpoint:
    """Tests for the /api/focus/{issue_number} endpoint."""

    def test_focus_existing_session(self, client, sample_orchestrator):
        """Test focusing on an existing session."""
        with patch("issue_orchestrator.iterm2.select_tab_by_name") as mock_select:
            mock_select.return_value = True
            response = client.post("/api/focus/123")
            assert response.status_code == 200
            assert response.json()["status"] == "focused"
            assert response.json()["issue_number"] == 123

    def test_focus_nonexistent_session(self, client):
        """Test focusing on a session that doesn't exist."""
        response = client.post("/api/focus/999")
        assert response.status_code == 404
        assert "not found" in response.json().get("error", "").lower()

    def test_focus_fallback_to_tmux(self, client, sample_orchestrator):
        """Test focus fallback to tmux when iTerm2 fails."""
        with patch("issue_orchestrator.iterm2.select_tab_by_name") as mock_iterm, \
             patch("issue_orchestrator.tmux.get_manager") as mock_tmux:
            mock_iterm.return_value = False
            mock_manager = MagicMock()
            mock_manager.select_window.return_value = True
            mock_tmux.return_value = mock_manager

            response = client.post("/api/focus/123")
            assert response.status_code == 200
            assert response.json()["status"] == "focused"

    def test_focus_without_orchestrator(self, sample_orchestrator):
        """Test focus when orchestrator is not running."""
        old_orchestrator = web._orchestrator
        web._orchestrator = None
        try:
            client = TestClient(web.app)
            response = client.post("/api/focus/123")
            assert response.status_code == 503
        finally:
            web._orchestrator = old_orchestrator


class TestFinderEndpoint:
    """Tests for the /api/finder/{issue_number} endpoint."""

    def test_open_in_finder_success(self, client, sample_orchestrator, tmp_path):
        """Test opening worktree in Finder."""
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        sample_orchestrator.state.active_sessions[0].worktree_path = worktree

        with patch("subprocess.run") as mock_run, \
             patch("os.uname") as mock_uname:
            mock_uname.return_value.sysname = "Darwin"
            response = client.post("/api/finder/123")
            assert response.status_code == 200
            assert response.json()["status"] == "opened"

    def test_open_in_finder_session_not_found(self, client):
        """Test open in Finder for nonexistent session."""
        response = client.post("/api/finder/999")
        assert response.status_code == 404

    def test_open_in_finder_nonexistent_worktree(self, client, sample_orchestrator, tmp_path):
        """Test open in Finder when worktree doesn't exist."""
        sample_orchestrator.state.active_sessions[0].worktree_path = tmp_path / "nonexistent"

        with patch("os.uname") as mock_uname:
            mock_uname.return_value.sysname = "Darwin"
            response = client.post("/api/finder/123")
            assert response.status_code == 404
            assert "not found" in response.json().get("error", "").lower()

    def test_open_in_finder_non_macos(self, client, sample_orchestrator, tmp_path):
        """Test open in Finder on non-macOS system."""
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        sample_orchestrator.state.active_sessions[0].worktree_path = worktree

        with patch("os.uname") as mock_uname:
            mock_uname.return_value.sysname = "Linux"
            response = client.post("/api/finder/123")
            assert response.status_code == 400
            assert "macos" in response.json().get("error", "").lower()


class TestPromptEndpoint:
    """Tests for the /api/prompt/{agent_type} endpoint."""

    def test_open_prompt_success(self, client, sample_orchestrator, sample_agent_config):
        """Test opening agent prompt."""
        with patch("subprocess.run") as mock_run, \
             patch("os.uname") as mock_uname:
            mock_uname.return_value.sysname = "Darwin"
            response = client.post("/api/prompt/web")
            assert response.status_code == 200
            assert response.json()["status"] == "opened"

    def test_open_prompt_with_agent_prefix(self, client, sample_orchestrator):
        """Test opening prompt with full agent: prefix."""
        with patch("subprocess.run") as mock_run, \
             patch("os.uname") as mock_uname:
            mock_uname.return_value.sysname = "Darwin"
            response = client.post("/api/prompt/agent:web")
            assert response.status_code == 200
            assert response.json()["status"] == "opened"

    def test_open_prompt_agent_not_found(self, client):
        """Test opening prompt for agent that doesn't exist."""
        response = client.post("/api/prompt/nonexistent")
        assert response.status_code == 404
        assert "not found" in response.json().get("error", "").lower()

    def test_open_prompt_file_not_found(self, client, sample_orchestrator, tmp_path):
        """Test opening prompt when file doesn't exist."""
        nonexistent_prompt = tmp_path / "nonexistent.txt"
        sample_orchestrator.config.agents["agent:web"].prompt_path = nonexistent_prompt

        response = client.post("/api/prompt/web")
        assert response.status_code == 404
        assert "not found" in response.json().get("error", "").lower()

    def test_open_prompt_non_macos(self, client, sample_orchestrator):
        """Test opening prompt on non-macOS system."""
        with patch("os.uname") as mock_uname:
            mock_uname.return_value.sysname = "Linux"
            response = client.post("/api/prompt/web")
            assert response.status_code == 400


class TestShutdownEndpoint:
    """Tests for the /api/shutdown endpoint."""

    def test_shutdown_request(self, client, sample_orchestrator):
        """Test requesting orchestrator shutdown."""
        response = client.post("/api/shutdown")
        assert response.status_code == 200
        assert response.json()["status"] == "shutdown_requested"
        sample_orchestrator.request_shutdown.assert_called_once()

    def test_shutdown_without_orchestrator(self):
        """Test shutdown when orchestrator is not running."""
        web._orchestrator = None
        client = TestClient(web.app)
        response = client.post("/api/shutdown")
        assert response.status_code == 503


class TestInfoEndpoint:
    """Tests for the /api/info endpoint."""

    def test_get_info_success(self, client, sample_orchestrator):
        """Test getting orchestrator info."""
        response = client.get("/api/info")
        assert response.status_code == 200
        data = response.json()
        assert "version" in data
        assert "repo" in data
        assert "ui_mode" in data
        assert "max_sessions" in data
        assert "active_sessions" in data
        assert "completed_today" in data

    def test_get_info_contains_correct_data(self, client, sample_orchestrator):
        """Test that info contains correct data."""
        response = client.get("/api/info")
        assert response.status_code == 200
        data = response.json()
        assert data["repo"] == "owner/repo"
        assert data["ui_mode"] == "web"
        assert data["max_sessions"] == 3
        assert data["active_sessions"] == 1

    def test_get_info_without_orchestrator(self):
        """Test /api/info when orchestrator is not running."""
        web._orchestrator = None
        client = TestClient(web.app)
        response = client.get("/api/info")
        assert response.status_code == 503


class TestConfigEndpoint:
    """Tests for the /api/config endpoint."""

    def test_get_config_success(self, client, sample_orchestrator, tmp_path):
        """Test getting config file."""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text("test: config")
        sample_orchestrator.config.config_path = config_file

        response = client.get("/api/config")
        assert response.status_code == 200
        data = response.json()
        assert "config" in data
        assert "test: config" in data["config"]

    def test_get_config_file_not_found(self, client, sample_orchestrator, tmp_path):
        """Test getting config when file doesn't exist."""
        sample_orchestrator.config.config_path = tmp_path / "nonexistent.yaml"

        response = client.get("/api/config")
        assert response.status_code == 200
        data = response.json()
        assert "not found" in data["config"].lower()

    def test_get_config_without_orchestrator(self):
        """Test /api/config when orchestrator is not running."""
        web._orchestrator = None
        client = TestClient(web.app)
        response = client.get("/api/config")
        assert response.status_code == 503


class TestTestDataEndpoints:
    """Tests for test data creation and cleanup endpoints."""

    def test_create_test_issues(self, client, sample_orchestrator):
        """Test creating test issues."""
        with patch("issue_orchestrator.test_data.create_test_issues") as mock_create:
            mock_create.return_value = [
                "https://github.com/owner/repo/issues/1",
                "https://github.com/owner/repo/issues/2",
            ]
            response = client.post("/api/test/create")
            assert response.status_code == 200
            data = response.json()
            assert "created" in data
            assert len(data["created"]) == 2

    def test_create_test_issues_no_repo(self, client, sample_orchestrator):
        """Test creating test issues when repo is not configured."""
        sample_orchestrator.config.repo = None
        response = client.post("/api/test/create")
        assert response.status_code == 400

    def test_create_test_issues_error(self, client, sample_orchestrator):
        """Test create test issues when an error occurs."""
        with patch("issue_orchestrator.test_data.create_test_issues") as mock_create:
            mock_create.side_effect = Exception("API error")
            response = client.post("/api/test/create")
            assert response.status_code == 500

    def test_cleanup_test_issues(self, client, sample_orchestrator):
        """Test cleaning up test issues."""
        with patch("issue_orchestrator.test_data.cleanup_test_issues") as mock_cleanup:
            mock_cleanup.return_value = 2
            response = client.post("/api/test/cleanup")
            assert response.status_code == 200
            data = response.json()
            assert "closed" in data
            assert data["closed"] == 2

    def test_cleanup_test_issues_removes_history(self, client, sample_orchestrator):
        """Test that cleanup also removes test issues from history."""
        # Add test issue to history
        test_entry = SessionHistoryEntry(
            issue_number=999,
            title="[TEST] Sample test",
            agent_type="agent:web",
            status="completed",
            pr_url=None,
            runtime_minutes=5,
        )
        sample_orchestrator.state.session_history = [test_entry]

        with patch("issue_orchestrator.test_data.cleanup_test_issues") as mock_cleanup:
            mock_cleanup.return_value = 1
            response = client.post("/api/test/cleanup")
            assert response.status_code == 200
            # History should be cleared of test items
            assert len(sample_orchestrator.state.session_history) == 0


class TestHistoryEndpoints:
    """Tests for session history management endpoints."""

    def test_clear_history(self, client, sample_orchestrator):
        """Test clearing all history."""
        entry = SessionHistoryEntry(
            issue_number=100,
            title="Completed issue",
            agent_type="agent:web",
            status="completed",
            pr_url=None,
            runtime_minutes=10,
        )
        sample_orchestrator.state.session_history = [entry]
        sample_orchestrator.state.completed_today = [100]

        response = client.post("/api/history/clear")
        assert response.status_code == 200
        data = response.json()
        assert data["cleared"] == 1
        assert len(sample_orchestrator.state.session_history) == 0
        assert len(sample_orchestrator.state.completed_today) == 0

    def test_dismiss_history_entry(self, client, sample_orchestrator):
        """Test dismissing a single history entry."""
        entry = SessionHistoryEntry(
            issue_number=100,
            title="Completed issue",
            agent_type="agent:web",
            status="completed",
            pr_url=None,
            runtime_minutes=10,
        )
        sample_orchestrator.state.session_history = [entry]
        sample_orchestrator.state.completed_today = [100]

        response = client.post("/api/history/dismiss/100")
        assert response.status_code == 200
        data = response.json()
        assert data["dismissed"] == 1
        assert len(sample_orchestrator.state.session_history) == 0
        assert 100 not in sample_orchestrator.state.completed_today

    def test_dismiss_nonexistent_entry(self, client, sample_orchestrator):
        """Test dismissing an entry that doesn't exist."""
        response = client.post("/api/history/dismiss/999")
        assert response.status_code == 200
        data = response.json()
        assert data["dismissed"] == 0

    def test_retry_issue(self, client, sample_orchestrator):
        """Test retrying an issue from history."""
        entry = SessionHistoryEntry(
            issue_number=100,
            title="Failed issue",
            agent_type="agent:web",
            status="failed",
            pr_url=None,
            runtime_minutes=10,
        )
        sample_orchestrator.state.session_history = [entry]
        sample_orchestrator.state.completed_today = [100]

        response = client.post("/api/retry/100")
        assert response.status_code == 200
        data = response.json()
        assert data["retrying"] == 100
        assert len(sample_orchestrator.state.session_history) == 0
        assert 100 not in sample_orchestrator.state.completed_today

    def test_retry_nonexistent_issue(self, client):
        """Test retrying an issue that doesn't exist in history."""
        response = client.post("/api/retry/999")
        assert response.status_code == 200
        data = response.json()
        assert "retrying" in data


class TestDebugEndpoint:
    """Tests for the /api/debug endpoint."""

    def test_get_debug_info(self, client, sample_orchestrator):
        """Test getting debug information."""
        response = client.get("/api/debug")
        assert response.status_code == 200
        data = response.json()
        assert "paused" in data
        assert "config_path" in data
        assert "repo_root" in data
        assert "priority_queue" in data
        assert "agents" in data
        assert "startup_options" in data

    def test_debug_contains_agent_info(self, client, sample_orchestrator):
        """Test that debug info contains agent information."""
        response = client.get("/api/debug")
        assert response.status_code == 200
        data = response.json()
        assert "agent:web" in data["agents"]
        assert "timeout" in data["agents"]["agent:web"]

    def test_debug_startup_options(self, client, sample_orchestrator):
        """Test that debug info contains startup options."""
        response = client.get("/api/debug")
        assert response.status_code == 200
        data = response.json()
        assert data["startup_options"]["ui_mode"] == "web"
        assert data["startup_options"]["max_sessions"] == 3

    def test_get_debug_without_orchestrator(self):
        """Test /api/debug when orchestrator is not running."""
        web._orchestrator = None
        client = TestClient(web.app)
        response = client.get("/api/debug")
        assert response.status_code == 503
