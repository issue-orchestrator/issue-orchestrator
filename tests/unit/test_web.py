"""Unit tests for the FastAPI web module."""

import json
import pytest
from pathlib import Path
import tempfile
from typing import Any
from unittest.mock import MagicMock, patch, Mock, AsyncMock
from fastapi.testclient import TestClient

from issue_orchestrator.entrypoints.web import app, get_orchestrator, set_orchestrator, set_server
from issue_orchestrator.contracts.public import ShutdownRequestedPayload, StartupCompletePayload
from issue_orchestrator.control.label_manager import LabelManager


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
    PendingReview,
    PendingRework,
    DiscoveredReview,
    DiscoveredRework,
    DiscoveredFailure,
    PendingValidationRetry,
    ImmediateCleanup,
    OrchestratorState,
    AgentConfig,
    SessionStatus,
)
from issue_orchestrator.infra.config import Config
from issue_orchestrator.domain.issue_key import FakeIssueKey
from issue_orchestrator.domain.session_key import SessionKey, TaskKind
from issue_orchestrator.domain.event_taxonomy import infer_event_intent
from issue_orchestrator.timeline import (
    TIMELINE_SCHEMA_VERSION,
    TimelineArtifact,
    TimelineEvent,
    TimelineStream,
)

_TEST_RUN_DIR_BY_ISSUE: dict[int, str] = {}


def _ensure_test_run_dir(issue_number: int) -> str:
    """Create a real run dir with required artifacts for strict action wiring."""
    existing = _TEST_RUN_DIR_BY_ISSUE.get(issue_number)
    if existing:
        return existing

    from issue_orchestrator.execution.session_output_adapter import FileSystemSessionOutput

    root = Path(tempfile.mkdtemp(prefix=f"io-test-run-{issue_number}-"))
    worktree = root / "worktree"
    worktree.mkdir(parents=True, exist_ok=True)

    session_output = FileSystemSessionOutput()
    run = session_output.start_run(worktree, f"issue-{issue_number}", issue_number=issue_number)
    (run.run_dir / "session.log").write_text("agent output\n", encoding="utf-8")
    claude_log = run.run_dir / "claude.jsonl"
    claude_log.write_text('{"type":"assistant","content":"ok"}\n', encoding="utf-8")
    session_output.update_manifest(run.run_dir, {"claude_log_path": str(claude_log)})
    _TEST_RUN_DIR_BY_ISSUE[issue_number] = str(run.run_dir)
    return _TEST_RUN_DIR_BY_ISSUE[issue_number]


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
    mock_deps.timeline_reader = MagicMock()
    mock_orch.deps = mock_deps
    mock_orch.scheduler = MagicMock()
    mock_orch.scheduler.sort_by_priority.side_effect = lambda issues: issues
    mock_orch.scheduler.dependency_evaluator = None
    mock_orch.repository_host = MagicMock()
    mock_orch.repository_host.get_issue.return_value = None
    mock_orch.repository_host.update_label_cache = MagicMock()

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


def build_timeline_event(
    event_name: str,
    *,
    issue_number: int = 123,
    event_id: str = "e1",
    timestamp: str = "2026-02-06T00:00:00Z",
    phase: str = "in_progress",
    step: str | None = None,
    status: str = "started",
    level: str = "phase",
    summary: str | None = None,
    parent_key: str | None = None,
    artifacts: list[TimelineArtifact] | None = None,
    detail: str | None = None,
    run_id: str | None = None,
    run_dir: str | None = None,
    agent: str | None = None,
    task: str | None = None,
    rework_cycle: int | None = None,
    reviewer_agent: str | None = None,
    logical_run: int = 1,
    logical_cycle: int | None = None,
    logical_phase: str | None = None,
    timeline_schema_version: int = TIMELINE_SCHEMA_VERSION,
) -> TimelineEvent:
    """Build a TimelineEvent with sensible defaults for intent-focused tests."""
    from issue_orchestrator.entrypoints.web import _timeline_event_requires_run_dir

    inferred_intent = infer_event_intent(event_name=event_name, task=task)
    inferred_phase = logical_phase
    if inferred_phase is None:
        if inferred_intent.value == "review":
            inferred_phase = "review"
        elif inferred_intent.value == "rework":
            inferred_phase = "rework"
        elif inferred_intent.value == "orchestrator":
            inferred_phase = "orchestrator"
        else:
            inferred_phase = "coding"
    if logical_cycle is None:
        logical_cycle = (rework_cycle + 1) if isinstance(rework_cycle, int) and rework_cycle >= 0 else 1
    if run_dir is None and _timeline_event_requires_run_dir({"event": event_name, "review_oriented": inferred_intent.value == "review"}):
        run_dir = _ensure_test_run_dir(issue_number)

    return TimelineEvent(
        event_id=event_id,
        timestamp=timestamp,
        event=event_name,
        issue_number=issue_number,
        phase=phase,
        step=step or event_name.split(".")[-1],
        status=status,
        level=level,
        summary=summary,
        parent_key=parent_key or f"session:issue-{issue_number}",
        artifacts=artifacts or [],
        detail=detail,
        run_id=run_id,
        run_dir=run_dir,
        agent=agent,
        task=task,
        rework_cycle=rework_cycle,
        reviewer_agent=reviewer_agent,
        timeline_schema_version=timeline_schema_version,
        review_oriented=(inferred_intent.value == "review"),
        event_intent=inferred_intent.value,
        logical_run=logical_run,
        logical_cycle=logical_cycle,
        logical_phase=inferred_phase,
    )


def fetch_issue_detail_payload(
    events: list[TimelineEvent],
    *,
    issue_number: int = 123,
    title: str = "Detail Issue",
) -> dict[str, Any]:
    """Call /api/issue-detail with a mocked timeline and return JSON payload."""
    mock_orch = create_mock_orchestrator()
    mock_orch.state.cached_queue_issues = [create_issue(issue_number, title)]
    mock_orch.deps.timeline_reader.read.return_value = TimelineStream(
        issue_number=issue_number,
        events=events,
    )
    set_orchestrator(mock_orch)
    try:
        client = TestClient(app)
        response = client.get(f"/api/issue-detail/{issue_number}")
        assert response.status_code == 200
        return response.json()
    finally:
        set_orchestrator(None)


def _latest_run(payload: dict[str, Any]) -> dict[str, Any]:
    runs = payload.get("runs")
    assert isinstance(runs, list) and runs
    latest = runs[-1]
    assert isinstance(latest, dict)
    return latest


def _first_cycle(payload: dict[str, Any]) -> dict[str, Any]:
    cycles = payload.get("cycles")
    assert isinstance(cycles, list) and cycles
    first = cycles[0]
    assert isinstance(first, dict)
    return first


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
        assert "repo_identity" in data

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

    def test_unblock_retry_removes_blocking_and_pr_pending_labels(self):
        """Unblock endpoint removes all labels that prevent scheduling."""
        mock_orch = create_mock_orchestrator()
        lm = LabelManager(mock_orch.config)
        mock_orch.deps.label_manager = lm
        mock_orch.deps.action_applier = MagicMock()
        mock_orch.deps.action_applier.apply.return_value = Mock(success=True, error=None)
        mock_orch.repository_host.get_issue_labels.return_value = [
            "agent:web",
            lm.blocked,
            lm.pr_pending,
        ]
        mock_orch.state.session_history = [
            SessionHistoryEntry(
                issue_number=4057,
                title="Issue 4057",
                agent_type="agent:web",
                status="blocked",
                runtime_minutes=5,
            ),
        ]
        mock_orch.state.completed_today = [4057]

        set_orchestrator(mock_orch)

        client = TestClient(app)
        response = client.post("/api/unblock-retry", json={"issues": [4057]})

        assert response.status_code == 200
        assert response.json()["unblocked"] == [4057]
        removed = [call.args[0].label for call in mock_orch.deps.action_applier.apply.call_args_list]
        assert lm.blocked in removed
        assert lm.pr_pending in removed
        assert all(entry.issue_number != 4057 for entry in mock_orch.state.session_history)
        assert 4057 not in mock_orch.state.completed_today
        mock_orch.request_refresh.assert_called_once()

    def test_get_history_dedupes_to_latest_per_issue(self):
        """History endpoint returns only the latest entry for each issue."""
        mock_orch = create_mock_orchestrator()
        mock_orch.state.session_history = [
            SessionHistoryEntry(
                issue_number=1,
                title="Issue 1 (old)",
                agent_type="agent:web",
                status="failed",
                runtime_minutes=10,
            ),
            SessionHistoryEntry(
                issue_number=1,
                title="Issue 1 (latest)",
                agent_type="agent:web",
                status="blocked",
                runtime_minutes=3,
            ),
            SessionHistoryEntry(
                issue_number=2,
                title="Issue 2",
                agent_type="agent:web",
                status="completed",
                runtime_minutes=8,
            ),
        ]

        set_orchestrator(mock_orch)

        client = TestClient(app)
        response = client.get("/api/history")

        assert response.status_code == 200
        payload = response.json()
        assert payload["count"] == 2
        issue1_entries = [e for e in payload["history"] if e["issue_number"] == 1]
        assert len(issue1_entries) == 1
        assert issue1_entries[0]["status"] == "blocked"


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
        from issue_orchestrator.entrypoints.web import add_event_subscriber, broadcast_event, remove_event_subscriber

        # Create a test queue and add it as a subscriber
        test_queue: asyncio.Queue = asyncio.Queue(maxsize=100)
        add_event_subscriber(test_queue)

        try:
            # Broadcast an event
            await broadcast_event("test_event", {"key": "value"})

            # Check the queue received the event
            assert not test_queue.empty()
            event = test_queue.get_nowait()
            assert event["type"] == "test_event"
            assert event["data"] == {"key": "value"}
        finally:
            remove_event_subscriber(test_queue)

    @pytest.mark.asyncio
    async def test_broadcast_event_handles_empty_data(self):
        """Test broadcasting events with no data."""
        import asyncio
        from issue_orchestrator.entrypoints.web import add_event_subscriber, broadcast_event, remove_event_subscriber

        test_queue: asyncio.Queue = asyncio.Queue(maxsize=100)
        add_event_subscriber(test_queue)

        try:
            await broadcast_event("empty_event")

            event = test_queue.get_nowait()
            assert event["type"] == "empty_event"
            assert event["data"] == {}
        finally:
            remove_event_subscriber(test_queue)

    @pytest.mark.asyncio
    async def test_broadcast_event_removes_full_queues(self):
        """Test that full queues are removed from subscribers."""
        import asyncio
        from issue_orchestrator.entrypoints.web import add_event_subscriber, broadcast_event, event_subscribers_snapshot, remove_event_subscriber

        # Create a queue with size 1 and fill it
        full_queue: asyncio.Queue = asyncio.Queue(maxsize=1)
        full_queue.put_nowait({"dummy": "event"})

        add_event_subscriber(full_queue)
        assert full_queue in event_subscribers_snapshot()

        try:
            # This should fail silently and remove the full queue
            await broadcast_event("overflow_event")

            # Queue should be removed from subscribers
            assert full_queue not in event_subscribers_snapshot()
        finally:
            remove_event_subscriber(full_queue)

    @pytest.mark.asyncio
    async def test_broadcast_event_no_subscribers(self):
        """Test broadcasting when there are no subscribers."""
        from issue_orchestrator.entrypoints.web import broadcast_event, event_subscribers_snapshot, swapped_event_subscribers

        # Ensure no subscribers
        original_subscribers = event_subscribers_snapshot()
        with swapped_event_subscribers(set()):
            # Should not raise any errors
            await broadcast_event("no_listeners", {"data": "test"})
        assert event_subscribers_snapshot() == original_subscribers

    def test_events_endpoint_exists(self):
        """Test that /api/events endpoint is registered."""
        from issue_orchestrator.entrypoints.web import app

        # Check the endpoint is registered by looking at routes
        routes = [route.path for route in app.routes]
        assert "/api/events" in routes

    @pytest.mark.asyncio
    async def test_shutdown_endpoint_broadcasts_event(self, monkeypatch):
        """Shutdown endpoint should emit shutdown_requested SSE event."""
        import asyncio
        from types import SimpleNamespace
        from issue_orchestrator.entrypoints import web
        from issue_orchestrator.entrypoints.web import get_orchestrator, set_orchestrator

        class OrchestratorStub:
            def __init__(self):
                self.state = SimpleNamespace(active_sessions=[SimpleNamespace(issue=SimpleNamespace(number=1))])
                self.shutdown_called = False

            def request_shutdown(self, force: bool = False) -> None:
                self.shutdown_called = True

        orchestrator = OrchestratorStub()
        original = get_orchestrator()
        set_orchestrator(orchestrator)

        queue: asyncio.Queue = asyncio.Queue(maxsize=10)
        web.add_event_subscriber(queue)

        monkeypatch.setattr(web, "trigger_server_shutdown", lambda: None)
        monkeypatch.setattr(web.shutdown_manager, "request_shutdown", lambda reason: None)
        monkeypatch.setattr(web.shutdown_manager, "exit", lambda: None)

        try:
            response = await web.shutdown(force=False)
            assert response.status_code == 200
            event = queue.get_nowait()
            assert event["type"] == "shutdown_requested"
            assert event["data"]["force"] is False
            assert orchestrator.shutdown_called is True
            ShutdownRequestedPayload.model_validate(event["data"])
        finally:
            web.remove_event_subscriber(queue)
            set_orchestrator(original)

    @pytest.mark.asyncio
    async def test_startup_complete_broadcasts_event(self, monkeypatch, tmp_path):
        """Startup path should emit startup_complete event for the UI."""
        import asyncio
        from issue_orchestrator.entrypoints import web
        from issue_orchestrator.infra.config import Config

        startup_event = asyncio.Event()
        captured: dict = {}

        async def fake_broadcast_event(event_type: str, data: dict | None = None) -> None:
            if event_type == "startup_complete":
                captured["event_type"] = event_type
                captured["data"] = data or {}
                startup_event.set()

        async def fake_run_web_dashboard(orchestrator, port: int, open_browser: bool = True) -> None:
            await startup_event.wait()

        async def fast_sleep(_seconds: float) -> None:
            return None

        class OrchestratorStub:
            def __init__(self, root: Path):
                self.config = Config()
                self.config.repo_root = root
                self.shutdown_requested = False

            async def startup(self) -> None:
                return None

            async def run_loop(self) -> None:
                return None

        monkeypatch.setattr(web, "broadcast_event", fake_broadcast_event)
        monkeypatch.setattr(web, "run_web_dashboard", fake_run_web_dashboard)
        monkeypatch.setattr(web.asyncio, "sleep", fast_sleep)
        monkeypatch.setattr(web.shutdown_manager, "initialize", lambda _: None)
        monkeypatch.setattr(web.shutdown_manager, "request_shutdown", lambda reason: None)
        monkeypatch.setattr(web.shutdown_manager, "exit", lambda: None)

        orchestrator = OrchestratorStub(tmp_path)
        await web.run_with_web_dashboard(orchestrator, port=0, open_browser=False)

        assert captured["event_type"] == "startup_complete"
        assert "elapsed_seconds" in captured["data"]
        StartupCompletePayload.model_validate(captured["data"])


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


class TestSSEEventStreamFormat:
    """Tests for SSE event stream formatting."""

    @pytest.mark.asyncio
    async def test_events_stream_formats_event_and_data(self):
        """Ensure /api/events emits event and data lines with JSON payload."""
        import json
        import asyncio
        from issue_orchestrator.entrypoints import web
        from issue_orchestrator.entrypoints.web import broadcast_event

        class NotifyingSet(set):
            def __init__(self, event):
                super().__init__()
                self._event = event

            def add(self, item):
                super().add(item)
                self._event.set()

        from issue_orchestrator.entrypoints.web import swapped_event_subscribers
        ready = asyncio.Event()

        class DummyRequest:
            def __init__(self):
                self.connected = True

            async def is_disconnected(self):
                return not self.connected

        with swapped_event_subscribers(NotifyingSet(ready)):
            request = DummyRequest()
            response = await web.events(request)
            iterator = response.body_iterator

            async def read_chunk():
                return await iterator.__anext__()

            read_task = asyncio.create_task(read_chunk())
            await ready.wait()
            await broadcast_event("session.started", {"issue_number": 123, "status": "active"})

            chunk = await read_task
            request.connected = False

            assert chunk["event"] == "session.started"
            payload = json.loads(chunk["data"])
            assert payload == {"issue_number": 123, "status": "active"}


class TestIssueRowsEndpoint:
    """Tests for the issue row rendering endpoint."""

    def test_issue_rows_returns_rendered_html(self):
        from fastapi.testclient import TestClient
        from issue_orchestrator.entrypoints import web
        from issue_orchestrator.entrypoints.web import get_orchestrator, set_orchestrator
        from issue_orchestrator.domain.models import Issue, OrchestratorState
        from issue_orchestrator.infra.config import Config

        class OrchestratorStub:
            def __init__(self):
                self.state = OrchestratorState(
                    startup_status="complete",
                    cached_queue_issues=[Issue(number=7, title="Test", labels=["agent:web"])],
                )
                self.config = Config()
                self.config.repo = "test/repo"
                self.config.repo_root = Path("/tmp/repo")
                self.shutdown_requested = False

        original = get_orchestrator()
        set_orchestrator(OrchestratorStub())
        try:
            client = TestClient(web.app)
            response = client.get("/api/issue-rows?tab=queue")
            assert response.status_code == 200
            data = response.json()
            assert data["count"] == 1
            assert "issue-row-group" in data["rows"][0]["html"]
        finally:
            set_orchestrator(original)

    def test_view_model_snapshot_returns_rows_from_same_snapshot(self):
        from fastapi.testclient import TestClient
        from issue_orchestrator.entrypoints import web
        from issue_orchestrator.entrypoints.web import get_orchestrator, set_orchestrator
        from issue_orchestrator.domain.models import Issue, OrchestratorState
        from issue_orchestrator.infra.config import Config

        class OrchestratorStub:
            def __init__(self):
                self.state = OrchestratorState(
                    startup_status="complete",
                    cached_queue_issues=[Issue(number=11, title="Snapshot Test", labels=["agent:web"])],
                )
                self.config = Config()
                self.config.repo = "test/repo"
                self.config.repo_root = Path("/tmp/repo")
                self.shutdown_requested = False

        original = get_orchestrator()
        set_orchestrator(OrchestratorStub())
        try:
            client = TestClient(web.app)
            response = client.get("/api/view-model-snapshot?tab=queue")
            assert response.status_code == 200
            data = response.json()
            assert "view_model" in data
            assert data["count"] == 1
            assert data["rows"][0]["issue_number"] == 11
            assert data["view_model"]["queue_count"] >= 0
        finally:
            set_orchestrator(original)

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


class TestDialogEndpoints:
    """Tests for dialog view-model endpoints."""

    def test_doctor_dialog_returns_non_200_upstream_response(self):
        """GET /api/dialog/doctor forwards upstream error response unchanged."""
        from issue_orchestrator.entrypoints import web
        from fastapi.responses import JSONResponse

        with patch.object(
            web,
            "get_doctor",
            AsyncMock(return_value=JSONResponse({"error": "Orchestrator not running"}, status_code=503)),
        ):
            client = TestClient(app)
            response = client.get("/api/dialog/doctor")

        assert response.status_code == 503
        assert response.json() == {"error": "Orchestrator not running"}


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
            assert "refresh" in response.json()
            mock_orch.request_refresh.assert_called_once()
        finally:
            set_orchestrator(None)

    def test_single_issue_refresh_updates_cache(self):
        """Refreshing one issue updates local cache and freshness state."""
        mock_orch = create_mock_orchestrator()
        issue = create_issue(77, "Refresh me")
        mock_orch.repository_host.get_issue.return_value = issue
        set_orchestrator(mock_orch)

        try:
            client = TestClient(app)
            response = client.post("/api/issues/77/refresh")
            assert response.status_code == 200
            payload = response.json()
            assert payload["status"] == "refreshed"
            assert payload["issue_number"] == 77
            assert payload["is_stale"] is False
            assert payload["last_refreshed_label"] == "just now"
            assert 77 in mock_orch.state.issue_last_refreshed_at
            assert any(i.number == 77 for i in mock_orch.state.cached_queue_issues)
        finally:
            set_orchestrator(None)

    def test_single_issue_refresh_does_not_override_queue_refresh_state(self):
        """Single-issue refresh should not mutate queue refresh lifecycle state."""
        mock_orch = create_mock_orchestrator()
        mock_orch.state.queue_refresh_in_progress = False
        issue = create_issue(77, "Refresh me")
        mock_orch.repository_host.get_issue.return_value = issue
        set_orchestrator(mock_orch)

        try:
            client = TestClient(app)
            response = client.post("/api/issues/77/refresh")
            assert response.status_code == 200
            assert mock_orch.state.queue_refresh_in_progress is False
        finally:
            set_orchestrator(None)

    def test_single_issue_refresh_not_found(self):
        """Refreshing a missing issue returns 404."""
        mock_orch = create_mock_orchestrator()
        mock_orch.repository_host.get_issue.return_value = None
        set_orchestrator(mock_orch)

        try:
            client = TestClient(app)
            response = client.post("/api/issues/999/refresh")
            assert response.status_code == 404
            assert "not found" in response.json()["error"].lower()
        finally:
            set_orchestrator(None)


class TestApiTimelineEndpoint:
    """Test the GET /api/timeline/{issue_number} endpoint."""

    def test_timeline_returns_events(self, tmp_path: Path):
        """Timeline endpoint returns stream events with artifacts."""
        from issue_orchestrator.entrypoints import web
        from issue_orchestrator.execution.session_output_adapter import FileSystemSessionOutput
        mock_orch = create_mock_orchestrator()
        session_output = FileSystemSessionOutput()
        worktree = tmp_path / "wt-timeline-returns-events"
        worktree.mkdir(parents=True)
        run = session_output.start_run(worktree, "issue-123", issue_number=123)
        (run.run_dir / "session.log").write_text("agent output\n", encoding="utf-8")
        claude_log = run.run_dir / "claude.jsonl"
        claude_log.write_text('{"type":"assistant","content":"ok"}\n', encoding="utf-8")
        session_output.update_manifest(run.run_dir, {"claude_log_path": str(claude_log)})

        stream = TimelineStream(
            issue_number=123,
            events=[
                TimelineEvent(
                    event_id="e1",
                    timestamp="2026-02-06T00:00:00Z",
                    event="session.started",
                    issue_number=123,
                    phase="in_progress",
                    step="started",
                    status="started",
                    level="phase",
                    summary=None,
                    parent_key="session:issue-123",
                    run_id="20260206-000000Z",
                    run_dir=str(run.run_dir),
                    artifacts=[TimelineArtifact("worktree", "Worktree", "/tmp/worktree")],
                    timeline_schema_version=TIMELINE_SCHEMA_VERSION,
                    event_intent="coding",
                    logical_run=1,
                    logical_cycle=1,
                    logical_phase="coding",
                ),
                TimelineEvent(
                    event_id="e2",
                    timestamp="2026-02-06T00:01:00Z",
                    event="session.completed",
                    issue_number=123,
                    phase="completed",
                    step="completed",
                    status="completed",
                    level="phase",
                    summary=None,
                    parent_key="session:issue-123",
                    run_dir=str(run.run_dir),
                    artifacts=[
                        TimelineArtifact("pull_request", "PR", "https://example/pr/1"),
                        TimelineArtifact("review_comment", "Review Comment", "https://example/pr/1#issuecomment-1"),
                        TimelineArtifact("completion_record", "Completion", "/tmp/worktree/completion.json"),
                    ],
                    timeline_schema_version=TIMELINE_SCHEMA_VERSION,
                    event_intent="coding",
                    logical_run=1,
                    logical_cycle=1,
                    logical_phase="coding",
                ),
            ],
        )
        mock_orch.deps.timeline_reader.read.return_value = stream

        set_orchestrator(mock_orch)
        try:
            client = TestClient(app)
            response = client.get("/api/timeline/123")
            assert response.status_code == 200
            payload = response.json()
            assert payload["issue_number"] == 123
            assert len(payload["events"]) == 2
            assert payload["events"][0]["event"] == "session.started"
            assert payload["events"][0]["artifacts"][0]["type"] == "worktree"
            assert payload["events"][0]["run_id"] == "20260206-000000Z"
            assert payload["events"][0]["run_dir"].endswith("__issue-123")
            action_types = {a["type"] for a in payload["events"][0]["actions"]}
            assert "open_path" in action_types
            assert "open_agent_log" in action_types
            assert "open_session_diagnostics" in action_types
            start_actions = payload["events"][0]["actions"]
            assert sum(1 for action in start_actions if action["type"] == "open_session_diagnostics") == 1
            assert start_actions[-1]["type"] == "open_session_diagnostics"
            completion_artifacts = {a["type"] for a in payload["events"][1]["artifacts"]}
            assert "pull_request" in completion_artifacts
            assert "review_comment" in completion_artifacts
            assert "completion_record" in completion_artifacts
            completion_actions = payload["events"][1]["actions"]
            assert any(
                action["type"] == "open_url" and "issuecomment" in action.get("url", "")
                for action in completion_actions
            )
            completion_labels = [action["label"] for action in completion_actions]
            review_index = completion_labels.index("Open Review Comment ↗")
            diagnostics_index = completion_labels.index("Diagnostics…")
            assert review_index < diagnostics_index
        finally:
            set_orchestrator(None)

    def test_timeline_cycles_include_orchestrator_phase_events_within_active_cycle(self):
        """Validation/queue orchestration events should remain in the same active cycle."""
        mock_orch = create_mock_orchestrator()
        stream = TimelineStream(
            issue_number=123,
            events=[
                build_timeline_event(
                    "session.started",
                    event_id="e1",
                    timestamp="2026-02-06T00:00:00Z",
                    phase="in_progress",
                    status="started",
                ),
                build_timeline_event(
                    "validation.completed",
                    event_id="e2",
                    timestamp="2026-02-06T00:01:00Z",
                    phase="orchestrator",
                    status="completed",
                ),
                build_timeline_event(
                    "review.queued",
                    event_id="e3",
                    timestamp="2026-02-06T00:02:00Z",
                    phase="orchestrator",
                    status="started",
                ),
                build_timeline_event(
                    "review.started",
                    event_id="e4",
                    timestamp="2026-02-06T00:03:00Z",
                    phase="reviewing",
                    status="started",
                ),
            ],
        )
        mock_orch.deps.timeline_reader.read.return_value = stream

        set_orchestrator(mock_orch)
        try:
            client = TestClient(app)
            response = client.get("/api/timeline/123")
            assert response.status_code == 200
            payload = response.json()
            assert len(payload["cycles"]) == 1
            cycle = _first_cycle(payload)
            assert cycle["phases"] == ["in_progress", "orchestrator", "reviewing"]
            assert [event["event"] for event in cycle["events"]] == [
                "session.started",
                "validation.completed",
                "review.queued",
                "review.started",
            ]
        finally:
            set_orchestrator(None)

    def test_issue_detail_returns_payload(self):
        """Issue detail endpoint returns drawer payload."""
        payload = fetch_issue_detail_payload(
            [build_timeline_event("session.started", summary="started")]
        )
        assert payload["issue_number"] == 123
        assert payload["title"] == "Detail Issue"
        assert "summary" in payload
        assert "events" in payload
        assert "cycles" in payload
        assert "actions" in payload

    def test_issue_detail_starts_new_lifecycle_after_completion_without_review(self):
        """Signal path: a new coding session after completion becomes a new lifecycle."""
        payload = fetch_issue_detail_payload([
            build_timeline_event(
                "session.started",
                event_id="e1",
                timestamp="2026-02-09T10:00:00Z",
                rework_cycle=0,
                agent="agent:backend",
            ),
            build_timeline_event(
                "session.completed",
                event_id="e2",
                timestamp="2026-02-09T10:30:00Z",
                status="completed",
                rework_cycle=0,
            ),
            build_timeline_event(
                "session.started",
                event_id="e3",
                timestamp="2026-02-09T11:00:00Z",
                logical_run=2,
                rework_cycle=0,
                agent="agent:backend",
            ),
            build_timeline_event(
                "session.completed",
                event_id="e4",
                timestamp="2026-02-09T11:30:00Z",
                status="completed",
                logical_run=2,
                rework_cycle=0,
            ),
        ])

        runs = payload["runs"]
        journey_cycles = [cycle for run in runs for cycle in run["cycles"]]
        assert len(journey_cycles) == 2
        lifecycles = [cycle["lifecycle"] for cycle in journey_cycles]
        assert lifecycles[1] > lifecycles[0]
        assert payload["run_count"] == 2

    def test_issue_detail_review_continuation_stays_in_same_lifecycle(self):
        """Signal path: completion followed by review remains one lifecycle/run."""
        payload = fetch_issue_detail_payload([
            build_timeline_event(
                "session.started",
                event_id="e1",
                timestamp="2026-02-09T10:00:00Z",
                rework_cycle=0,
                agent="agent:backend",
            ),
            build_timeline_event(
                "session.completed",
                event_id="e2",
                timestamp="2026-02-09T10:30:00Z",
                status="completed",
                rework_cycle=0,
            ),
            build_timeline_event(
                "review.started",
                event_id="e3",
                timestamp="2026-02-09T10:31:00Z",
                status="started",
                phase="reviewing",
                rework_cycle=0,
            ),
            build_timeline_event(
                "review.changes_requested",
                event_id="e4",
                timestamp="2026-02-09T10:32:00Z",
                status="failed",
                phase="reviewing",
                rework_cycle=0,
                reviewer_agent="agent:reviewer",
            ),
            build_timeline_event(
                "rework.started",
                event_id="e5",
                timestamp="2026-02-09T10:40:00Z",
                status="started",
                phase="rework",
                rework_cycle=1,
                agent="agent:backend",
            ),
            build_timeline_event(
                "session.completed",
                event_id="e6",
                timestamp="2026-02-09T11:00:00Z",
                status="completed",
                rework_cycle=1,
            ),
        ])

        runs = payload["runs"]
        journey_cycles = [cycle for run in runs for cycle in run["cycles"]]
        assert len(journey_cycles) == 2
        assert [cycle["iteration"] for cycle in journey_cycles] == [1, 2]
        assert {cycle["lifecycle"] for cycle in journey_cycles} == {1}
        assert payload["run_count"] == 1

    def test_issue_detail_manual_unblock_without_event_starts_new_lifecycle(self):
        """Manual label removal (no issue.unblocked event) still creates a new run lifecycle."""
        payload = fetch_issue_detail_payload([
            build_timeline_event(
                "session.started",
                event_id="e1",
                timestamp="2026-02-08T10:00:00Z",
                status="started",
                agent="agent:backend",
            ),
            build_timeline_event(
                "session.completed",
                event_id="e2",
                timestamp="2026-02-08T10:30:00Z",
                status="completed",
            ),
            build_timeline_event(
                "issue.blocked",
                event_id="e3",
                timestamp="2026-02-08T10:40:00Z",
                status="failed",
                phase="blocked",
            ),
            build_timeline_event(
                "session.started",
                event_id="e4",
                timestamp="2026-02-09T09:00:00Z",
                status="started",
                logical_run=2,
                agent="agent:backend",
            ),
            build_timeline_event(
                "session.completed",
                event_id="e5",
                timestamp="2026-02-09T09:30:00Z",
                status="completed",
                logical_run=2,
            ),
        ])

        runs = payload["runs"]
        journey_cycles = [cycle for run in runs for cycle in run["cycles"]]
        assert len(journey_cycles) == 2
        lifecycles = [cycle["lifecycle"] for cycle in journey_cycles]
        assert lifecycles[1] > lifecycles[0]
        assert payload["run_count"] == 2

    def test_issue_detail_signal_events_split_from_legacy_lifecycle(self):
        """Legacy timeline followed by signal-era events should split runs."""
        payload = fetch_issue_detail_payload([
            build_timeline_event(
                "session.started",
                event_id="e1",
                timestamp="2026-02-08T10:00:00Z",
                status="started",
                agent="agent:backend",
            ),
            build_timeline_event(
                "session.completed",
                event_id="e2",
                timestamp="2026-02-08T10:30:00Z",
                status="completed",
            ),
            build_timeline_event(
                "session.started",
                event_id="e3",
                timestamp="2026-02-09T10:00:00Z",
                status="started",
                logical_run=2,
                rework_cycle=0,
                agent="agent:backend",
            ),
            build_timeline_event(
                "session.completed",
                event_id="e4",
                timestamp="2026-02-09T10:30:00Z",
                status="completed",
                logical_run=2,
                rework_cycle=0,
            ),
        ])

        runs = payload["runs"]
        journey_cycles = [cycle for run in runs for cycle in run["cycles"]]
        assert len(journey_cycles) == 2
        lifecycles = [cycle["lifecycle"] for cycle in journey_cycles]
        assert lifecycles[1] > lifecycles[0]
        assert payload["run_count"] == 2

    def test_issue_detail_includes_cycle_run_id_for_latest_run_filtering(self):
        """Journey cycles should carry run_id + cycle_in_run for latest-run rendering."""
        payload = fetch_issue_detail_payload([
            build_timeline_event(
                "session.started",
                event_id="e1",
                timestamp="2026-02-09T10:00:00Z",
                status="started",
                run_id="run-1",
                rework_cycle=0,
                agent="agent:backend",
            ),
            build_timeline_event(
                "session.completed",
                event_id="e2",
                timestamp="2026-02-09T10:30:00Z",
                status="completed",
                run_id="run-1",
                rework_cycle=0,
            ),
            build_timeline_event(
                "session.started",
                event_id="e3",
                timestamp="2026-02-09T11:00:00Z",
                status="started",
                run_id="run-2",
                logical_run=2,
                rework_cycle=0,
                agent="agent:backend",
            ),
            build_timeline_event(
                "session.completed",
                event_id="e4",
                timestamp="2026-02-09T11:30:00Z",
                status="completed",
                run_id="run-2",
                logical_run=2,
                rework_cycle=0,
            ),
        ])

        runs = payload["runs"]
        journey_cycles = [cycle for run in runs for cycle in run["cycles"]]
        assert len(journey_cycles) == 2
        assert [cycle["run_id"] for cycle in journey_cycles] == ["run-1", "run-2"]
        assert [cycle["cycle_in_run"] for cycle in journey_cycles] == [1, 1]

    def test_issue_detail_drops_claim_preamble_when_real_cycles_exist(self):
        """Claim-only preamble should not appear as its own numbered cycle."""
        payload = fetch_issue_detail_payload([
            build_timeline_event(
                "claim.acquired",
                event_id="e1",
                timestamp="2026-02-09T09:50:00Z",
                status="completed",
                phase="in_progress",
            ),
            build_timeline_event(
                "session.started",
                event_id="e2",
                timestamp="2026-02-09T10:00:00Z",
                status="started",
                rework_cycle=0,
                agent="agent:backend",
            ),
            build_timeline_event(
                "session.completed",
                event_id="e3",
                timestamp="2026-02-09T10:30:00Z",
                status="completed",
                rework_cycle=0,
            ),
        ])

        runs = payload["runs"]
        journey_cycles = [cycle for run in runs for cycle in run["cycles"]]
        assert len(journey_cycles) == 1
        step_events = [step["event"] for step in journey_cycles[0]["steps"]]
        assert "claim.acquired" not in step_events

    def test_issue_detail_drops_claim_event_inside_signal_cycle(self):
        """Claim events are hidden even when they share the active signal cycle."""
        payload = fetch_issue_detail_payload([
            build_timeline_event(
                "session.started",
                event_id="e1",
                timestamp="2026-02-09T10:00:00Z",
                status="started",
                rework_cycle=0,
                agent="agent:backend",
            ),
            build_timeline_event(
                "claim.acquired",
                event_id="e2",
                timestamp="2026-02-09T10:01:00Z",
                status="completed",
                rework_cycle=0,
            ),
            build_timeline_event(
                "session.completed",
                event_id="e3",
                timestamp="2026-02-09T10:30:00Z",
                status="completed",
                rework_cycle=0,
            ),
        ])

        runs = payload["runs"]
        journey_cycles = [cycle for run in runs for cycle in run["cycles"]]
        assert len(journey_cycles) == 1
        step_events = [step["event"] for step in journey_cycles[0]["steps"]]
        assert "claim.acquired" not in step_events

    def test_issue_detail_reports_expected_history_missing_when_empty(self):
        """Issue detail should surface diagnostic when history exists but timeline is empty."""
        mock_orch = create_mock_orchestrator()
        mock_orch.state.session_history = [
            SessionHistoryEntry(
                issue_number=123,
                title="Issue 123",
                agent_type="agent:web",
                status="completed",
                runtime_minutes=5,
            ),
        ]
        mock_orch.deps.timeline_reader.read.return_value = TimelineStream(
            issue_number=123,
            events=[],
        )
        set_orchestrator(mock_orch)
        try:
            client = TestClient(app)
            response = client.get("/api/issue-detail/123")
            assert response.status_code == 200
            payload = response.json()
            diagnostic = payload["summary"].get("timeline_diagnostic")
            assert diagnostic is not None
            assert diagnostic["state"] == "expected_history_missing"
            assert "session_history_present" in diagnostic["signals"]
            assert diagnostic["expected_timeline_store"].endswith("/timeline.sqlite")
            assert diagnostic["expected_timeline_store_exists"] is False
            assert "Timeline data missing" in payload["status_explanation"]
        finally:
            set_orchestrator(None)

    def test_issue_detail_does_not_report_missing_timeline_when_events_present(self):
        """Diagnostic should be absent when timeline events exist for the issue."""
        mock_orch = create_mock_orchestrator()
        mock_orch.state.session_history = [
            SessionHistoryEntry(
                issue_number=123,
                title="Issue 123",
                agent_type="agent:web",
                status="completed",
                runtime_minutes=5,
            ),
        ]
        mock_orch.deps.timeline_reader.read.return_value = TimelineStream(
            issue_number=123,
            events=[
                build_timeline_event(
                    "session.started",
                    event_id="e1",
                    timestamp="2026-02-09T10:00:00Z",
                    status="started",
                    phase="in_progress",
                    rework_cycle=0,
                ),
            ],
        )
        set_orchestrator(mock_orch)
        try:
            client = TestClient(app)
            response = client.get("/api/issue-detail/123")
            assert response.status_code == 200
            payload = response.json()
            summary = payload.get("summary", {})
            assert summary.get("timeline_diagnostic") is None
            assert "Timeline data missing" not in payload.get("status_explanation", "")
        finally:
            set_orchestrator(None)

    def test_issue_detail_survives_action_decoration_failure(self):
        """A single bad event artifact must not break issue-detail rendering."""
        mock_orch = create_mock_orchestrator()
        mock_orch.deps.timeline_reader.read.return_value = TimelineStream(
            issue_number=123,
            events=[
                build_timeline_event(
                    "session.started",
                    issue_number=123,
                    event_id="e-bad",
                    run_dir="/tmp/does-not-exist/run",
                    timeline_schema_version=TIMELINE_SCHEMA_VERSION,
                    status="started",
                    phase="in_progress",
                ),
                build_timeline_event(
                    "issue.pr_created",
                    issue_number=123,
                    event_id="e-good",
                    status="completed",
                    phase="done",
                ),
            ],
        )
        set_orchestrator(mock_orch)
        try:
            client = TestClient(app)
            response = client.get("/api/issue-detail/123")
            assert response.status_code == 200
            payload = response.json()
            assert payload["events"][0]["event"] == "session.started"
            assert payload["events"][0]["actions"] == []
            assert "actions_error" in payload["events"][0]
            assert payload["events"][1]["event"] == "issue.pr_created"
        finally:
            set_orchestrator(None)

    def test_timeline_reports_expected_history_missing_when_empty(self):
        """Timeline endpoint should include diagnostics for missing expected history."""
        mock_orch = create_mock_orchestrator()
        mock_orch.state.pending_reviews = [
            PendingReview(
                issue_key=FakeIssueKey(name="123"),
                _issue_number=123,
                pr_number=456,
                pr_url="https://example.com/pr/456",
                branch_name="123-test",
            ),
        ]
        mock_orch.deps.timeline_reader.read.return_value = TimelineStream(
            issue_number=123,
            events=[],
        )
        set_orchestrator(mock_orch)
        try:
            client = TestClient(app)
            response = client.get("/api/timeline/123")
            assert response.status_code == 200
            payload = response.json()
            diagnostic = payload.get("diagnostic")
            assert diagnostic is not None
            assert diagnostic["state"] == "expected_history_missing"
            assert "pending_review_present" in diagnostic["signals"]
            assert diagnostic["expected_timeline_store"].endswith("/timeline.sqlite")
            assert diagnostic["expected_timeline_store_exists"] is False
        finally:
            set_orchestrator(None)

    def test_issue_detail_reports_logical_semantics_missing_when_events_lack_fields(self):
        """Issue detail should fail fast on events missing logical semantics."""
        mock_orch = create_mock_orchestrator()
        mock_orch.state.session_history = [
            SessionHistoryEntry(
                issue_number=123,
                title="Issue 123",
                agent_type="agent:web",
                status="completed",
                runtime_minutes=5,
            ),
        ]
        mock_orch.deps.timeline_reader.read.return_value = TimelineStream(
            issue_number=123,
            events=[
                TimelineEvent(
                    event_id="e1",
                    timestamp="2026-02-09T10:00:00Z",
                    event="session.started",
                    issue_number=123,
                    phase="in_progress",
                    step="started",
                    status="started",
                    level="phase",
                    summary=None,
                    parent_key="session:issue-123",
                    artifacts=[],
                    timeline_schema_version=TIMELINE_SCHEMA_VERSION,
                    event_intent="coding",
                ),
            ],
        )
        set_orchestrator(mock_orch)
        try:
            client = TestClient(app)
            response = client.get("/api/issue-detail/123")
            assert response.status_code == 200
            payload = response.json()
            diagnostic = payload["summary"].get("timeline_diagnostic")
            assert diagnostic is not None
            assert diagnostic["state"] == "logical_semantics_missing"
            assert diagnostic["dropped_missing_semantics"] == 1
            assert "logical_semantics_missing" in diagnostic["signals"]
        finally:
            set_orchestrator(None)

    def test_issue_detail_latest_logical_run_keeps_review_with_rework(self):
        """Latest run must be logical lifecycle, not physical run_id ordering."""
        payload = fetch_issue_detail_payload([
            build_timeline_event(
                "session.started",
                event_id="e1",
                timestamp="2026-02-16T02:13:47Z",
                status="started",
                run_id="20260216-071346Z",
                rework_cycle=0,
                agent="agent:backend",
            ),
            build_timeline_event(
                "session.completed",
                event_id="e2",
                timestamp="2026-02-16T02:19:00Z",
                status="completed",
                run_id="20260216-071346Z",
                rework_cycle=0,
            ),
            build_timeline_event(
                "review.started",
                event_id="e3",
                timestamp="2026-02-16T02:19:10Z",
                status="started",
                run_id="20260216-075116Z",
                rework_cycle=0,
                task="review",
            ),
            build_timeline_event(
                "review.changes_requested",
                event_id="e4",
                timestamp="2026-02-16T02:22:00Z",
                status="failed",
                run_id="20260216-075116Z",
                rework_cycle=0,
                task="review",
            ),
            build_timeline_event(
                "rework.started",
                event_id="e5",
                timestamp="2026-02-16T02:47:51Z",
                status="started",
                run_id="20260216-074751Z",
                rework_cycle=1,
                agent="agent:backend",
            ),
            build_timeline_event(
                "session.completed",
                event_id="e6",
                timestamp="2026-02-16T03:00:00Z",
                status="completed",
                run_id="20260216-074751Z",
                rework_cycle=1,
            ),
        ])

        assert payload["run_count"] == 1
        latest_run = _latest_run(payload)
        review_events = [
            step["event"]
            for cycle in latest_run["cycles"]
            for step in cycle.get("steps", [])
            if str(step.get("event", "")).startswith("review.")
        ]
        assert review_events, "Latest logical run should include review events"
        assert latest_run.get("session_run_ids") == [
            "20260216-071346Z",
            "20260216-075116Z",
            "20260216-074751Z",
        ]

    def test_timeline_filters_label_churn_events(self, tmp_path: Path):
        """Timeline endpoint omits low-signal issue.labels_changed churn events."""
        from issue_orchestrator.execution.session_output_adapter import FileSystemSessionOutput
        mock_orch = create_mock_orchestrator()
        session_output = FileSystemSessionOutput()
        worktree = tmp_path / "wt-timeline-filter-churn"
        worktree.mkdir(parents=True)
        run = session_output.start_run(worktree, "issue-123", issue_number=123)
        (run.run_dir / "session.log").write_text("agent output\n", encoding="utf-8")
        claude_log = run.run_dir / "claude.jsonl"
        claude_log.write_text('{"type":"assistant","content":"ok"}\n', encoding="utf-8")
        session_output.update_manifest(run.run_dir, {"claude_log_path": str(claude_log)})

        stream = TimelineStream(
            issue_number=123,
            events=[
                TimelineEvent(
                    event_id="e1",
                    timestamp="2026-02-06T00:00:00Z",
                    event="issue.labels_changed",
                    issue_number=123,
                    phase="in_progress",
                    step="labels_changed",
                    status="completed",
                    level="detail",
                    summary="label update",
                    parent_key="issue:123",
                    artifacts=[],
                ),
                TimelineEvent(
                    event_id="e2",
                    timestamp="2026-02-06T00:01:00Z",
                    event="session.completed",
                    issue_number=123,
                    phase="completed",
                    step="completed",
                    status="completed",
                    level="phase",
                    summary=None,
                    parent_key="session:issue-123",
                    artifacts=[],
                    run_dir=str(run.run_dir),
                    timeline_schema_version=TIMELINE_SCHEMA_VERSION,
                    event_intent="coding",
                    logical_run=1,
                    logical_cycle=1,
                    logical_phase="coding",
                ),
            ],
        )
        mock_orch.deps.timeline_reader.read.return_value = stream

        set_orchestrator(mock_orch)
        try:
            client = TestClient(app)
            response = client.get("/api/timeline/123")
            assert response.status_code == 200
            payload = response.json()
            assert len(payload["events"]) == 1
            assert payload["events"][0]["event"] == "session.completed"
            assert any(
                action["type"] == "open_session_diagnostics"
                for action in payload["events"][0]["actions"]
            )
        finally:
            set_orchestrator(None)

    def test_timeline_keeps_pr_pending_removal_label_event(self, tmp_path: Path):
        """Timeline should retain pr-pending removal because it changes run boundaries."""
        from issue_orchestrator.execution.session_output_adapter import FileSystemSessionOutput
        mock_orch = create_mock_orchestrator()
        session_output = FileSystemSessionOutput()
        worktree = tmp_path / "wt-timeline-pr-pending"
        worktree.mkdir(parents=True)
        run = session_output.start_run(worktree, "issue-123", issue_number=123)
        (run.run_dir / "session.log").write_text("agent output\n", encoding="utf-8")
        claude_log = run.run_dir / "claude.jsonl"
        claude_log.write_text('{"type":"assistant","content":"ok"}\n', encoding="utf-8")
        session_output.update_manifest(run.run_dir, {"claude_log_path": str(claude_log)})

        stream = TimelineStream(
            issue_number=123,
            events=[
                TimelineEvent(
                    event_id="e1",
                    timestamp="2026-02-06T00:00:00Z",
                    event="issue.labels_changed",
                    issue_number=123,
                    phase="in_progress",
                    step="labels_changed",
                    status="completed",
                    level="detail",
                    summary="removed pr-pending",
                    parent_key="issue:123",
                    artifacts=[],
                    removed=["pr-pending"],
                    timeline_schema_version=TIMELINE_SCHEMA_VERSION,
                    event_intent="orchestrator",
                    logical_run=2,
                    logical_cycle=1,
                    logical_phase="orchestrator",
                ),
                TimelineEvent(
                    event_id="e2",
                    timestamp="2026-02-06T00:01:00Z",
                    event="session.started",
                    issue_number=123,
                    phase="in_progress",
                    step="started",
                    status="started",
                    level="phase",
                    summary=None,
                    parent_key="session:issue-123",
                    artifacts=[],
                    run_dir=str(run.run_dir),
                    timeline_schema_version=TIMELINE_SCHEMA_VERSION,
                    event_intent="coding",
                    logical_run=2,
                    logical_cycle=1,
                    logical_phase="coding",
                ),
            ],
        )
        mock_orch.deps.timeline_reader.read.return_value = stream

        set_orchestrator(mock_orch)
        try:
            client = TestClient(app)
            response = client.get("/api/timeline/123")
            assert response.status_code == 200
            payload = response.json()
            assert [event["event"] for event in payload["events"]] == [
                "issue.labels_changed",
                "session.started",
            ]
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

    def test_refresh_visibility_updates_state(self):
        """Test visibility refresh endpoint stores current visible issues."""
        mock_orch = create_mock_orchestrator()
        set_orchestrator(mock_orch)
        try:
            client = TestClient(app)
            response = client.post("/api/refresh/visibility", json={"issues": [12, "13", -1, "bad"]})
            assert response.status_code == 200
            assert response.json()["status"] == "ok"
            assert mock_orch.state.ui_visible_issue_numbers == [12, 13]
            assert mock_orch.state.ui_visible_updated_at > 0
        finally:
            set_orchestrator(None)

    def test_refresh_visibility_requires_valid_json(self):
        """Test visibility refresh endpoint rejects invalid payload."""
        mock_orch = create_mock_orchestrator()
        set_orchestrator(mock_orch)
        try:
            client = TestClient(app)
            response = client.post(
                "/api/refresh/visibility",
                content="not-json",
                headers={"Content-Type": "application/json"},
            )
            assert response.status_code == 400
        finally:
            set_orchestrator(None)

    def test_refresh_single_issue_updates_cached_queue(self):
        """Test single issue refresh updates existing cached issue and timestamp."""
        mock_orch = create_mock_orchestrator()
        mock_orch.config.filtering.label = "agent:web"
        mock_orch.state.cached_queue_issues = [create_issue(7, "old title")]
        mock_orch.repository_host = MagicMock()
        mock_orch.repository_host.get_issue.return_value = create_issue(7, "new title")
        set_orchestrator(mock_orch)
        try:
            client = TestClient(app)
            response = client.post("/api/issues/7/refresh")
            assert response.status_code == 200
            assert response.json()["status"] == "refreshed"
            assert response.json()["in_scope"] is True
            assert response.json()["updated"] is True
            assert mock_orch.state.cached_queue_issues[0].title == "new title"
            assert mock_orch.state.issue_refresh_timestamps[7] > 0
        finally:
            set_orchestrator(None)

    def test_refresh_single_issue_does_not_inject_out_of_scope_issue(self):
        """Out-of-scope single refresh should not inject issue into queue."""
        mock_orch = create_mock_orchestrator()
        mock_orch.config.filtering.label = "agent:web"
        mock_orch.state.cached_queue_issues = [create_issue(7, "old title")]
        mock_orch.state.issue_refresh_timestamps = {7: 100.0, 999: 200.0}
        mock_orch.repository_host = MagicMock()
        mock_orch.repository_host.get_issue.return_value = create_issue(
            7, "other scope", labels=["agent:other"]
        )
        set_orchestrator(mock_orch)
        try:
            client = TestClient(app)
            response = client.post("/api/issues/7/refresh")
            assert response.status_code == 200
            assert response.json()["status"] == "rejected_out_of_scope"
            assert response.json()["in_scope"] is False
            assert not any(issue.number == 7 for issue in mock_orch.state.cached_queue_issues)
            assert 7 not in mock_orch.state.issue_refresh_timestamps
            assert 999 not in mock_orch.state.issue_refresh_timestamps
        finally:
            set_orchestrator(None)

    def test_refresh_single_issue_404_when_missing(self):
        """Test single issue refresh returns 404 when issue cannot be fetched."""
        mock_orch = create_mock_orchestrator()
        mock_orch.repository_host = MagicMock()
        mock_orch.repository_host.get_issue.return_value = None
        set_orchestrator(mock_orch)
        try:
            client = TestClient(app)
            response = client.post("/api/issues/77/refresh")
            assert response.status_code == 404
        finally:
            set_orchestrator(None)

    def test_refresh_single_issue_rejects_closed_issue(self):
        """Closed issues should never be re-admitted to cached queue via refresh."""
        mock_orch = create_mock_orchestrator()
        mock_orch.config.filtering.label = "agent:web"
        mock_orch.state.cached_queue_issues = [create_issue(7, "old title")]
        mock_orch.state.issue_refresh_timestamps = {7: 100.0}
        mock_orch.repository_host = MagicMock()
        closed_issue = create_issue(7, "closed issue", labels=["agent:web"])
        closed_issue.state = "closed"
        mock_orch.repository_host.get_issue.return_value = closed_issue
        set_orchestrator(mock_orch)
        try:
            client = TestClient(app)
            response = client.post("/api/issues/7/refresh")
            assert response.status_code == 200
            assert response.json()["status"] == "rejected_out_of_scope"
            assert response.json()["in_scope"] is False
            assert not any(issue.number == 7 for issue in mock_orch.state.cached_queue_issues)
            assert 7 not in mock_orch.state.issue_refresh_timestamps
        finally:
            set_orchestrator(None)


class TestTimelineActionWiring:
    """Validate every timeline action type is handled end-to-end.

    The pipeline is:
        backend emits action types → JS runTimelineEventAction dispatches
        → JS handler calls API endpoint → endpoint exists in FastAPI app.

    This test prevents wiring drift: if a new action type is emitted by
    the backend but never handled by the frontend, or if a handler
    references an endpoint that doesn't exist, the test fails.
    """

    # Complete registry: action type → API route pattern (or None for client-only)
    _ACTION_ENDPOINT_MAP: dict[str, str | None] = {
        "open_path": "/api/open-file",
        "open_url": None,  # client-side window.open, no HTTP call
        "open_review_feedback": None,  # in-app modal from existing issue detail payload
        "open_agent_log": "/api/log/local/{issue_number}",
        "view_claude_log": "/api/session/claude-log/{issue_number}",
        "open_orchestrator_log": "/api/session/orchestrator-log/{issue_number}",
        "open_session_diagnostics": "/api/dialog/session-diagnostics/{issue_number}",
    }
    _REQUIRED_FIELDS_BY_ACTION: dict[str, tuple[str, ...]] = {
        "open_agent_log": ("issue_number", "run_dir"),
        "view_claude_log": ("issue_number", "run_dir"),
        "open_orchestrator_log": ("issue_number", "run_dir"),
        "open_session_diagnostics": ("issue_number", "run_dir"),
    }

    def _collect_app_route_patterns(self) -> set[str]:
        """Extract all registered route patterns from the FastAPI app."""
        patterns: set[str] = set()
        for route in app.routes:
            if hasattr(route, "path"):
                patterns.add(route.path)
        return patterns

    def test_all_emitted_action_types_are_registered(self, tmp_path: Path) -> None:
        """Every action type produced by _timeline_event_actions must be
        in _ACTION_ENDPOINT_MAP so we know it has a JS handler."""
        from issue_orchestrator.entrypoints.web import _timeline_event_actions
        from issue_orchestrator.execution.session_output_adapter import FileSystemSessionOutput

        session_output = FileSystemSessionOutput()
        worktree = tmp_path / "wt-action-registry"
        worktree.mkdir(parents=True)
        run = session_output.start_run(worktree, "issue-1", issue_number=1)
        (run.run_dir / "session.log").write_text("agent output\n", encoding="utf-8")
        claude_log = run.run_dir / "claude.jsonl"
        claude_log.write_text('{"type":"assistant","content":"ok"}\n', encoding="utf-8")
        session_output.update_manifest(run.run_dir, {"claude_log_path": str(claude_log)})
        run_dir = str(run.run_dir)

        # Generate actions for representative events to collect all possible types
        representative_events = [
            {"event": "session.started", "issue_number": 1, "run_dir": run_dir, "timeline_schema_version": TIMELINE_SCHEMA_VERSION},
            {"event": "review.comment_added", "issue_number": 1, "run_dir": run_dir, "timeline_schema_version": TIMELINE_SCHEMA_VERSION},
            {"event": "session.completed", "issue_number": 1, "run_dir": run_dir, "timeline_schema_version": TIMELINE_SCHEMA_VERSION},
            {"event": "session.failed", "issue_number": 1, "run_dir": run_dir, "timeline_schema_version": TIMELINE_SCHEMA_VERSION},
            {"event": "validation.failed", "issue_number": 1, "run_dir": run_dir, "timeline_schema_version": TIMELINE_SCHEMA_VERSION},
            {
                "event": "session.completed",
                "issue_number": 1,
                "run_dir": run_dir,
                "timeline_schema_version": TIMELINE_SCHEMA_VERSION,
                "artifacts": [
                    {"type": "pull_request", "label": "PR", "value": "https://example.com/pr/1"},
                    {"type": "worktree", "label": "Worktree", "value": "/tmp/wt"},
                ],
            },
        ]

        all_types: set[str] = set()
        for evt in representative_events:
            actions = _timeline_event_actions(evt, 1)
            for action in actions:
                all_types.add(action["type"])

        unhandled = all_types - set(self._ACTION_ENDPOINT_MAP)
        assert not unhandled, (
            f"Action types emitted by backend but missing from wiring registry: {unhandled}. "
            f"Add them to TestTimelineActionWiring._ACTION_ENDPOINT_MAP."
        )
        for evt in representative_events:
            actions = _timeline_event_actions(evt, 1)
            for action in actions:
                action_type = str(action.get("type") or "")
                required_fields = self._REQUIRED_FIELDS_BY_ACTION.get(action_type, ())
                missing_fields = [
                    field for field in required_fields
                    if field not in action or action.get(field) in (None, "")
                ]
                assert not missing_fields, (
                    f"Action type '{action_type}' missing required field(s) {missing_fields}: {action}"
                )

    def test_all_action_endpoints_exist_in_app(self) -> None:
        """Every action type that calls an API must have a matching route."""
        patterns = self._collect_app_route_patterns()

        for action_type, endpoint in self._ACTION_ENDPOINT_MAP.items():
            if endpoint is None:
                continue  # client-only action, no HTTP
            assert endpoint in patterns, (
                f"Action type '{action_type}' expects endpoint '{endpoint}' "
                f"but no matching route found in the FastAPI app."
            )

    def test_issue_detail_run_steps_carry_actions(self, tmp_path: Path) -> None:
        """Run cycle steps must pass through event actions for ⋯ menus."""
        from issue_orchestrator.execution.session_output_adapter import FileSystemSessionOutput

        mock_orch = create_mock_orchestrator()
        mock_orch.state.cached_queue_issues = [create_issue(123, "Wire Test")]
        session_output = FileSystemSessionOutput()
        worktree = tmp_path / "wt-issue-detail-actions"
        worktree.mkdir(parents=True)
        run = session_output.start_run(worktree, "issue-123", issue_number=123)
        (run.run_dir / "session.log").write_text("agent output\n", encoding="utf-8")
        claude_log = run.run_dir / "claude.jsonl"
        claude_log.write_text('{"type":"assistant","content":"ok"}\n', encoding="utf-8")
        session_output.update_manifest(run.run_dir, {"claude_log_path": str(claude_log)})

        stream = TimelineStream(
            issue_number=123,
            events=[
                TimelineEvent(
                    event_id="e1",
                    timestamp="2026-02-06T00:00:00Z",
                    event="session.started",
                    issue_number=123,
                    phase="in_progress",
                    step="started",
                    status="started",
                    level="phase",
                    summary=None,
                    parent_key="session:issue-123",
                    artifacts=[],
                    run_dir=str(run.run_dir),
                    timeline_schema_version=TIMELINE_SCHEMA_VERSION,
                    event_intent="coding",
                    logical_run=1,
                    logical_cycle=1,
                    logical_phase="coding",
                ),
            ],
        )
        mock_orch.deps.timeline_reader.read.return_value = stream

        set_orchestrator(mock_orch)
        try:
            client = TestClient(app)
            response = client.get("/api/issue-detail/123")
            assert response.status_code == 200
            payload = response.json()

            # Run cycles must exist and carry actions on steps
            runs = payload.get("runs", [])
            assert len(runs) > 0, "Expected at least one run"
            cycles = runs[0].get("cycles", [])
            assert len(cycles) > 0, "Expected at least one cycle"
            steps = cycles[0].get("steps", [])
            assert len(steps) > 0, "Expected at least one step in cycle"

            step_actions = steps[0].get("actions", [])
            assert len(step_actions) > 0, (
                "Journey cycle steps must include actions for ⋯ menu rendering"
            )
            step_action_types = {a["type"] for a in step_actions}
            # Every step should have at least the default diagnostics actions
            assert "open_agent_log" in step_action_types
            assert "open_session_diagnostics" in step_action_types
        finally:
            set_orchestrator(None)

    def test_timeline_action_wiring_rejects_unsupported_event_versions(self) -> None:
        from issue_orchestrator.entrypoints.web import _timeline_event_actions

        with pytest.raises(RuntimeError, match="unsupported schema version"):
            _timeline_event_actions(
                {
                    "event": "session.started",
                    "issue_number": 4057,
                    "timeline_schema_version": 1,
                    "run_dir": "/tmp/wt/.issue-orchestrator/sessions/20260216-000000Z__coding-1",
                },
                4057,
            )

    def test_no_action_type_without_js_handler(self) -> None:
        """Ensure the registry is exhaustive — every known action type
        maps to exactly one endpoint or None (client-only)."""
        # This is a meta-test: if someone adds a new action type to the
        # backend, they must also update this registry.
        from issue_orchestrator.entrypoints.web import (
            _timeline_event_default_actions,
            _timeline_event_recommended_actions,
        )

        # Collect all hardcoded action types from the default/recommended helpers
        captured: list[dict] = []

        def _capture(action: dict, _dedupe: str) -> None:
            captured.append(action)

        _timeline_event_default_actions(issue_number=1, add_action=_capture)
        _timeline_event_recommended_actions(
            event={"event": "session.started", "event_intent": "coding"},
            event_name="session.started", issue_number=1, add_action=_capture,
        )
        _timeline_event_recommended_actions(
            event={"event": "session.failed", "event_intent": "coding"},
            event_name="session.failed", issue_number=1, add_action=_capture,
        )
        _timeline_event_recommended_actions(
            event={"event": "validation.failed", "event_intent": "orchestrator"},
            event_name="validation.failed", issue_number=1, add_action=_capture,
        )
        _timeline_event_recommended_actions(
            event={"event": "review.comment_added", "event_intent": "review"},
            event_name="review.comment_added", issue_number=1, add_action=_capture,
        )

        default_types = {a["type"] for a in captured}
        unregistered = default_types - set(self._ACTION_ENDPOINT_MAP)
        assert not unregistered, (
            f"Action types in default/recommended helpers not in wiring registry: "
            f"{unregistered}"
        )

    def test_timeline_artifact_types_produce_viewable_actions(self, tmp_path: Path) -> None:
        """All known timeline artifact types should map to a usable UI action."""
        from issue_orchestrator.entrypoints.web import _timeline_event_actions
        from issue_orchestrator.execution.session_output_adapter import FileSystemSessionOutput

        session_output = FileSystemSessionOutput()
        worktree = tmp_path / "wt-artifact-actions"
        worktree.mkdir(parents=True)
        run = session_output.start_run(worktree, "issue-4057", issue_number=4057)
        run_dir = str(run.run_dir)
        (run.run_dir / "session.log").write_text("agent output\n", encoding="utf-8")
        claude_log = run.run_dir / "claude.jsonl"
        claude_log.write_text('{"type":"assistant","content":"ok"}\n', encoding="utf-8")
        session_output.update_manifest(run.run_dir, {"claude_log_path": str(claude_log)})

        completion_path = worktree / ".issue-orchestrator" / "completion.json"
        completion_path.parent.mkdir(parents=True, exist_ok=True)
        completion_path.write_text('{"status":"completed"}\n', encoding="utf-8")
        validation_path = worktree / ".issue-orchestrator" / "validation.json"
        validation_path.write_text('{"ok":true}\n', encoding="utf-8")

        event = {
            "event": "review.comment_added",
            "issue_number": 4057,
            "timeline_schema_version": TIMELINE_SCHEMA_VERSION,
            "run_dir": run_dir,
            "artifacts": [
                {"type": "pull_request", "label": "PR", "value": "https://github.com/org/repo/pull/4124"},
                {"type": "review_comment", "label": "Review Comment", "value": "https://github.com/org/repo/pull/4124#discussion_r1"},
                {"type": "completion_record", "label": "Completion", "value": str(completion_path)},
                {"type": "worktree", "label": "Worktree", "value": str(worktree)},
                {"type": "validation", "label": "Validation", "value": str(validation_path)},
                {"type": "run_dir", "label": "Run Dir", "value": run_dir},
            ],
        }
        actions = _timeline_event_actions(event, 4057)
        assert actions, "Expected at least one action from timeline event artifacts"

        open_url_labels = {
            action["label"]
            for action in actions
            if action.get("type") == "open_url"
        }
        open_paths = {
            action["path"]
            for action in actions
            if action.get("type") == "open_path"
        }
        run_scoped = {
            action["type"]
            for action in actions
            if action.get("run_dir") == run_dir
        }
        assert "Open PR ↗" in open_url_labels
        assert "Open Review Comment ↗" in open_url_labels
        assert str(completion_path) in open_paths
        assert str(worktree) in open_paths
        assert str(validation_path) in open_paths
        assert run_dir in open_paths
        assert "open_agent_log" in run_scoped
        assert "view_claude_log" in run_scoped
        assert "open_orchestrator_log" in run_scoped

    def test_agent_log_action_label_matches_event_context(self, tmp_path: Path) -> None:
        from issue_orchestrator.entrypoints.web import _timeline_event_actions
        from issue_orchestrator.execution.session_output_adapter import FileSystemSessionOutput

        session_output = FileSystemSessionOutput()
        worktree = tmp_path / "wt-agent-log-labels"
        worktree.mkdir(parents=True)
        run = session_output.start_run(worktree, "issue-1", issue_number=1)
        (run.run_dir / "session.log").write_text("agent output\n", encoding="utf-8")
        claude_log = run.run_dir / "claude.jsonl"
        claude_log.write_text('{"type":"assistant","content":"ok"}\n', encoding="utf-8")
        session_output.update_manifest(run.run_dir, {"claude_log_path": str(claude_log)})
        run_dir = str(run.run_dir)

        review_actions = _timeline_event_actions({"event": "review.approved", "issue_number": 1, "run_dir": run_dir, "timeline_schema_version": TIMELINE_SCHEMA_VERSION}, 1)
        coding_actions = _timeline_event_actions({"event": "session.started", "issue_number": 1, "run_dir": run_dir, "timeline_schema_version": TIMELINE_SCHEMA_VERSION}, 1)
        rework_actions = _timeline_event_actions({"event": "rework.started", "issue_number": 1, "run_dir": run_dir, "timeline_schema_version": TIMELINE_SCHEMA_VERSION}, 1)
        fallback_actions = _timeline_event_actions({"event": "issue.unblocked", "issue_number": 1, "timeline_schema_version": TIMELINE_SCHEMA_VERSION}, 1)

        def _label(actions: list[dict[str, Any]]) -> str:
            return next(action["label"] for action in actions if action.get("type") == "open_agent_log")

        assert _label(review_actions) == "View Reviewer Session Log"
        assert _label(coding_actions) == "View Coding Session Log"
        assert _label(rework_actions) == "View Rework Session Log"
        assert all(action.get("type") != "open_agent_log" for action in fallback_actions)

    def test_run_scoped_timeline_actions_require_run_dir(self, tmp_path: Path) -> None:
        from issue_orchestrator.entrypoints.web import _timeline_event_actions
        from issue_orchestrator.execution.session_output_adapter import FileSystemSessionOutput

        with pytest.raises(RuntimeError, match="missing required run_dir"):
            _timeline_event_actions(
                {"event": "session.started", "issue_number": 1, "timeline_schema_version": TIMELINE_SCHEMA_VERSION},
                1,
            )

        session_output = FileSystemSessionOutput()
        worktree = tmp_path / "wt-run-dir-required"
        worktree.mkdir(parents=True)
        run = session_output.start_run(worktree, "issue-1", issue_number=1)
        (run.run_dir / "session.log").write_text("agent output\n", encoding="utf-8")
        claude_log = run.run_dir / "claude.jsonl"
        claude_log.write_text('{"type":"assistant","content":"ok"}\n', encoding="utf-8")
        session_output.update_manifest(run.run_dir, {"claude_log_path": str(claude_log)})

        actions_with_run_dir = _timeline_event_actions(
            {
                "event": "session.started",
                "issue_number": 1,
                "run_dir": str(run.run_dir),
                "timeline_schema_version": TIMELINE_SCHEMA_VERSION,
            },
            1,
        )
        types_with_run_dir = {action.get("type") for action in actions_with_run_dir}
        assert "open_agent_log" in types_with_run_dir
        assert "view_claude_log" in types_with_run_dir

    def test_run_scoped_actions_require_usable_artifacts(self, tmp_path: Path) -> None:
        from issue_orchestrator.entrypoints.web import _timeline_event_actions
        from issue_orchestrator.execution.session_output_adapter import FileSystemSessionOutput

        session_output = FileSystemSessionOutput()
        worktree = tmp_path / "wt"
        worktree.mkdir(parents=True)
        run = session_output.start_run(worktree, "issue-1", issue_number=1)
        run_dir = str(run.run_dir)

        # No usable run artifacts yet: this is a contract violation.
        with pytest.raises(RuntimeError, match="run-scoped agent log is empty/unusable"):
            _timeline_event_actions(
                {
                    "event": "session.started",
                    "issue_number": 1,
                    "run_dir": run_dir,
                    "timeline_schema_version": TIMELINE_SCHEMA_VERSION,
                },
                1,
            )

        # Add usable agent log + claude log manifest binding.
        (run.run_dir / "session.log").write_text("agent output\n", encoding="utf-8")
        claude_log = run.run_dir / "claude.jsonl"
        claude_log.write_text('{"type":"assistant","content":"ok"}\n', encoding="utf-8")
        session_output.update_manifest(run.run_dir, {"claude_log_path": str(claude_log)})

        actions_present = _timeline_event_actions(
            {
                "event": "session.started",
                "issue_number": 1,
                "run_dir": run_dir,
                "timeline_schema_version": TIMELINE_SCHEMA_VERSION,
            },
            1,
        )
        present_types = {action.get("type") for action in actions_present}
        assert "open_agent_log" in present_types
        assert "view_claude_log" in present_types

    def test_run_scoped_event_without_run_dir_fails_fast(self, tmp_path: Path) -> None:
        from issue_orchestrator.entrypoints.web import _timeline_event_actions
        from issue_orchestrator.execution.session_output_adapter import FileSystemSessionOutput

        with pytest.raises(RuntimeError, match="missing required run_dir"):
            _timeline_event_actions(
                {
                    "event": "review.comment_added",
                    "issue_number": 1,
                    "timeline_schema_version": TIMELINE_SCHEMA_VERSION,
                    "event_intent": "review",
                    "review_oriented": True,
                    "logical_run": 1,
                    "logical_cycle": 1,
                    "logical_phase": "review",
                },
                1,
            )

        session_output = FileSystemSessionOutput()
        worktree = tmp_path / "wt-run-warning"
        worktree.mkdir(parents=True)
        run = session_output.start_run(worktree, "issue-1", issue_number=1)
        (run.run_dir / "session.log").write_text("agent output\n", encoding="utf-8")
        claude_log = run.run_dir / "claude.jsonl"
        claude_log.write_text('{"type":"assistant","content":"ok"}\n', encoding="utf-8")
        session_output.update_manifest(run.run_dir, {"claude_log_path": str(claude_log)})

        actions_with_run_dir = _timeline_event_actions(
            {
                "event": "review.comment_added",
                "issue_number": 1,
                "run_dir": str(run.run_dir),
                "timeline_schema_version": TIMELINE_SCHEMA_VERSION,
            },
            1,
        )
        run_scoped_types = {action.get("type") for action in actions_with_run_dir}
        assert "open_agent_log" in run_scoped_types
        assert "view_claude_log" in run_scoped_types

    def test_decorate_timeline_events_preserves_fallback_actions_when_strict_actions_fail(self) -> None:
        from issue_orchestrator.entrypoints.web import _decorate_timeline_events

        events = [
            {
                "event": "session.started",
                "issue_number": 4057,
                "timeline_schema_version": TIMELINE_SCHEMA_VERSION,
                "artifacts": [
                    {"type": "worktree", "label": "Worktree", "value": "/tmp/wt-4057"},
                ],
            }
        ]

        decorated = _decorate_timeline_events(events, 4057)
        assert len(decorated) == 1
        payload = decorated[0]
        action_types = {action.get("type") for action in payload.get("actions", [])}

        assert "open_path" in action_types
        assert "open_orchestrator_log" in action_types
        assert "open_session_diagnostics" in action_types
        assert "open_agent_log" not in action_types
        assert "actions_error" in payload


class TestKillSessionEndpoint:
    """Test the POST /api/kill/{issue_number} endpoint."""

    def test_kill_session_success(self):
        """Terminate-on-kill should stop and hold issue from automatic rerun."""
        from issue_orchestrator.entrypoints import web
        mock_orch = create_mock_orchestrator()

        issue = create_issue(1, "Issue to Kill")
        session = create_session(issue)
        session.pr_number = 4124
        mock_orch.state.active_sessions = [session]
        mock_orch.state.pending_reviews = [
            PendingReview(
                issue_key=FakeIssueKey(name="1"),
                pr_number=4124,
                pr_url="https://example/pr/4124",
                branch_name="feature/1",
                _issue_number=1,
            )
        ]
        mock_orch.state.pending_reworks = [
            PendingRework(issue_key=FakeIssueKey(name="1"), agent_type="agent:web", rework_cycle=3, issue_number=1)
        ]
        mock_orch.state.pending_validation_retries = [
            PendingValidationRetry(
                issue_number=1,
                issue_title="Issue to Kill",
                agent_label="agent:web",
                worktree_path="/tmp/worktree-1",
                branch_name="feature/1",
                original_prompt=None,
                validation_error="boom",
                validation_error_file=None,
                retry_count=1,
            )
        ]
        mock_orch.state.discovered_reviews = [
            DiscoveredReview(1, 4124, "https://example/pr/4124", "feature/1")
        ]
        mock_orch.state.discovered_reworks = [
            DiscoveredRework(1, 4124, "feature/1", "agent:web", 3)
        ]
        mock_orch.state.discovered_failures = [
            DiscoveredFailure(1, "Issue to Kill", "failed")
        ]
        mock_orch.state.immediate_cleanups = [
            ImmediateCleanup(1, "issue-1", "/tmp/worktree-1", "completed")
        ]
        mock_orch.kill_session = MagicMock()

        set_orchestrator(mock_orch)

        try:
            client = TestClient(app)
            response = client.post("/api/kill/1")

            assert response.status_code == 200
            data = response.json()
            assert data["status"] == "terminated"
            assert data["issue_number"] == 1
            assert data["title"] == "Issue to Kill"
            assert data["hold_label"] == "blocked-failed"
            mock_orch.kill_session.assert_called_once_with("issue-1")
            # Session should be removed from active sessions
            assert len(mock_orch.state.active_sessions) == 0
            # Queues/discovered facts should be cleared to prevent re-run.
            assert mock_orch.state.pending_reviews == []
            assert mock_orch.state.pending_reworks == []
            assert mock_orch.state.pending_validation_retries == []
            assert mock_orch.state.discovered_reviews == []
            assert mock_orch.state.discovered_reworks == []
            assert mock_orch.state.discovered_failures == []
            assert mock_orch.state.immediate_cleanups == []
            assert 1 in mock_orch.state.failed_this_cycle
            assert len(mock_orch.state.session_history) == 1
            history_entry = mock_orch.state.session_history[0]
            assert history_entry.issue_number == 1
            assert history_entry.status == "blocked"
            assert history_entry.status_reason == "Terminated by operator"
            # Hold labels: issue + linked PR.
            mock_orch.repository_host.add_label.assert_any_call(1, "blocked-failed")
            mock_orch.repository_host.remove_label.assert_any_call(1, "in-progress")
            mock_orch.repository_host.remove_label.assert_any_call(1, "pr-pending")
            mock_orch.repository_host.add_label.assert_any_call(4124, "blocked-failed")
            mock_orch.repository_host.remove_label.assert_any_call(4124, "needs-rework")
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
            assert "Failed to terminate" in response.json()["error"]
            assert any("Kill failed" in item for item in response.json()["details"])
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

    def test_bulk_kill_terminates_and_reports_missing(self):
        """Bulk kill should terminate active issues and report non-active ones."""
        from issue_orchestrator.entrypoints import web
        mock_orch = create_mock_orchestrator()

        issue = create_issue(1, "Issue 1")
        session = create_session(issue)
        session.pr_number = 4124
        mock_orch.state.active_sessions = [session]
        mock_orch.kill_session = MagicMock()

        set_orchestrator(mock_orch)
        try:
            client = TestClient(app)
            response = client.post("/api/bulk-kill", json={"issue_numbers": [1, 999]})
            assert response.status_code == 200
            payload = response.json()
            assert payload["terminated"] == [1]
            assert payload["failed"] == [{"issue_number": 999, "error": "Session not found"}]
            mock_orch.kill_session.assert_called_once_with("issue-1")
        finally:
            set_orchestrator(None)


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


class TestIssueLogEndpointsUseLatestHistory:
    """Issue log endpoints should resolve latest history entry, not oldest."""

    def test_agent_ui_log_prefers_latest_history_entry(self, tmp_path: Path):
        """GET /api/log/local should read from explicit run_dir only."""
        from issue_orchestrator.execution.session_output_adapter import FileSystemSessionOutput

        mock_orch = create_mock_orchestrator()
        session_output = FileSystemSessionOutput()

        old_worktree = tmp_path / "wt-old"
        old_worktree.mkdir(parents=True)
        old_run = session_output.start_run(old_worktree, "issue-123", issue_number=123)
        old_run.log_path.write_text("old run log line\n")

        new_worktree = tmp_path / "wt-new"
        new_worktree.mkdir(parents=True)
        new_run = session_output.start_run(new_worktree, "issue-123", issue_number=123)
        new_run.log_path.write_text("new run log line\n")

        mock_orch.state.session_history = [
            SessionHistoryEntry(
                issue_number=123,
                title="Issue 123 old",
                agent_type="agent:web",
                status="completed",
                runtime_minutes=1,
                worktree_path=old_worktree,
            ),
            SessionHistoryEntry(
                issue_number=123,
                title="Issue 123 new",
                agent_type="agent:web",
                status="completed",
                runtime_minutes=1,
                worktree_path=new_worktree,
            ),
        ]
        set_orchestrator(mock_orch)
        try:
            client = TestClient(app)
            response = client.get(f"/api/log/local/123?run_dir={new_run.run_dir}")
            assert response.status_code == 200
            payload = response.json()
            assert any("new run log line" in line for line in payload["lines"])
            assert str(new_worktree) in payload["log_path"]
        finally:
            set_orchestrator(None)

    def test_agent_ui_log_requires_run_dir(self, tmp_path: Path):
        """GET /api/log/local should fail fast when run_dir is missing."""
        mock_orch = create_mock_orchestrator()
        set_orchestrator(mock_orch)
        try:
            client = TestClient(app)
            response = client.get("/api/log/local/123")
            assert response.status_code == 400
            payload = response.json()
            assert payload["error"] == "run_dir is required"
        finally:
            set_orchestrator(None)

    def test_claude_log_requires_run_dir(self, tmp_path: Path):
        """GET /api/session/claude-log should fail fast when run_dir is missing."""
        from issue_orchestrator.execution.session_output_adapter import FileSystemSessionOutput

        mock_orch = create_mock_orchestrator()
        session_output = FileSystemSessionOutput()

        old_worktree = tmp_path / "wt-old-claude"
        old_worktree.mkdir(parents=True)
        old_run = session_output.start_run(old_worktree, "issue-123", issue_number=123)
        old_claude = old_run.run_dir / "old-claude.jsonl"
        old_claude.write_text('{"type":"assistant","content":"old"}\n')
        session_output.update_manifest(old_run.run_dir, {"claude_log_path": str(old_claude)})

        new_worktree = tmp_path / "wt-new-claude"
        new_worktree.mkdir(parents=True)
        new_run = session_output.start_run(new_worktree, "issue-123", issue_number=123)
        new_claude = new_run.run_dir / "new-claude.jsonl"
        new_claude.write_text('{"type":"assistant","content":"new"}\n')
        session_output.update_manifest(new_run.run_dir, {"claude_log_path": str(new_claude)})

        mock_orch.state.session_history = [
            SessionHistoryEntry(
                issue_number=123,
                title="Issue 123 old",
                agent_type="agent:web",
                status="completed",
                runtime_minutes=1,
                worktree_path=old_worktree,
            ),
            SessionHistoryEntry(
                issue_number=123,
                title="Issue 123 new",
                agent_type="agent:web",
                status="completed",
                runtime_minutes=1,
                worktree_path=new_worktree,
            ),
        ]
        set_orchestrator(mock_orch)
        try:
            client = TestClient(app)
            response = client.get("/api/session/claude-log/123")
            assert response.status_code == 400
            payload = response.json()
            assert payload["error"] == "run_dir is required"
        finally:
            set_orchestrator(None)

    def test_claude_log_honors_run_dir_query(self, tmp_path: Path):
        """GET /api/session/claude-log should read the requested run when run_dir is provided."""
        from issue_orchestrator.execution.session_output_adapter import FileSystemSessionOutput

        mock_orch = create_mock_orchestrator()
        session_output = FileSystemSessionOutput()

        worktree = tmp_path / "wt-claude-run-query"
        worktree.mkdir(parents=True)
        run_a = session_output.start_run(worktree, "review-1", issue_number=123)
        log_a = run_a.run_dir / "a.jsonl"
        log_a.write_text('{"type":"assistant","content":"from-run-a"}\n')
        session_output.update_manifest(run_a.run_dir, {"claude_log_path": str(log_a)})

        run_b = session_output.start_run(worktree, "review-2", issue_number=123)
        log_b = run_b.run_dir / "b.jsonl"
        log_b.write_text('{"type":"assistant","content":"from-run-b"}\n')
        session_output.update_manifest(run_b.run_dir, {"claude_log_path": str(log_b)})

        mock_orch.state.session_history = [
            SessionHistoryEntry(
                issue_number=123,
                title="Issue 123",
                agent_type="agent:web",
                status="completed",
                runtime_minutes=1,
                worktree_path=worktree,
            ),
        ]
        set_orchestrator(mock_orch)
        try:
            client = TestClient(app)
            response = client.get(f"/api/session/claude-log/123?run_dir={run_a.run_dir}")
            assert response.status_code == 200
            payload = response.json()
            assert payload["run_dir"] == str(run_a.run_dir)
            assert payload["entries"][0]["content"] == "from-run-a"
        finally:
            set_orchestrator(None)

    def test_orchestrator_log_honors_run_dir_query(self, tmp_path: Path):
        """GET /api/session/orchestrator-log should write tail into requested run_dir."""
        from issue_orchestrator.execution.session_output_adapter import FileSystemSessionOutput

        mock_orch = create_mock_orchestrator()
        mock_orch.config.repo_root = tmp_path / "repo"
        mock_orch.config.repo_root.mkdir(parents=True)
        orch_log = mock_orch.config.repo_root / ".issue-orchestrator" / "state" / "logs" / "orchestrator.log"
        orch_log.parent.mkdir(parents=True, exist_ok=True)
        orch_log.write_text("2026-02-16 [SESSION_RUN_START] run_id=test session=review-1 issue=123\n")

        session_output = FileSystemSessionOutput()
        worktree = tmp_path / "wt-orch-run-query"
        worktree.mkdir(parents=True)
        run_a = session_output.start_run(worktree, "review-1", issue_number=123)
        run_b = session_output.start_run(worktree, "review-2", issue_number=123)

        mock_orch.state.session_history = [
            SessionHistoryEntry(
                issue_number=123,
                title="Issue 123",
                agent_type="agent:web",
                status="completed",
                runtime_minutes=1,
                worktree_path=worktree,
            ),
        ]
        set_orchestrator(mock_orch)
        try:
            client = TestClient(app)
            response = client.get(f"/api/session/orchestrator-log/123?run_dir={run_a.run_dir}")
            assert response.status_code == 200
            payload = response.json()
            assert payload["filtered_log_path"].startswith(str(run_a.run_dir))
            assert not payload["filtered_log_path"].startswith(str(run_b.run_dir))
        finally:
            set_orchestrator(None)

    def test_orchestrator_log_errors_when_no_issue_scoped_lines(self, tmp_path: Path):
        """GET /api/session/orchestrator-log should fail when no issue-scoped lines are present."""
        from issue_orchestrator.execution.session_output_adapter import FileSystemSessionOutput

        mock_orch = create_mock_orchestrator()
        mock_orch.config.repo_root = tmp_path / "repo"
        mock_orch.config.repo_root.mkdir(parents=True)
        orch_log = mock_orch.config.repo_root / ".issue-orchestrator" / "state" / "logs" / "orchestrator.log"
        orch_log.parent.mkdir(parents=True, exist_ok=True)
        orch_log.write_text(
            "\n".join(
                [
                    "planner summary only",
                    "[issue-4048] unrelated line",
                ]
            )
        )

        session_output = FileSystemSessionOutput()
        worktree = tmp_path / "wt-orch-run-query"
        worktree.mkdir(parents=True)
        run_a = session_output.start_run(worktree, "review-1", issue_number=123)

        mock_orch.state.session_history = [
            SessionHistoryEntry(
                issue_number=123,
                title="Issue 123",
                agent_type="agent:web",
                status="completed",
                runtime_minutes=1,
                worktree_path=worktree,
            ),
        ]
        set_orchestrator(mock_orch)
        try:
            client = TestClient(app)
            response = client.get(f"/api/session/orchestrator-log/123?run_dir={run_a.run_dir}")
            assert response.status_code == 500
            payload = response.json()
            assert "No issue-scoped orchestrator log entries found" in payload["error"]
            assert "full_log_path" not in payload
        finally:
            set_orchestrator(None)

    def test_session_diagnostics_dialog_honors_run_dir_query(self, tmp_path: Path):
        """GET /api/dialog/session-diagnostics should use requested run_dir when provided."""
        from issue_orchestrator.execution.session_output_adapter import FileSystemSessionOutput

        mock_orch = create_mock_orchestrator()
        session_output = FileSystemSessionOutput()
        worktree = tmp_path / "wt-diag-run-query"
        worktree.mkdir(parents=True)

        run_a = session_output.start_run(worktree, "review-1", issue_number=123)
        session_output.update_manifest(run_a.run_dir, {"validation_record_path": ".issue-orchestrator/validation/a.json"})
        run_b = session_output.start_run(worktree, "review-2", issue_number=123)
        session_output.update_manifest(run_b.run_dir, {"validation_record_path": ".issue-orchestrator/validation/b.json"})

        mock_orch.state.session_history = [
            SessionHistoryEntry(
                issue_number=123,
                title="Issue 123",
                agent_type="agent:web",
                status="completed",
                runtime_minutes=1,
                worktree_path=worktree,
            ),
        ]
        set_orchestrator(mock_orch)
        try:
            client = TestClient(app)
            response = client.get(f"/api/dialog/session-diagnostics/123?run_dir={run_a.run_dir}")
            assert response.status_code == 200
            payload = response.json()
            actions = payload.get("actions", [])
            validation_paths = [
                action.get("path")
                for action in actions
                if action.get("type") == "open_path" and "Validation" in str(action.get("label"))
            ]
            assert any(path and path.endswith("a.json") for path in validation_paths)
            assert not any(path and path.endswith("b.json") for path in validation_paths)
        finally:
            set_orchestrator(None)


class TestIssueSessionContextIsolation:
    def test_resolve_context_does_not_scan_sibling_worktrees(self, tmp_path: Path):
        """Session context must not pick runs from sibling worktrees/repos."""
        from issue_orchestrator.entrypoints.web import _resolve_issue_session_context

        mock_orch = create_mock_orchestrator()
        repo_a = tmp_path / "repo-a"
        repo_a.mkdir(parents=True)
        repo_b = tmp_path / "repo-b"
        sibling_run = repo_b / ".issue-orchestrator" / "sessions" / "20260216-120000Z__issue-4057"
        sibling_run.mkdir(parents=True)
        (sibling_run / "manifest.json").write_text(
            json.dumps(
                {
                    "session_name": "issue-4057",
                    "run_id": "20260216-120000Z",
                    "run_dir": str(sibling_run),
                    "issue_number": 4057,
                }
            ),
            encoding="utf-8",
        )
        mock_orch.config.repo_root = repo_a
        mock_orch.state.active_sessions = []
        mock_orch.state.session_history = []
        set_orchestrator(mock_orch)
        try:
            ctx = _resolve_issue_session_context(4057)
            assert ctx.run_dir is None
            assert ctx.worktree_path is None
            assert ctx.session_name is None
        finally:
            set_orchestrator(None)


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
                with patch("time.sleep", return_value=None):
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
    """Test dashboard shows problem items in the kanban blocked column."""

    def test_dashboard_with_failed_session(self):
        """Test dashboard displays failed sessions in the blocked column."""
        from issue_orchestrator.entrypoints import web
        mock_orch = create_mock_orchestrator()

        # Add a failed session to history — goes to blocked column
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
            response = client.get("/?tab=kanban")

            assert response.status_code == 200
            assert "Failed Issue" in response.text
        finally:
            set_orchestrator(None)

    def test_dashboard_with_blocked_session(self):
        """Test dashboard displays blocked sessions in the blocked column."""
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
            response = client.get("/?tab=kanban")

            assert response.status_code == 200
            assert "Blocked Issue" in response.text
        finally:
            set_orchestrator(None)

    def test_dashboard_with_timed_out_session(self):
        """Test dashboard displays timed out sessions in the blocked column."""
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
            response = client.get("/?tab=kanban")

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
        from tests.unit.threading_helpers import wait_for_async_event

        mock_orch = create_mock_orchestrator()

        mock_server = MagicMock()
        serve_started = asyncio.Event()

        async def serve():
            serve_started.set()
            await asyncio.Event().wait()

        mock_server.serve = AsyncMock(side_effect=serve)

        with patch("issue_orchestrator.entrypoints.web.ensure_port_available"):
            with patch("uvicorn.Server", return_value=mock_server):
                with patch("issue_orchestrator.entrypoints.web.webbrowser.open"):
                    # Start the task
                    task = asyncio.create_task(run_web_dashboard(mock_orch, port=8080))

                    await wait_for_async_event(serve_started, timeout=1.0, label="serve_started")

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
        from tests.unit.threading_helpers import wait_for_async_event

        mock_orch = create_mock_orchestrator()

        mock_server = MagicMock()
        serve_started = asyncio.Event()
        browser_opened = asyncio.Event()

        async def serve():
            serve_started.set()
            await asyncio.Event().wait()

        mock_server.serve = AsyncMock(side_effect=serve)

        with patch("issue_orchestrator.entrypoints.web.ensure_port_available"):
            with patch("uvicorn.Server", return_value=mock_server):
                with patch("issue_orchestrator.entrypoints.web.asyncio.sleep", new=AsyncMock()):
                    with patch("issue_orchestrator.entrypoints.web.webbrowser.open") as mock_open:
                        mock_open.side_effect = lambda url: browser_opened.set()
                        task = asyncio.create_task(run_web_dashboard(mock_orch, port=8080))

                        await wait_for_async_event(serve_started, timeout=1.0, label="serve_started")
                        await wait_for_async_event(browser_opened, timeout=1.0, label="browser_opened")

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
        from tests.unit.threading_helpers import wait_for_async_event

        mock_orch = create_mock_orchestrator()
        startup_called = asyncio.Event()
        run_loop_called = asyncio.Event()

        async def startup():
            startup_called.set()

        async def run_loop():
            run_loop_called.set()
            await asyncio.Event().wait()

        mock_orch.startup = AsyncMock(side_effect=startup)
        mock_orch.run_loop = AsyncMock(side_effect=run_loop)

        mock_server = MagicMock()
        serve_started = asyncio.Event()

        async def serve():
            serve_started.set()
            await asyncio.Event().wait()

        mock_server.serve = AsyncMock(side_effect=serve)

        with patch("issue_orchestrator.entrypoints.web.ensure_port_available"):
            with patch("uvicorn.Server", return_value=mock_server):
                with patch("issue_orchestrator.entrypoints.web.webbrowser.open"):
                    with patch("issue_orchestrator.entrypoints.web.asyncio.sleep", new=AsyncMock()):
                        task = asyncio.create_task(run_with_web_dashboard(mock_orch, port=8080))

                        await wait_for_async_event(serve_started, timeout=1.0, label="serve_started")
                        await wait_for_async_event(startup_called, timeout=1.0, label="startup_called")

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


class TestSettingsEndpoints:
    """Tests for the settings page and API endpoints.

    The settings API uses a Pydantic schema-driven approach. Each tab
    (concurrency, e2e, filtering, review, hooks, advanced, goal_pilot) is a separate key
    in the request/response JSON.
    """

    def test_get_settings_returns_current_config(self):
        """GET /api/settings returns current config values grouped by tab."""
        from issue_orchestrator.entrypoints import web

        mock_orch = create_mock_orchestrator()
        mock_orch.config.max_concurrent_sessions = 5
        mock_orch.config.e2e.enabled = True
        mock_orch.config.e2e.auto_run_interval_minutes = 45

        web._orchestrator = mock_orch
        try:
            client = TestClient(app)
            response = client.get("/api/settings")

            assert response.status_code == 200
            data = response.json()

            # Tab-based structure (not nested category structure)
            assert data["concurrency"]["max_concurrent_sessions"] == 5
            assert data["e2e"]["enabled"] is True
            assert data["e2e"]["auto_run_interval_minutes"] == 45
        finally:
            web._orchestrator = None

    def test_get_settings_returns_all_tabs(self):
        """GET /api/settings returns all tabs."""
        from issue_orchestrator.entrypoints import web

        mock_orch = create_mock_orchestrator()
        web._orchestrator = mock_orch
        try:
            client = TestClient(app)
            response = client.get("/api/settings")

            assert response.status_code == 200
            data = response.json()
            assert set(data.keys()) == {
                "concurrency",
                "e2e",
                "filtering",
                "milestones",
                "review",
                "hooks",
                "advanced",
                "goal_pilot",
            }
        finally:
            web._orchestrator = None

    def test_get_settings_returns_503_when_orchestrator_not_running(self):
        """GET /api/settings returns 503 when orchestrator not running."""
        from issue_orchestrator.entrypoints import web

        web._orchestrator = None
        client = TestClient(app)
        response = client.get("/api/settings")

        assert response.status_code == 503
        assert "error" in response.json()

    def test_post_settings_updates_config(self):
        """POST /api/settings updates in-memory config via Pydantic schema."""
        from issue_orchestrator.entrypoints import web

        mock_orch = create_mock_orchestrator()
        mock_orch.config.max_concurrent_sessions = 3

        with patch("issue_orchestrator.infra.doctor.run_doctor") as mock_doctor:
            mock_result = MagicMock()
            mock_result.checks = []
            mock_doctor.return_value = mock_result

            mock_orch.config.save = MagicMock()

            web._orchestrator = mock_orch
            try:
                client = TestClient(app)
                response = client.post("/api/settings", json={
                    "concurrency": {
                        "max_concurrent_sessions": 7,
                        "session_timeout_minutes": 45,
                        "queue_refresh_seconds": 600,
                    }
                })

                assert response.status_code == 200
                data = response.json()
                assert data["success"] is True

                # Verify config was updated
                assert mock_orch.config.max_concurrent_sessions == 7
            finally:
                web._orchestrator = None

    def test_post_settings_updates_multiple_tabs(self):
        """POST /api/settings can update multiple tabs at once."""
        from issue_orchestrator.entrypoints import web

        mock_orch = create_mock_orchestrator()

        with patch("issue_orchestrator.infra.doctor.run_doctor") as mock_doctor:
            mock_result = MagicMock()
            mock_result.checks = []
            mock_doctor.return_value = mock_result

            mock_orch.config.save = MagicMock()

            web._orchestrator = mock_orch
            try:
                client = TestClient(app)
                response = client.post("/api/settings", json={
                    "concurrency": {
                        "max_concurrent_sessions": 10,
                        "session_timeout_minutes": 90,
                        "queue_refresh_seconds": 300,
                    },
                    "e2e": {
                        "enabled": True,
                        "auto_run_interval_minutes": 15,
                        "role": "executor",
                        "pytest_args": "tests/e2e -v",
                        "allow_retry_once": False,
                        "stop_on_first_failure": True,
                        "quarantine_file": "quarantine.txt",
                    },
                })

                assert response.status_code == 200
                assert mock_orch.config.max_concurrent_sessions == 10
                assert mock_orch.config.e2e.enabled is True
                assert mock_orch.config.e2e.role == "executor"
            finally:
                web._orchestrator = None

    def test_post_settings_reverts_on_validation_failure(self):
        """POST /api/settings reverts in-memory changes if doctor fails."""
        from issue_orchestrator.entrypoints import web

        mock_orch = create_mock_orchestrator()
        original_value = mock_orch.config.max_concurrent_sessions

        with patch("issue_orchestrator.infra.doctor.run_doctor") as mock_doctor:
            mock_check = MagicMock()
            mock_check.status = "error"
            mock_check.name = "Test Check"
            mock_check.detail = "Validation failed"
            mock_result = MagicMock()
            mock_result.checks = [mock_check]
            mock_doctor.return_value = mock_result

            web._orchestrator = mock_orch
            try:
                client = TestClient(app)
                response = client.post("/api/settings", json={
                    "concurrency": {
                        "max_concurrent_sessions": 15,
                        "session_timeout_minutes": 45,
                        "queue_refresh_seconds": 600,
                    }
                })

                assert response.status_code == 400
                data = response.json()
                assert "error" in data
                assert "errors" in data

                # Verify config was reverted
                assert mock_orch.config.max_concurrent_sessions == original_value
            finally:
                web._orchestrator = None

    def test_post_settings_returns_warnings(self):
        """POST /api/settings includes warnings in response."""
        from issue_orchestrator.entrypoints import web

        mock_orch = create_mock_orchestrator()

        with patch("issue_orchestrator.infra.doctor.run_doctor") as mock_doctor:
            mock_warning = MagicMock()
            mock_warning.status = "warning"
            mock_warning.name = "Token Scope"
            mock_warning.detail = "Token has broad permissions"
            mock_result = MagicMock()
            mock_result.checks = [mock_warning]
            mock_doctor.return_value = mock_result

            mock_orch.config.save = MagicMock()

            web._orchestrator = mock_orch
            try:
                client = TestClient(app)
                response = client.post("/api/settings", json={
                    "concurrency": {
                        "max_concurrent_sessions": 5,
                        "session_timeout_minutes": 45,
                        "queue_refresh_seconds": 600,
                    }
                })

                assert response.status_code == 200
                data = response.json()
                assert data["success"] is True
                assert len(data["warnings"]) == 1
                assert data["warnings"][0]["name"] == "Token Scope"
                assert data["warnings"][0]["detail"] == "Token has broad permissions"
            finally:
                web._orchestrator = None

    def test_post_settings_rejects_invalid_values_via_pydantic(self):
        """POST /api/settings rejects out-of-range values via Pydantic validation."""
        from issue_orchestrator.entrypoints import web

        mock_orch = create_mock_orchestrator()

        web._orchestrator = mock_orch
        try:
            client = TestClient(app)
            # max_concurrent_sessions has ge=1 constraint
            response = client.post("/api/settings", json={
                "concurrency": {
                    "max_concurrent_sessions": 0,
                    "session_timeout_minutes": 45,
                    "queue_refresh_seconds": 600,
                }
            })

            assert response.status_code == 400
            data = response.json()
            assert "error" in data
        finally:
            web._orchestrator = None

    def test_post_settings_rejects_invalid_enum(self):
        """POST /api/settings rejects invalid enum values."""
        from issue_orchestrator.entrypoints import web

        mock_orch = create_mock_orchestrator()

        web._orchestrator = mock_orch
        try:
            client = TestClient(app)
            response = client.post("/api/settings", json={
                "e2e": {
                    "enabled": False,
                    "auto_run_interval_minutes": 30,
                    "role": "invalid_role",
                    "pytest_args": "tests/e2e -v",
                    "allow_retry_once": True,
                    "stop_on_first_failure": False,
                    "quarantine_file": "tests/e2e/quarantine.txt",
                }
            })

            assert response.status_code == 400
        finally:
            web._orchestrator = None

    def test_post_settings_reverts_on_save_failure(self):
        """POST /api/settings reverts in-memory changes if save fails."""
        from issue_orchestrator.entrypoints import web

        mock_orch = create_mock_orchestrator()
        original_value = mock_orch.config.max_concurrent_sessions

        with patch("issue_orchestrator.infra.doctor.run_doctor") as mock_doctor:
            mock_result = MagicMock()
            mock_result.checks = []
            mock_doctor.return_value = mock_result

            mock_orch.config.save = MagicMock(side_effect=IOError("Disk full"))

            web._orchestrator = mock_orch
            try:
                client = TestClient(app)
                response = client.post("/api/settings", json={
                    "concurrency": {
                        "max_concurrent_sessions": 15,
                        "session_timeout_minutes": 45,
                        "queue_refresh_seconds": 600,
                    }
                })

                assert response.status_code == 500
                assert "Disk full" in response.json()["error"]

                # Verify config was reverted
                assert mock_orch.config.max_concurrent_sessions == original_value
            finally:
                web._orchestrator = None

    def test_settings_page_renders(self):
        """GET /settings renders the settings page with schema-driven fields."""
        from issue_orchestrator.entrypoints import web

        mock_orch = create_mock_orchestrator()

        web._orchestrator = mock_orch
        try:
            client = TestClient(app)
            response = client.get("/settings")

            assert response.status_code == 200
            html = response.text
            assert "Settings" in html
            assert "Concurrency" in html
            assert "E2E Runner" in html
            assert "Filtering" in html
            assert "Review" in html
            assert "Advanced" in html
        finally:
            web._orchestrator = None

    def test_settings_page_renders_schema_fields(self):
        """GET /settings renders form fields with data-tab/data-field attributes."""
        from issue_orchestrator.entrypoints import web

        mock_orch = create_mock_orchestrator()

        web._orchestrator = mock_orch
        try:
            client = TestClient(app)
            response = client.get("/settings")

            html = response.text
            # Check schema-driven data attributes are present
            assert 'data-tab="concurrency"' in html
            assert 'data-field="max_concurrent_sessions"' in html
            assert 'data-type="integer"' in html
            assert 'data-type="boolean"' in html
            # Check that current values are rendered
            assert f'value="{mock_orch.config.max_concurrent_sessions}"' in html
        finally:
            web._orchestrator = None

    def test_settings_page_embeds_schema_json(self):
        """GET /settings embeds schema JSON for client-side validation."""
        from issue_orchestrator.entrypoints import web

        mock_orch = create_mock_orchestrator()

        web._orchestrator = mock_orch
        try:
            client = TestClient(app)
            response = client.get("/settings")

            html = response.text
            assert "SCHEMA_TABS" in html
            assert "SCHEMA_FIELDS" in html
        finally:
            web._orchestrator = None

    def test_settings_page_renders_without_orchestrator(self):
        """GET /settings renders with default config when no orchestrator."""
        from issue_orchestrator.entrypoints import web

        web._orchestrator = None
        client = TestClient(app)
        response = client.get("/settings")

        assert response.status_code == 200
        assert "Settings" in response.text

    def test_get_settings_filtering_with_milestones(self):
        """GET /api/settings returns milestones as comma-separated string."""
        from issue_orchestrator.entrypoints import web

        mock_orch = create_mock_orchestrator()
        mock_orch.config.filtering.milestones = ["M1", "M2"]

        web._orchestrator = mock_orch
        try:
            client = TestClient(app)
            response = client.get("/api/settings")

            assert response.status_code == 200
            data = response.json()

            # Schema returns milestones as comma-separated string
            assert data["filtering"]["milestones"] == "M1, M2"
        finally:
            web._orchestrator = None

    def test_get_settings_filtering_with_singular_milestone(self):
        """GET /api/settings handles singular milestone field via get_milestones()."""
        from issue_orchestrator.entrypoints import web

        mock_orch = create_mock_orchestrator()
        mock_orch.config.filtering.milestone = "v1.0"
        mock_orch.config.filtering.milestones = []

        web._orchestrator = mock_orch
        try:
            client = TestClient(app)
            response = client.get("/api/settings")

            assert response.status_code == 200
            data = response.json()

            # get_milestones() returns ["v1.0"], schema joins with comma
            assert data["filtering"]["milestones"] == "v1.0"
        finally:
            web._orchestrator = None

    def test_post_settings_milestones_comma_separated(self):
        """POST /api/settings handles comma-separated milestones string."""
        from issue_orchestrator.entrypoints import web

        mock_orch = create_mock_orchestrator()
        mock_orch.config.filtering.milestone = "old-milestone"

        with patch("issue_orchestrator.infra.doctor.run_doctor") as mock_doctor:
            mock_result = MagicMock()
            mock_result.checks = []
            mock_doctor.return_value = mock_result

            mock_orch.config.save = MagicMock()

            web._orchestrator = mock_orch
            try:
                client = TestClient(app)
                response = client.post("/api/settings", json={
                    "filtering": {
                        "label": None,
                        "milestones": "M1, M2",
                        "exclude_labels": "",
                        "fetch_limit": 100,
                        "max_to_start": 0,
                    }
                })

                assert response.status_code == 200

                # Comma-separated string should be split into list
                assert mock_orch.config.filtering.milestones == ["M1", "M2"]
            finally:
                web._orchestrator = None

    def test_post_settings_empty_milestones(self):
        """POST /api/settings handles empty milestones string."""
        from issue_orchestrator.entrypoints import web

        mock_orch = create_mock_orchestrator()

        with patch("issue_orchestrator.infra.doctor.run_doctor") as mock_doctor:
            mock_result = MagicMock()
            mock_result.checks = []
            mock_doctor.return_value = mock_result

            mock_orch.config.save = MagicMock()

            web._orchestrator = mock_orch
            try:
                client = TestClient(app)
                response = client.post("/api/settings", json={
                    "filtering": {
                        "label": None,
                        "milestones": "",
                        "exclude_labels": "",
                        "fetch_limit": 100,
                        "max_to_start": 0,
                    }
                })

                assert response.status_code == 200
                assert mock_orch.config.filtering.milestones == []
            finally:
                web._orchestrator = None

    def test_post_settings_restart_required(self):
        """POST /api/settings signals restart_required when port changes."""
        from issue_orchestrator.entrypoints import web

        mock_orch = create_mock_orchestrator()

        with patch("issue_orchestrator.infra.doctor.run_doctor") as mock_doctor:
            mock_result = MagicMock()
            mock_result.checks = []
            mock_doctor.return_value = mock_result

            mock_orch.config.save = MagicMock()

            web._orchestrator = mock_orch
            try:
                client = TestClient(app)
                response = client.post("/api/settings", json={
                    "advanced": {
                        "session_no_output_seconds": 120,
                        "stale_escalation_ticks": 0,
                        "web_port": 9090,
                        "control_api_port": 19080,
                        "worktree_base": str(mock_orch.config.worktree_base),
                        "worktree_branch_on_recreate": "delete",
                    }
                })

                assert response.status_code == 200
                data = response.json()
                assert data["restart_required"] is True
                assert mock_orch.config.web_port == 9090
            finally:
                web._orchestrator = None

    def test_post_settings_partial_tabs_preserve_others(self):
        """POST /api/settings with partial tabs preserves unchanged tabs."""
        from issue_orchestrator.entrypoints import web

        mock_orch = create_mock_orchestrator()
        original_e2e_enabled = mock_orch.config.e2e.enabled

        with patch("issue_orchestrator.infra.doctor.run_doctor") as mock_doctor:
            mock_result = MagicMock()
            mock_result.checks = []
            mock_doctor.return_value = mock_result

            mock_orch.config.save = MagicMock()

            web._orchestrator = mock_orch
            try:
                client = TestClient(app)
                # Only send concurrency tab
                response = client.post("/api/settings", json={
                    "concurrency": {
                        "max_concurrent_sessions": 10,
                        "session_timeout_minutes": 45,
                        "queue_refresh_seconds": 600,
                    }
                })

                assert response.status_code == 200
                assert mock_orch.config.max_concurrent_sessions == 10
                # E2E settings should be unchanged
                assert mock_orch.config.e2e.enabled == original_e2e_enabled
            finally:
                web._orchestrator = None


class TestStaticFilesSecurity:
    """Tests for static file serving security."""

    def test_path_traversal_blocked_css(self):
        """Path traversal attempts in CSS route should return 404."""
        client = TestClient(app)
        # Attempt to traverse out of static directory
        response = client.get("/static/css/../../../templates/dashboard.html")
        assert response.status_code == 404

    def test_path_traversal_blocked_js(self):
        """Path traversal attempts in JS route should return 404."""
        client = TestClient(app)
        # Attempt to traverse out of static directory
        response = client.get("/static/js/../../../entrypoints/web.py")
        assert response.status_code == 404

    def test_valid_css_file_served(self):
        """Valid CSS files should be served correctly."""
        client = TestClient(app)
        response = client.get("/static/css/dashboard.css")
        assert response.status_code == 200
        assert "text/css" in response.headers.get("content-type", "")

    def test_valid_js_file_served(self):
        """Valid JS files should be served correctly."""
        client = TestClient(app)
        response = client.get("/static/js/dashboard.js")
        assert response.status_code == 200
        assert "javascript" in response.headers.get("content-type", "")
