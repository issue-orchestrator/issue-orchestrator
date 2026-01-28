"""Unit tests for the control API module.

This test suite covers the behavior of HTTP endpoints in the control API,
focusing on:
- State transitions (pause/resume/shutdown)
- Request handling (refresh with inflight_stable_ids)
- Error responses when orchestrator is not initialized
- SSE event streaming behavior
- Snapshot generation

Testing strategy:
- Mock the orchestrator dependency at the module level
- Use FastAPI's TestClient for synchronous endpoint testing
- Test actual behavior, not implementation details
"""

import pytest
import json
from pathlib import Path
from unittest.mock import MagicMock, patch, AsyncMock
from fastapi.testclient import TestClient

from issue_orchestrator.entrypoints.control_api import (
    control_app,
    set_orchestrator,
    get_orchestrator,
    set_supervisor,
)
from issue_orchestrator.domain.models import OrchestratorState
from issue_orchestrator.infra.config import Config
from issue_orchestrator.infra.supervisor import (
    DefaultSupervisorOps,
    SupervisorOps,
    SupervisorStatus,
)


# --- Fixtures ---


def create_mock_orchestrator():
    """Create a mock orchestrator with required attributes."""
    mock = MagicMock()

    # Create state with realistic defaults
    mock.state = OrchestratorState()

    # Mock methods that endpoints call
    mock.pause = MagicMock()
    mock.resume = MagicMock()
    mock.request_shutdown = MagicMock()
    mock.request_refresh = MagicMock()

    # Mock event_hub for SSE endpoints
    mock.event_hub = MagicMock()
    mock.event_hub.last_event_id = 0
    mock.event_hub.stats.return_value = {
        "subscribers": 0,
        "buffer_size": 0,
        "oldest_event_id": None,
        "newest_event_id": None,
    }

    # Mock config for snapshot endpoint
    mock.config = Config()
    mock.config.repo = "test/repo"

    # Mock deps for snapshot endpoint
    mock.deps = MagicMock()
    mock.deps.repository_host = MagicMock()

    # Mock event context for snapshot (use public property)
    mock.event_context = MagicMock()
    mock.event_context.tick_id = 0

    return mock


@pytest.fixture
def mock_orchestrator():
    """Fixture providing a mock orchestrator."""
    return create_mock_orchestrator()


@pytest.fixture
def client_with_orchestrator(mock_orchestrator):
    """Create a test client with the orchestrator set."""
    set_orchestrator(mock_orchestrator)
    try:
        yield TestClient(control_app), mock_orchestrator
    finally:
        set_orchestrator(None)


@pytest.fixture
def client_without_orchestrator():
    """Create a test client without an orchestrator."""
    set_orchestrator(None)
    return TestClient(control_app)


# --- Test: Orchestrator Not Initialized (503 errors) ---


class TestOrchestratorNotInitialized:
    """Test that endpoints return 503 when orchestrator is not initialized."""

    def test_refresh_returns_503(self, client_without_orchestrator):
        """POST /api/refresh returns 503 when orchestrator is None."""
        response = client_without_orchestrator.post("/api/refresh")

        assert response.status_code == 503
        assert response.json()["error"] == "Orchestrator not initialized"

    def test_pause_returns_503(self, client_without_orchestrator):
        """POST /api/pause returns 503 when orchestrator is None."""
        response = client_without_orchestrator.post("/api/pause")

        assert response.status_code == 503
        assert response.json()["error"] == "Orchestrator not initialized"

    def test_resume_returns_503(self, client_without_orchestrator):
        """POST /api/resume returns 503 when orchestrator is None."""
        response = client_without_orchestrator.post("/api/resume")

        assert response.status_code == 503
        assert response.json()["error"] == "Orchestrator not initialized"

    def test_status_returns_503(self, client_without_orchestrator):
        """GET /api/status returns 503 when orchestrator is None."""
        response = client_without_orchestrator.get("/api/status")

        assert response.status_code == 503
        assert response.json()["error"] == "Orchestrator not initialized"

    def test_events_returns_503(self, client_without_orchestrator):
        """GET /api/events returns 503 when orchestrator is None."""
        response = client_without_orchestrator.get("/api/events")

        assert response.status_code == 503
        assert response.json()["error"] == "Event hub not initialized"

    def test_events_since_returns_503(self, client_without_orchestrator):
        """GET /api/events_since returns 503 when orchestrator is None."""
        response = client_without_orchestrator.get("/api/events_since?after=0")

        assert response.status_code == 503
        assert response.json()["error"] == "Event hub not initialized"

    def test_events_stats_returns_503(self, client_without_orchestrator):
        """GET /api/events_stats returns 503 when orchestrator is None."""
        response = client_without_orchestrator.get("/api/events_stats")

        assert response.status_code == 503
        assert response.json()["error"] == "Event hub not initialized"

    def test_snapshot_returns_503(self, client_without_orchestrator):
        """GET /api/snapshot returns 503 when orchestrator is None."""
        response = client_without_orchestrator.get("/api/snapshot")

        assert response.status_code == 503
        assert response.json()["error"] == "Orchestrator not initialized"

    def test_shutdown_returns_503(self, client_without_orchestrator):
        """POST /api/shutdown returns 503 when orchestrator is None."""
        response = client_without_orchestrator.post("/api/shutdown")

        assert response.status_code == 503
        assert response.json()["error"] == "Orchestrator not initialized"


class TestEventHubNotInitialized:
    """Test SSE endpoints when event_hub is None."""

    def test_events_returns_503_when_event_hub_none(self, mock_orchestrator):
        """GET /api/events returns 503 when event_hub is None."""
        mock_orchestrator.event_hub = None
        set_orchestrator(mock_orchestrator)
        try:
            client = TestClient(control_app)
            response = client.get("/api/events")

            assert response.status_code == 503
            assert response.json()["error"] == "Event hub not initialized"
        finally:
            set_orchestrator(None)

    def test_events_since_returns_503_when_event_hub_none(self, mock_orchestrator):
        """GET /api/events_since returns 503 when event_hub is None."""
        mock_orchestrator.event_hub = None
        set_orchestrator(mock_orchestrator)
        try:
            client = TestClient(control_app)
            response = client.get("/api/events_since?after=0")

            assert response.status_code == 503
            assert response.json()["error"] == "Event hub not initialized"
        finally:
            set_orchestrator(None)

    def test_snapshot_returns_503_when_event_hub_none(self, mock_orchestrator):
        """GET /api/snapshot returns 503 when event_hub is None."""
        mock_orchestrator.event_hub = None
        set_orchestrator(mock_orchestrator)
        try:
            client = TestClient(control_app)
            response = client.get("/api/snapshot")

            assert response.status_code == 503
            assert response.json()["error"] == "Event hub not initialized"
        finally:
            set_orchestrator(None)


# --- Test: State Transition Endpoints ---


class TestPauseEndpoint:
    """Test the POST /api/pause endpoint."""

    def test_pause_calls_orchestrator_pause(self, client_with_orchestrator):
        """Pausing calls orchestrator.pause() and returns paused status."""
        client, mock_orch = client_with_orchestrator

        response = client.post("/api/pause")

        assert response.status_code == 200
        assert response.json() == {"status": "paused"}
        mock_orch.pause.assert_called_once()

    def test_pause_is_idempotent(self, client_with_orchestrator):
        """Pausing twice calls pause() twice (orchestrator handles idempotency)."""
        client, mock_orch = client_with_orchestrator

        client.post("/api/pause")
        client.post("/api/pause")

        assert mock_orch.pause.call_count == 2


class TestResumeEndpoint:
    """Test the POST /api/resume endpoint."""

    def test_resume_calls_orchestrator_resume(self, client_with_orchestrator):
        """Resuming calls orchestrator.resume() and returns resumed status."""
        client, mock_orch = client_with_orchestrator

        response = client.post("/api/resume")

        assert response.status_code == 200
        assert response.json() == {"status": "resumed"}
        mock_orch.resume.assert_called_once()

    def test_resume_is_idempotent(self, client_with_orchestrator):
        """Resuming twice calls resume() twice (orchestrator handles idempotency)."""
        client, mock_orch = client_with_orchestrator

        client.post("/api/resume")
        client.post("/api/resume")

        assert mock_orch.resume.call_count == 2


class TestShutdownEndpoint:
    """Test the POST /api/shutdown endpoint."""

    def test_shutdown_calls_request_shutdown(self, client_with_orchestrator):
        """Shutdown calls orchestrator.request_shutdown()."""
        client, mock_orch = client_with_orchestrator

        response = client.post("/api/shutdown")

        assert response.status_code == 200
        assert response.json() == {"status": "shutdown_requested"}
        mock_orch.request_shutdown.assert_called_once()


# --- Test: Refresh Endpoint ---


class TestRefreshEndpoint:
    """Test the POST /api/refresh endpoint."""

    def test_refresh_without_body(self, client_with_orchestrator):
        """Refresh without body calls request_refresh with empty set."""
        client, mock_orch = client_with_orchestrator

        response = client.post("/api/refresh")

        assert response.status_code == 200
        assert response.json() == {"status": "refresh_requested"}
        mock_orch.request_refresh.assert_called_once_with(inflight_stable_ids=set())

    def test_refresh_with_inflight_stable_ids(self, client_with_orchestrator):
        """Refresh with inflight_stable_ids passes them to request_refresh."""
        client, mock_orch = client_with_orchestrator

        response = client.post(
            "/api/refresh",
            json={"inflight_stable_ids": ["issue-1", "issue-2", "issue-3"]}
        )

        assert response.status_code == 200
        assert response.json() == {"status": "refresh_requested"}
        mock_orch.request_refresh.assert_called_once()
        call_args = mock_orch.request_refresh.call_args
        assert call_args.kwargs["inflight_stable_ids"] == {"issue-1", "issue-2", "issue-3"}

    def test_refresh_with_integer_stable_ids(self, client_with_orchestrator):
        """Refresh converts integer stable_ids to strings."""
        client, mock_orch = client_with_orchestrator

        response = client.post(
            "/api/refresh",
            json={"inflight_stable_ids": [1, 2, 3]}
        )

        assert response.status_code == 200
        call_args = mock_orch.request_refresh.call_args
        assert call_args.kwargs["inflight_stable_ids"] == {"1", "2", "3"}

    def test_refresh_ignores_malformed_json(self, client_with_orchestrator):
        """Refresh ignores malformed JSON body and uses empty set."""
        client, mock_orch = client_with_orchestrator

        response = client.post(
            "/api/refresh",
            content="not valid json",
            headers={"Content-Type": "application/json"}
        )

        assert response.status_code == 200
        mock_orch.request_refresh.assert_called_once_with(inflight_stable_ids=set())

    def test_refresh_ignores_empty_body(self, client_with_orchestrator):
        """Refresh with empty body uses empty set."""
        client, mock_orch = client_with_orchestrator

        response = client.post("/api/refresh", content="")

        assert response.status_code == 200
        mock_orch.request_refresh.assert_called_once_with(inflight_stable_ids=set())


# --- Test: Status Endpoint ---


class TestStatusEndpoint:
    """Test the GET /api/status endpoint."""

    def test_status_returns_state_summary(self, client_with_orchestrator):
        """Status endpoint returns orchestrator state summary."""
        client, mock_orch = client_with_orchestrator

        # Set up state with some data
        mock_orch.state.paused = True
        mock_orch.state.active_sessions = [MagicMock(), MagicMock()]
        mock_orch.state.pending_reviews = [MagicMock()]
        mock_orch.state.pending_reworks = []
        mock_orch.state.completed_today = [1, 2, 3]
        mock_orch.state.cached_queue_issues = [MagicMock(), MagicMock(), MagicMock()]

        response = client.get("/api/status")

        assert response.status_code == 200
        data = response.json()
        assert data["paused"] is True
        assert data["active_sessions"] == 2
        assert data["pending_reviews"] == 1
        assert data["pending_reworks"] == 0
        assert data["completed_today"] == 3
        assert data["issues_in_queue"] == 3

    def test_status_reflects_running_state(self, client_with_orchestrator):
        """Status shows paused=False when running."""
        client, mock_orch = client_with_orchestrator

        mock_orch.state.paused = False

        response = client.get("/api/status")

        assert response.json()["paused"] is False


# --- Test: Events Since Endpoint ---


class TestEventsSinceEndpoint:
    """Test the GET /api/events_since endpoint."""

    def test_events_since_returns_buffered_events(self, client_with_orchestrator):
        """events_since returns events after the specified event_id."""
        client, mock_orch = client_with_orchestrator

        # Mock get_since to return events
        mock_event = MagicMock()
        mock_event.event_id = 5
        mock_event.type = "session.started"
        mock_event.issue_key = "123"
        mock_event.payload = {"agent": "developer"}

        mock_orch.event_hub.get_since.return_value = [mock_event]
        mock_orch.event_hub.last_event_id = 5
        mock_orch.event_hub.stats.return_value = {
            "oldest_event_id": 1,
            "newest_event_id": 5,
            "buffer_size": 5,
        }

        response = client.get("/api/events_since?after=3")

        assert response.status_code == 200
        data = response.json()
        assert len(data["events"]) == 1
        assert data["events"][0]["event_id"] == 5
        assert data["events"][0]["type"] == "session.started"
        assert data["events"][0]["issue_key"] == "123"
        assert data["last_event_id"] == 5

    def test_events_since_with_no_events(self, client_with_orchestrator):
        """events_since returns empty list when no events after id."""
        client, mock_orch = client_with_orchestrator

        mock_orch.event_hub.get_since.return_value = []
        mock_orch.event_hub.last_event_id = 10

        response = client.get("/api/events_since?after=10")

        assert response.status_code == 200
        data = response.json()
        assert data["events"] == []
        assert data["last_event_id"] == 10

    def test_events_since_default_after_is_zero(self, client_with_orchestrator):
        """events_since defaults to after=0 if not specified."""
        client, mock_orch = client_with_orchestrator

        mock_orch.event_hub.get_since.return_value = []

        response = client.get("/api/events_since")

        mock_orch.event_hub.get_since.assert_called_once_with(0)


# --- Test: Events Stats Endpoint ---


class TestEventsStatsEndpoint:
    """Test the GET /api/events_stats endpoint."""

    def test_events_stats_returns_hub_stats(self, client_with_orchestrator):
        """events_stats returns the event hub statistics."""
        client, mock_orch = client_with_orchestrator

        mock_orch.event_hub.stats.return_value = {
            "buffer_size": 42,
            "buffer_max": 1000,
            "subscribers": 3,
            "oldest_event_id": 10,
            "newest_event_id": 52,
        }

        response = client.get("/api/events_stats")

        assert response.status_code == 200
        data = response.json()
        assert data["stats"]["buffer_size"] == 42
        assert data["stats"]["subscribers"] == 3


# --- Test: Health Endpoint ---


class TestHealthEndpoint:
    """Test the GET /api/health endpoint."""

    def test_health_returns_degraded_when_orchestrator_not_initialized(self):
        """Health endpoint returns 503 when orchestrator is not initialized."""
        set_orchestrator(None)
        client = TestClient(control_app)

        response = client.get("/api/health")

        assert response.status_code == 503
        data = response.json()
        assert data["orchestrator"]["status"] == "not_initialized"
        assert "terminal" in data

    def test_health_returns_degraded_when_terminal_unhealthy(self, client_with_orchestrator):
        """Health endpoint returns 503 when terminal health check fails."""
        client, mock_orchestrator = client_with_orchestrator

        # Mock unhealthy terminal
        mock_orchestrator.deps.runner.terminal_health_check.return_value = {"healthy": False, "error": "test"}

        response = client.get("/api/health")

        assert response.status_code == 503
        data = response.json()
        assert data["orchestrator"]["status"] == "running"
        assert data["overall"] == "degraded"

    def test_health_returns_healthy_when_terminal_ok(self, client_with_orchestrator):
        """Health endpoint returns 200 when everything is healthy."""
        client, mock_orchestrator = client_with_orchestrator

        # Mock healthy terminal
        mock_orchestrator.deps.runner.terminal_health_check.return_value = {
            "healthy": True,
            "server_running": True,
            "session_exists": True,
            "backend": "tmux",
        }

        response = client.get("/api/health")

        assert response.status_code == 200
        data = response.json()
        assert data["orchestrator"]["status"] == "running"
        assert data["overall"] == "healthy"


# --- Test: GH Audit Report Endpoint ---


class TestGHAuditReportEndpoint:
    """Test the POST /api/gh_audit_report endpoint."""

    def test_audit_report_returns_error_when_disabled(self, client_with_orchestrator):
        """gh_audit_report returns 400 when audit is disabled."""
        client, _ = client_with_orchestrator

        with patch("issue_orchestrator.entrypoints.control_api.gh_audit.enabled", return_value=False):
            response = client.post("/api/gh_audit_report")

        assert response.status_code == 400
        assert response.json()["error"] == "GH audit not enabled"

    def test_audit_report_returns_path_when_enabled(self, client_with_orchestrator):
        """gh_audit_report returns path when audit is enabled."""
        client, _ = client_with_orchestrator

        with patch("issue_orchestrator.entrypoints.control_api.gh_audit.enabled", return_value=True):
            with patch("issue_orchestrator.entrypoints.control_api.gh_audit.emit_report", return_value="/tmp/report.json"):
                response = client.post("/api/gh_audit_report")

        assert response.status_code == 200
        assert response.json() == {"status": "ok", "path": "/tmp/report.json"}


# --- Test: Snapshot Endpoint ---


class TestSnapshotEndpoint:
    """Test the GET /api/snapshot endpoint."""

    def test_snapshot_builds_and_returns_data(self, client_with_orchestrator):
        """Snapshot endpoint builds snapshot and returns JSON data."""
        client, mock_orch = client_with_orchestrator

        mock_orch.event_hub.last_event_id = 42
        mock_orch.event_context.tick_id = 10

        # Mock SnapshotBuilder
        with patch("issue_orchestrator.entrypoints.control_api.asyncio.to_thread") as mock_to_thread:
            mock_to_thread.return_value = {
                "snapshot_id": 42,
                "tick_id": 10,
                "sessions": [],
                "queue": [],
            }

            response = client.get("/api/snapshot")

        assert response.status_code == 200
        data = response.json()
        assert data["snapshot_id"] == 42
        assert data["tick_id"] == 10

    def test_snapshot_returns_500_on_error(self, client_with_orchestrator):
        """Snapshot endpoint returns 500 when snapshot building fails."""
        client, mock_orch = client_with_orchestrator

        with patch("issue_orchestrator.entrypoints.control_api.asyncio.to_thread") as mock_to_thread:
            mock_to_thread.side_effect = Exception("Build failed")

            response = client.get("/api/snapshot")

        assert response.status_code == 500
        assert response.json()["error"] == "snapshot_failed"
        assert "Build failed" in response.json()["detail"]


# --- Test: set_orchestrator and get_orchestrator ---


class TestOrchestratorAccessors:
    """Test the module-level orchestrator accessors."""

    def test_set_and_get_orchestrator(self):
        """set_orchestrator and get_orchestrator work correctly."""
        mock = MagicMock()

        set_orchestrator(mock)
        try:
            assert get_orchestrator() is mock
        finally:
            set_orchestrator(None)

        assert get_orchestrator() is None

    def test_set_orchestrator_to_none(self):
        """set_orchestrator(None) clears the orchestrator."""
        mock = MagicMock()
        set_orchestrator(mock)
        set_orchestrator(None)

        assert get_orchestrator() is None


# --- Test: ControlAPIServer ---


class TestControlAPIServer:
    """Test the ControlAPIServer lifecycle management class."""

    def test_init_sets_attributes(self, mock_orchestrator):
        """Server initialization stores orchestrator and port."""
        from issue_orchestrator.entrypoints.control_api import ControlAPIServer

        server = ControlAPIServer(mock_orchestrator, port=8888)

        assert server.orchestrator is mock_orchestrator
        assert server.port == 8888

    def test_init_uses_default_port(self, mock_orchestrator):
        """Server uses default port 19080 when not specified."""
        from issue_orchestrator.entrypoints.control_api import ControlAPIServer

        server = ControlAPIServer(mock_orchestrator)

        assert server.port == 19080

    @pytest.mark.asyncio
    async def test_start_sets_global_orchestrator(self, mock_orchestrator):
        """Starting the server sets the global orchestrator reference."""
        from issue_orchestrator.entrypoints.control_api import ControlAPIServer
        import uvicorn

        server = ControlAPIServer(mock_orchestrator, port=19999)

        # Mock uvicorn.Config and Server to avoid actually starting a server
        mock_server_instance = MagicMock()
        mock_server_instance.started = True
        mock_server_instance.serve = AsyncMock()

        with patch.object(uvicorn, "Config"):
            with patch.object(uvicorn, "Server", return_value=mock_server_instance):
                await server.start()

                # Verify orchestrator was set globally
                assert get_orchestrator() is mock_orchestrator

                # Clean up
                set_orchestrator(None)

    @pytest.mark.asyncio
    async def test_stop_signals_server_exit(self, mock_orchestrator):
        """Stopping sets should_exit on the uvicorn server."""
        from issue_orchestrator.entrypoints.control_api import ControlAPIServer
        import asyncio

        server = ControlAPIServer(mock_orchestrator, port=19999)
        # Set up internal state for testing server stop lifecycle (noqa: SLF001)
        server._server = MagicMock()  # noqa: SLF001
        server._task = asyncio.create_task(asyncio.sleep(0))  # noqa: SLF001
        await server._task  # noqa: SLF001

        await server.stop()

        assert server._server.should_exit is True  # noqa: SLF001

    @pytest.mark.asyncio
    async def test_stop_handles_missing_server(self, mock_orchestrator):
        """Stopping when server is None does not raise."""
        from issue_orchestrator.entrypoints.control_api import ControlAPIServer

        server = ControlAPIServer(mock_orchestrator)
        # Set up internal state for testing stop() handles missing server (noqa: SLF001)
        server._server = None  # noqa: SLF001
        server._task = None  # noqa: SLF001

        # Should not raise
        await server.stop()


# --- Test: SSE Events Endpoint ---
# NOTE: Full SSE streaming tests require integration tests with proper async handling.
# The SSE endpoint behavior is covered by:
# - 503 error tests (when orchestrator/event_hub is None)
# - events_since tests (for event buffering)
# - The EventHub unit tests in test_event_hub.py


# =============================================================================
# Supervisor Control API Tests
# =============================================================================
# These test the /control/orchestrator/* endpoints that use the Supervisor
# to manage orchestrator processes.


import os


@pytest.fixture
def supervisor_client():
    """Create a test client for supervisor endpoints (no orchestrator needed)."""
    return TestClient(control_app)


@pytest.fixture
def mock_supervisor():
    """Inject a mock SupervisorOps into the control API."""
    mock = MagicMock(spec=SupervisorOps)
    mock.status.return_value = SupervisorStatus(state="stopped")
    mock.stop.return_value = True
    mock.stop_by_port.return_value = True
    set_supervisor(mock)
    yield mock
    set_supervisor(DefaultSupervisorOps())


class TestSupervisorStatus:
    """Tests for GET /control/orchestrator/status endpoint."""

    def test_status_returns_stopped_when_no_lock(
        self, supervisor_client: TestClient, tmp_path: Path
    ) -> None:
        """Return stopped state when no orchestrator is running."""
        response = supervisor_client.get(
            "/control/orchestrator/status",
            params={"repo_root": str(tmp_path)},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["state"] == "stopped"

    def test_status_returns_running_with_lock(
        self, supervisor_client: TestClient, tmp_path: Path
    ) -> None:
        """Return running state when lock exists and process is alive."""
        # Create lock file with current process PID
        lock_dir = tmp_path / ".issue-orchestrator"
        lock_dir.mkdir(parents=True)
        lock_path = lock_dir / "lock.json"

        lock_data = {
            "repo_root": str(tmp_path),
            "pid": os.getpid(),
            "started_at": "2024-01-01T00:00:00Z",
            "http_port": 8080,
            "state_dir": str(tmp_path / ".issue-orchestrator" / "state"),
        }
        with open(lock_path, "w") as f:
            json.dump(lock_data, f)

        response = supervisor_client.get(
            "/control/orchestrator/status",
            params={"repo_root": str(tmp_path)},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["state"] == "running"
        assert data["pid"] == os.getpid()

    def test_status_returns_orphaned_when_detected(
        self, supervisor_client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Return running state when untracked orchestrator is detected."""
        from issue_orchestrator.entrypoints import control_api

        def fake_detect(repo_root: Path, config_name: str) -> dict:
            return {
                "port": 19080,
                "health": "ok",
                "tick_age_seconds": 1.2,
                "status": {"shutdown_requested": False, "active_sessions": []},
            }

        monkeypatch.setattr(control_api, "_detect_orchestrator_by_port", fake_detect)

        response = supervisor_client.get(
            "/control/orchestrator/status",
            params={"repo_root": str(tmp_path), "config_name": "default.yaml"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["state"] == "running"
        assert data["orphaned"] is True
        assert data["port"] == 19080

    def test_status_rejects_invalid_repo_root(
        self, supervisor_client: TestClient
    ) -> None:
        """Return 400 for invalid repo_root."""
        response = supervisor_client.get(
            "/control/orchestrator/status",
            params={"repo_root": "/nonexistent/path"},
        )

        assert response.status_code == 400
        assert "Invalid" in response.json()["error"]

    def test_status_rejects_missing_repo_root(
        self, supervisor_client: TestClient
    ) -> None:
        """Return 422 when repo_root is missing."""
        response = supervisor_client.get("/control/orchestrator/status")

        assert response.status_code == 422  # FastAPI validation error


class TestSupervisorStop:
    """Tests for POST /control/orchestrator/stop endpoint."""

    def test_stop_returns_stopped_when_no_lock(
        self, supervisor_client: TestClient, tmp_path: Path
    ) -> None:
        """Return stopped when no orchestrator is running (goal achieved)."""
        response = supervisor_client.post(
            "/control/orchestrator/stop",
            json={"repo_root": str(tmp_path)},
        )

        assert response.status_code == 200
        data = response.json()
        # When no lock exists, the orchestrator is already stopped - goal achieved
        assert data["status"] == "stopped"

    def test_stop_rejects_invalid_repo_root(
        self, supervisor_client: TestClient
    ) -> None:
        """Return 400 for invalid repo_root."""
        response = supervisor_client.post(
            "/control/orchestrator/stop",
            json={"repo_root": "/nonexistent/path"},
        )

        assert response.status_code == 400
        assert "Invalid" in response.json()["error"]

    def test_stop_rejects_invalid_json(self, supervisor_client: TestClient) -> None:
        """Return 400 for invalid JSON."""
        response = supervisor_client.post(
            "/control/orchestrator/stop",
            content="not json",
            headers={"Content-Type": "application/json"},
        )

        assert response.status_code == 400
        assert "Invalid JSON" in response.json()["error"]

    def test_stop_rejects_invalid_port(self, supervisor_client: TestClient, tmp_path: Path) -> None:
        """Return 400 for invalid port."""
        response = supervisor_client.post(
            "/control/orchestrator/stop",
            json={"repo_root": str(tmp_path), "port": -1},
        )

        assert response.status_code == 400
        assert "Invalid port" in response.json()["error"]

    def test_stop_returns_port_mismatch(
        self, supervisor_client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        mock_supervisor: MagicMock,
    ) -> None:
        """Return 409 when port does not match orchestrator."""
        from issue_orchestrator.entrypoints import control_api

        mock_supervisor.status.return_value = SupervisorStatus(state="stopped")
        monkeypatch.setattr(control_api, "_confirm_orchestrator_at_port", lambda *_: False)

        response = supervisor_client.post(
            "/control/orchestrator/stop",
            json={"repo_root": str(tmp_path), "port": 19080},
        )

        assert response.status_code == 409
        assert response.json()["error"] == "port_mismatch"


class TestSupervisorStart:
    """Tests for POST /control/orchestrator/start endpoint."""

    def test_start_rejects_invalid_repo_root(
        self, supervisor_client: TestClient
    ) -> None:
        """Return 400 for invalid repo_root."""
        response = supervisor_client.post(
            "/control/orchestrator/start",
            json={"repo_root": "/nonexistent/path"},
        )

        assert response.status_code == 400
        assert "Invalid" in response.json()["error"]

    def test_start_rejects_invalid_port(
        self, supervisor_client: TestClient, tmp_path: Path
    ) -> None:
        """Return 400 for invalid port."""
        response = supervisor_client.post(
            "/control/orchestrator/start",
            json={"repo_root": str(tmp_path), "port": -1},
        )

        assert response.status_code == 400
        assert "Invalid port" in response.json()["error"]

    def test_start_rejects_invalid_port_type(
        self, supervisor_client: TestClient, tmp_path: Path
    ) -> None:
        """Return 400 for non-integer port."""
        response = supervisor_client.post(
            "/control/orchestrator/start",
            json={"repo_root": str(tmp_path), "port": "not a number"},
        )

        assert response.status_code == 400
        assert "Invalid port" in response.json()["error"]

    def test_start_reports_orphaned_when_detected(
        self, supervisor_client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Return 409 when an untracked orchestrator is detected."""
        from issue_orchestrator.entrypoints import control_api

        monkeypatch.setattr(
            control_api,
            "_detect_orchestrator_by_port",
            lambda *_: {"port": 19080, "health": "ok"},
        )

        response = supervisor_client.post(
            "/control/orchestrator/start",
            json={"repo_root": str(tmp_path), "config_name": "default.yaml"},
        )

        assert response.status_code == 409
        assert response.json()["error"] == "orphaned_running"

    def test_start_force_restart_stops_orphaned(
        self, supervisor_client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        mock_supervisor: MagicMock,
    ) -> None:
        """Force restart should stop the orphaned process before starting."""
        from issue_orchestrator.entrypoints import control_api
        from issue_orchestrator.infra.repo_lock import LockInfo

        # Create config file (required since start endpoint loads config to check instances)
        config_dir = tmp_path / ".issue-orchestrator" / "config"
        config_dir.mkdir(parents=True)
        (config_dir / "default.yaml").write_text("agents: {}\n")

        monkeypatch.setenv("ISSUE_ORCHESTRATOR_CONFIG_DIR", str(tmp_path / "config"))
        monkeypatch.setattr(
            control_api,
            "_detect_orchestrator_by_port",
            lambda *_: {"port": 19080, "health": "ok"},
        )
        mock_supervisor.stop_by_port.return_value = True
        mock_supervisor.start.return_value = LockInfo(
            repo_root=str(tmp_path),
            pid=123,
            started_at="",
            http_port=19080,
            state_dir=str(tmp_path / ".issue-orchestrator" / "state"),
            recovered=False,
        )

        response = supervisor_client.post(
            "/control/orchestrator/start",
            json={
                "repo_root": str(tmp_path),
                "config_name": "default.yaml",
                "force_restart": True,
            },
        )

        assert response.status_code == 200
        assert response.json()["status"] == "started"


class TestSupervisorLastFailure:
    """Tests for GET /control/orchestrator/last_failure endpoint."""

    def test_last_failure_returns_none_when_no_file(
        self, supervisor_client: TestClient, tmp_path: Path
    ) -> None:
        """Return null when no failure file exists."""
        response = supervisor_client.get(
            "/control/orchestrator/last_failure",
            params={"repo_root": str(tmp_path)},
        )

        assert response.status_code == 200
        assert response.json()["last_failure"] is None

    def test_last_failure_returns_data_when_file_exists(
        self, supervisor_client: TestClient, tmp_path: Path
    ) -> None:
        """Return failure data when file exists."""
        state_dir = tmp_path / ".issue-orchestrator" / "state"
        state_dir.mkdir(parents=True)
        failure_path = state_dir / "last_failure.json"

        failure_data = {
            "phase": "bootstrap",
            "message": "Missing token",
            "suggested_fix": "Set GITHUB_TOKEN",
        }
        with open(failure_path, "w") as f:
            json.dump(failure_data, f)

        response = supervisor_client.get(
            "/control/orchestrator/last_failure",
            params={"repo_root": str(tmp_path)},
        )

        assert response.status_code == 200
        data = response.json()["last_failure"]
        assert data["phase"] == "bootstrap"
        assert data["message"] == "Missing token"


class TestSupervisorLogTail:
    """Tests for GET /control/orchestrator/log_tail endpoint."""

    def test_log_tail_returns_empty_when_no_log(
        self, supervisor_client: TestClient, tmp_path: Path
    ) -> None:
        """Return empty list when no log file exists."""
        response = supervisor_client.get(
            "/control/orchestrator/log_tail",
            params={"repo_root": str(tmp_path)},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["lines"] == []
        assert data["total_lines"] == 0

    def test_log_tail_returns_lines_when_log_exists(
        self, supervisor_client: TestClient, tmp_path: Path
    ) -> None:
        """Return log lines when file exists."""
        log_dir = tmp_path / ".issue-orchestrator" / "state" / "logs"
        log_dir.mkdir(parents=True)
        log_path = log_dir / "orchestrator.log"

        # Write some log lines
        lines = [f"Log line {i}" for i in range(10)]
        with open(log_path, "w") as f:
            f.write("\n".join(lines))

        response = supervisor_client.get(
            "/control/orchestrator/log_tail",
            params={"repo_root": str(tmp_path), "n": 5},
        )

        assert response.status_code == 200
        data = response.json()
        assert len(data["lines"]) <= 5
        assert data["total_lines"] == 10


class TestSupervisorRejectsNonlocalRepo:
    """Security tests: Supervisor Control API should reject non-local paths."""

    def test_rejects_relative_path(self, supervisor_client: TestClient) -> None:
        """Reject relative paths."""
        response = supervisor_client.get(
            "/control/orchestrator/status",
            params={"repo_root": "../some/path"},
        )

        # Should resolve and check if exists - ../some/path likely doesn't exist
        assert response.status_code == 400

    def test_rejects_empty_path(self, supervisor_client: TestClient) -> None:
        """Reject empty path."""
        response = supervisor_client.get(
            "/control/orchestrator/status",
            params={"repo_root": ""},
        )

        assert response.status_code == 400


# --- Test: Preflight Push Endpoint ---


class TestPreflightPushEndpoint:
    """Tests for POST /api/preflight-push endpoint.

    This endpoint uses GitWorkingCopy.push_preflight() internally, which
    follows the ports & adapters pattern. Tests mock the push_preflight
    method to verify endpoint behavior.
    """

    @pytest.fixture
    def client(self):
        """Create a test client (no orchestrator needed for this endpoint)."""
        return TestClient(control_app)

    def test_rejects_missing_worktree(self, client: TestClient) -> None:
        """Return 400 when worktree is not provided."""
        response = client.post(
            "/api/preflight-push",
            json={},
        )

        assert response.status_code == 400
        assert "worktree is required" in response.json()["error"]

    def test_rejects_nonexistent_worktree(self, client: TestClient) -> None:
        """Return 400 when worktree path does not exist."""
        response = client.post(
            "/api/preflight-push",
            json={"worktree": "/nonexistent/path"},
        )

        assert response.status_code == 400
        assert "does not exist" in response.json()["error"]

    def test_rejects_invalid_json(self, client: TestClient) -> None:
        """Return 400 when body is not valid JSON."""
        response = client.post(
            "/api/preflight-push",
            content="not json",
            headers={"Content-Type": "application/json"},
        )

        assert response.status_code == 400
        assert "Invalid JSON" in response.json()["error"]

    def test_returns_success_when_push_would_succeed(
        self, client: TestClient, tmp_path: Path
    ) -> None:
        """Return would_succeed=True when dry-run push succeeds."""
        from issue_orchestrator.ports.working_copy import PreflightResult

        # Create a fake worktree directory
        worktree = tmp_path / "worktree"
        worktree.mkdir()

        with patch(
            "issue_orchestrator.execution.git_working_copy.GitWorkingCopy.push_preflight"
        ) as mock_preflight:
            mock_preflight.return_value = PreflightResult(would_succeed=True)

            response = client.post(
                "/api/preflight-push",
                json={"worktree": str(worktree)},
            )

        assert response.status_code == 200
        data = response.json()
        assert data["would_succeed"] is True
        assert data["error"] is None

    def test_returns_failure_with_stale_info_error(
        self, client: TestClient, tmp_path: Path
    ) -> None:
        """Return would_succeed=False with fix hint for stale info error."""
        from issue_orchestrator.ports.working_copy import PreflightResult

        worktree = tmp_path / "worktree"
        worktree.mkdir()

        with patch(
            "issue_orchestrator.execution.git_working_copy.GitWorkingCopy.push_preflight"
        ) as mock_preflight:
            mock_preflight.return_value = PreflightResult(
                would_succeed=False,
                error="error: failed to push (stale info)",
                fix_hint="Branch has diverged. Run: git fetch origin && git rebase origin/main",
            )

            response = client.post(
                "/api/preflight-push",
                json={"worktree": str(worktree)},
            )

        assert response.status_code == 200
        data = response.json()
        assert data["would_succeed"] is False
        assert "stale info" in data["error"]
        assert "git fetch" in data["fix_hint"]

    def test_returns_failure_with_rejected_error(
        self, client: TestClient, tmp_path: Path
    ) -> None:
        """Return would_succeed=False with fix hint for rejected error."""
        from issue_orchestrator.ports.working_copy import PreflightResult

        worktree = tmp_path / "worktree"
        worktree.mkdir()

        with patch(
            "issue_orchestrator.execution.git_working_copy.GitWorkingCopy.push_preflight"
        ) as mock_preflight:
            mock_preflight.return_value = PreflightResult(
                would_succeed=False,
                error="! [rejected] branch -> branch (non-fast-forward)",
                fix_hint="Branch has diverged. Run: git fetch origin && git rebase origin/main",
            )

            response = client.post(
                "/api/preflight-push",
                json={"worktree": str(worktree)},
            )

        assert response.status_code == 200
        data = response.json()
        assert data["would_succeed"] is False
        assert "rejected" in data["error"]
        assert data["fix_hint"] is not None

    def test_handles_no_branch_detected(
        self, client: TestClient, tmp_path: Path
    ) -> None:
        """Return error when current branch cannot be determined."""
        from issue_orchestrator.ports.working_copy import PreflightResult

        worktree = tmp_path / "worktree"
        worktree.mkdir()

        with patch(
            "issue_orchestrator.execution.git_working_copy.GitWorkingCopy.push_preflight"
        ) as mock_preflight:
            mock_preflight.return_value = PreflightResult(
                would_succeed=False,
                error="Could not determine current branch",
                fix_hint="Ensure you are on a branch, not in detached HEAD state",
            )

            response = client.post(
                "/api/preflight-push",
                json={"worktree": str(worktree)},
            )

        assert response.status_code == 200
        data = response.json()
        assert data["would_succeed"] is False
        assert "branch" in data["error"].lower()

    def test_handles_timeout(self, client: TestClient, tmp_path: Path) -> None:
        """Return error when push check times out."""
        from issue_orchestrator.ports.working_copy import PreflightResult

        worktree = tmp_path / "worktree"
        worktree.mkdir()

        with patch(
            "issue_orchestrator.execution.git_working_copy.GitWorkingCopy.push_preflight"
        ) as mock_preflight:
            mock_preflight.return_value = PreflightResult(
                would_succeed=False,
                error="Push check timed out",
                fix_hint="Network or remote issue - retry later",
            )

            response = client.post(
                "/api/preflight-push",
                json={"worktree": str(worktree)},
            )

        assert response.status_code == 200
        data = response.json()
        assert data["would_succeed"] is False
        assert "timed out" in data["error"].lower()


# --- Test: Resume Issue Endpoint ---


class TestResumeIssueEndpoint:
    """Test the POST /api/issues/{issue_number}/resume endpoint."""

    def test_resume_returns_503_when_orchestrator_not_initialized(
        self, client_without_orchestrator
    ):
        """Returns 503 when orchestrator is None."""
        response = client_without_orchestrator.post("/api/issues/123/resume")

        assert response.status_code == 503
        assert response.json()["error"] == "Orchestrator not initialized"

    def test_resume_returns_404_when_worktree_not_found(
        self, client_with_orchestrator, tmp_path
    ):
        """Returns 404 when worktree does not exist."""
        client, mock_orch = client_with_orchestrator

        with patch(
            "issue_orchestrator.control.worktree_manager.get_worktree_path"
        ) as mock_get_path:
            mock_get_path.return_value = tmp_path / "nonexistent-worktree"

            response = client.post("/api/issues/123/resume")

        assert response.status_code == 404
        data = response.json()
        assert data["success"] is False
        assert "not found" in data["error"].lower()

    def test_resume_returns_404_when_no_completion_record(
        self, client_with_orchestrator, tmp_path
    ):
        """Returns 404 when completion.json does not exist."""
        client, mock_orch = client_with_orchestrator

        # Create worktree without completion.json
        worktree = tmp_path / "repo-123"
        worktree.mkdir()

        with patch(
            "issue_orchestrator.control.worktree_manager.get_worktree_path"
        ) as mock_get_path:
            mock_get_path.return_value = worktree

            response = client.post("/api/issues/123/resume")

        assert response.status_code == 404
        data = response.json()
        assert data["success"] is False
        assert "completion" in data["error"].lower()

    def test_resume_processes_completion_successfully(
        self, client_with_orchestrator, tmp_path
    ):
        """Successfully processes completion when worktree and completion.json exist."""
        client, mock_orch = client_with_orchestrator

        # Create worktree with completion.json
        worktree = tmp_path / "repo-123"
        worktree.mkdir()
        completion_dir = worktree / ".issue-orchestrator"
        completion_dir.mkdir()
        completion_path = completion_dir / "completion.json"
        completion_path.write_text('{"outcome": "completed"}')

        # Mock the completion processor
        mock_result = MagicMock()
        mock_result.success = True
        mock_result.message = "Completion processed"
        mock_result.pr_url = "https://github.com/test/repo/pull/456"
        mock_result.actions_taken = ["pushed", "pr_created"]
        mock_result.errors = []
        mock_orch.deps.completion_processor.process.return_value = mock_result

        with patch(
            "issue_orchestrator.control.worktree_manager.get_worktree_path"
        ) as mock_get_path:
            mock_get_path.return_value = worktree

            response = client.post("/api/issues/123/resume")

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["message"] == "Completion processed"
        assert data["pr_url"] == "https://github.com/test/repo/pull/456"
        assert data["actions_taken"] == ["pushed", "pr_created"]

        # Verify completion processor was called with correct args
        mock_orch.deps.completion_processor.process.assert_called_once()
        call_kwargs = mock_orch.deps.completion_processor.process.call_args.kwargs
        assert call_kwargs["worktree"] == worktree
        assert call_kwargs["issue_number"] == 123

    def test_resume_handles_processing_failure(
        self, client_with_orchestrator, tmp_path
    ):
        """Returns error when completion processing fails."""
        client, mock_orch = client_with_orchestrator

        # Create worktree with completion.json
        worktree = tmp_path / "repo-123"
        worktree.mkdir()
        completion_dir = worktree / ".issue-orchestrator"
        completion_dir.mkdir()
        completion_path = completion_dir / "completion.json"
        completion_path.write_text('{"outcome": "completed"}')

        # Mock the completion processor to raise an exception
        mock_orch.deps.completion_processor.process.side_effect = Exception(
            "Push failed: remote rejected"
        )

        with patch(
            "issue_orchestrator.control.worktree_manager.get_worktree_path"
        ) as mock_get_path:
            mock_get_path.return_value = worktree

            response = client.post("/api/issues/123/resume")

        assert response.status_code == 500
        data = response.json()
        assert data["success"] is False
        assert "remote rejected" in data["error"]

    def test_resume_fetches_issue_title_from_cache(
        self, client_with_orchestrator, tmp_path
    ):
        """Uses cached issue title when available."""
        client, mock_orch = client_with_orchestrator

        # Create worktree with completion.json
        worktree = tmp_path / "repo-123"
        worktree.mkdir()
        completion_dir = worktree / ".issue-orchestrator"
        completion_dir.mkdir()
        (completion_dir / "completion.json").write_text('{"outcome": "completed"}')

        # Add issue to cached queue
        mock_issue = MagicMock()
        mock_issue.number = 123
        mock_issue.title = "Cached Issue Title"
        mock_orch.state.cached_queue_issues = [mock_issue]

        mock_result = MagicMock()
        mock_result.success = True
        mock_result.message = "OK"
        mock_result.pr_url = None
        mock_result.actions_taken = []
        mock_result.errors = []
        mock_orch.deps.completion_processor.process.return_value = mock_result

        with patch(
            "issue_orchestrator.control.worktree_manager.get_worktree_path"
        ) as mock_get_path:
            mock_get_path.return_value = worktree

            response = client.post("/api/issues/123/resume")

        assert response.status_code == 200
        # Verify title was used from cache
        call_kwargs = mock_orch.deps.completion_processor.process.call_args.kwargs
        assert call_kwargs["issue_title"] == "Cached Issue Title"


class TestDebugSessionEndpoint:
    """Test the POST /api/issues/{issue_number}/debug-session endpoint."""

    def test_debug_session_returns_503_when_orchestrator_not_initialized(
        self, client_without_orchestrator
    ):
        """Returns 503 when orchestrator is None."""
        response = client_without_orchestrator.post("/api/issues/123/debug-session")

        assert response.status_code == 503
        assert response.json()["error"] == "Orchestrator not initialized"

    def test_debug_session_returns_404_when_worktree_not_found(
        self, client_with_orchestrator, tmp_path
    ):
        """Returns 404 when worktree does not exist."""
        client, mock_orch = client_with_orchestrator

        with patch(
            "issue_orchestrator.control.worktree_manager.get_worktree_path"
        ) as mock_get_path:
            mock_get_path.return_value = tmp_path / "nonexistent-worktree"

            response = client.post("/api/issues/123/debug-session")

        assert response.status_code == 404
        data = response.json()
        assert data["success"] is False
        assert "not found" in data["error"].lower()

    def test_debug_session_returns_404_when_issue_not_found(
        self, client_with_orchestrator, tmp_path
    ):
        """Returns 404 when issue is not in cache and can't be fetched."""
        client, mock_orch = client_with_orchestrator

        # Create worktree
        worktree = tmp_path / "repo-123"
        worktree.mkdir()

        # Empty cached queue
        mock_orch.state.cached_queue_issues = []
        # GitHub fetch returns None
        mock_orch.deps.repository_host.get_issue.return_value = None

        with patch(
            "issue_orchestrator.control.worktree_manager.get_worktree_path"
        ) as mock_get_path:
            mock_get_path.return_value = worktree

            response = client.post("/api/issues/123/debug-session")

        assert response.status_code == 404
        data = response.json()
        assert data["success"] is False
        assert "not found" in data["error"].lower()

    def test_debug_session_returns_400_when_no_agent_type(
        self, client_with_orchestrator, tmp_path
    ):
        """Returns 400 when issue has no agent type label."""
        client, mock_orch = client_with_orchestrator

        # Create worktree
        worktree = tmp_path / "repo-123"
        worktree.mkdir()

        # Issue without agent type
        mock_issue = MagicMock()
        mock_issue.number = 123
        mock_issue.title = "Test Issue"
        mock_issue.agent_type = None
        mock_orch.state.cached_queue_issues = [mock_issue]

        with patch(
            "issue_orchestrator.control.worktree_manager.get_worktree_path"
        ) as mock_get_path:
            mock_get_path.return_value = worktree

            response = client.post("/api/issues/123/debug-session")

        assert response.status_code == 400
        data = response.json()
        assert data["success"] is False
        assert "no agent type" in data["error"].lower()

    def test_debug_session_returns_400_when_agent_config_not_found(
        self, client_with_orchestrator, tmp_path
    ):
        """Returns 400 when agent config is not found."""
        client, mock_orch = client_with_orchestrator

        # Create worktree
        worktree = tmp_path / "repo-123"
        worktree.mkdir()

        # Issue with agent type but no config
        mock_issue = MagicMock()
        mock_issue.number = 123
        mock_issue.title = "Test Issue"
        mock_issue.agent_type = "agent:unknown"
        mock_orch.state.cached_queue_issues = [mock_issue]
        mock_orch.config.agents = {}  # No agent configs

        with patch(
            "issue_orchestrator.control.worktree_manager.get_worktree_path"
        ) as mock_get_path:
            mock_get_path.return_value = worktree

            response = client.post("/api/issues/123/debug-session")

        assert response.status_code == 400
        data = response.json()
        assert data["success"] is False
        assert "no agent config" in data["error"].lower()

    def test_debug_session_returns_409_when_session_already_exists(
        self, client_with_orchestrator, tmp_path
    ):
        """Returns 409 when a debug session already exists for the issue."""
        client, mock_orch = client_with_orchestrator

        # Create worktree
        worktree = tmp_path / "repo-123"
        worktree.mkdir()

        # Issue with agent type
        mock_issue = MagicMock()
        mock_issue.number = 123
        mock_issue.title = "Test Issue"
        mock_issue.agent_type = "agent:claude"
        mock_orch.state.cached_queue_issues = [mock_issue]

        # Agent config exists
        mock_agent_config = MagicMock()
        mock_agent_config.provider = None
        mock_agent_config.model = "sonnet"
        mock_orch.config.agents = {"agent:claude": mock_agent_config}

        # Session already exists
        mock_orch.deps.runner.session_exists.return_value = True

        with patch(
            "issue_orchestrator.control.worktree_manager.get_worktree_path"
        ) as mock_get_path:
            mock_get_path.return_value = worktree

            response = client.post("/api/issues/123/debug-session")

        assert response.status_code == 409
        data = response.json()
        assert data["success"] is False
        assert "already exists" in data["error"].lower()

    def test_debug_session_launches_successfully(
        self, client_with_orchestrator, tmp_path
    ):
        """Successfully launches debug session when worktree and issue exist."""
        client, mock_orch = client_with_orchestrator

        # Create worktree
        worktree = tmp_path / "repo-123"
        worktree.mkdir()

        # Issue with agent type
        mock_issue = MagicMock()
        mock_issue.number = 123
        mock_issue.title = "Test Issue"
        mock_issue.agent_type = "agent:claude"
        mock_orch.state.cached_queue_issues = [mock_issue]

        # Agent config - get_command returns the base command
        mock_agent_config = MagicMock()
        mock_agent_config.get_command.return_value = "claude --model sonnet 'Work on issue'"
        mock_orch.config.agents = {"agent:claude": mock_agent_config}
        mock_orch.config.web_port = 8080

        # Session doesn't exist yet
        mock_orch.deps.runner.session_exists.return_value = False
        # Session creation succeeds
        mock_orch.deps.runner.create_session.return_value = True

        with patch(
            "issue_orchestrator.control.worktree_manager.get_worktree_path"
        ) as mock_get_path:
            mock_get_path.return_value = worktree

            response = client.post("/api/issues/123/debug-session")

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["session_name"] == "debug-123"
        assert data["worktree_path"] == str(worktree)
        assert data["agent"] == "claude"
        assert "agent-done --resume" in data["hint"]

        # Verify get_command was called with debug context
        mock_agent_config.get_command.assert_called_once()
        call_kwargs = mock_agent_config.get_command.call_args.kwargs
        assert call_kwargs["issue_number"] == 123
        assert call_kwargs["issue_title"] == "Test Issue"
        assert call_kwargs["worktree"] == worktree
        assert "DEBUG SESSION" in call_kwargs["existing_work"]

        # Verify session was created with correct args
        mock_orch.deps.runner.create_session.assert_called_once()
        call_kwargs = mock_orch.deps.runner.create_session.call_args.kwargs
        assert call_kwargs["session_id"] == 123
        assert call_kwargs["working_dir"] == str(worktree)
        assert call_kwargs["session_name"] == "debug-123"
        assert "ORCHESTRATOR_ISSUE_NUMBER='123'" in call_kwargs["command"]
        assert "ORCHESTRATOR_API_PORT='8080'" in call_kwargs["command"]

    def test_debug_session_returns_500_when_session_creation_fails(
        self, client_with_orchestrator, tmp_path
    ):
        """Returns 500 when terminal session creation fails."""
        client, mock_orch = client_with_orchestrator

        # Create worktree
        worktree = tmp_path / "repo-123"
        worktree.mkdir()

        # Issue with agent type
        mock_issue = MagicMock()
        mock_issue.number = 123
        mock_issue.title = "Test Issue"
        mock_issue.agent_type = "agent:claude"
        mock_orch.state.cached_queue_issues = [mock_issue]

        # Agent config
        mock_agent_config = MagicMock()
        mock_agent_config.get_command.return_value = "claude 'Work on issue'"
        mock_orch.config.agents = {"agent:claude": mock_agent_config}
        mock_orch.config.web_port = 8080

        # Session doesn't exist yet
        mock_orch.deps.runner.session_exists.return_value = False
        # Session creation fails
        mock_orch.deps.runner.create_session.return_value = False

        with patch(
            "issue_orchestrator.control.worktree_manager.get_worktree_path"
        ) as mock_get_path:
            mock_get_path.return_value = worktree

            response = client.post("/api/issues/123/debug-session")

        assert response.status_code == 500
        data = response.json()
        assert data["success"] is False
        assert "failed to create" in data["error"].lower()

    def test_debug_session_uses_cached_issue_over_github_fetch(
        self, client_with_orchestrator, tmp_path
    ):
        """Uses cached issue data when available."""
        client, mock_orch = client_with_orchestrator

        # Create worktree
        worktree = tmp_path / "repo-123"
        worktree.mkdir()

        # Issue in cache
        mock_issue = MagicMock()
        mock_issue.number = 123
        mock_issue.title = "Cached Title"
        mock_issue.agent_type = "agent:claude"
        mock_orch.state.cached_queue_issues = [mock_issue]

        # Agent config
        mock_agent_config = MagicMock()
        mock_agent_config.get_command.return_value = "claude 'Work on issue'"
        mock_orch.config.agents = {"agent:claude": mock_agent_config}
        mock_orch.config.web_port = 8080

        mock_orch.deps.runner.session_exists.return_value = False
        mock_orch.deps.runner.create_session.return_value = True

        with patch(
            "issue_orchestrator.control.worktree_manager.get_worktree_path"
        ) as mock_get_path:
            mock_get_path.return_value = worktree

            response = client.post("/api/issues/123/debug-session")

        assert response.status_code == 200
        # GitHub should not have been called since issue was in cache
        mock_orch.deps.repository_host.get_issue.assert_not_called()


# --- Test: E2E Logs Endpoint ---


class TestE2ELogsEndpoint:
    """Test the /control/e2e/logs/{run_id} endpoint."""

    @pytest.fixture
    def e2e_client(self):
        """Create a test client for E2E endpoints (no orchestrator needed)."""
        return TestClient(control_app)

    def test_logs_returns_400_for_invalid_repo_root(self, e2e_client):
        """Invalid repo_root should return 400."""
        response = e2e_client.get(
            "/control/e2e/logs/1",
            params={"repo_root": "../invalid/path"}
        )
        assert response.status_code == 400
        assert response.json()["error"] == "Invalid repo_root"

    def test_logs_returns_404_when_db_not_found(self, e2e_client, tmp_path):
        """Missing E2E database should return 404."""
        response = e2e_client.get(
            "/control/e2e/logs/1",
            params={"repo_root": str(tmp_path)}
        )
        assert response.status_code == 404
        assert response.json()["error"] == "not_found"
        assert "E2E database not found" in response.json()["detail"]

    def test_logs_returns_404_when_run_not_found(self, e2e_client, tmp_path):
        """Non-existent run_id should return 404."""
        # Create the .issue-orchestrator directory and an empty DB
        db_dir = tmp_path / ".issue-orchestrator"
        db_dir.mkdir()
        db_path = db_dir / "e2e.db"

        # Create a minimal valid database
        import sqlite3
        conn = sqlite3.connect(db_path)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS e2e_runs (
                id INTEGER PRIMARY KEY,
                repo_root TEXT NOT NULL,
                orchestrator_id TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                status TEXT NOT NULL,
                exit_code INTEGER,
                pytest_args TEXT NOT NULL DEFAULT '[]',
                commit_sha TEXT,
                branch TEXT,
                retry_of INTEGER,
                is_retry_run INTEGER DEFAULT 0,
                duration_seconds REAL,
                note TEXT,
                log_path TEXT,
                artifacts_dir TEXT,
                worker_pid INTEGER,
                total_tests INTEGER,
                current_test TEXT
            )
        """)
        conn.close()

        response = e2e_client.get(
            "/control/e2e/logs/999",
            params={"repo_root": str(tmp_path)}
        )
        assert response.status_code == 404
        assert response.json()["error"] == "not_found"
        assert "Run 999 not found" in response.json()["detail"]

    def test_logs_returns_404_when_no_log_path(self, e2e_client, tmp_path):
        """Run without log_path should return 404."""
        db_dir = tmp_path / ".issue-orchestrator"
        db_dir.mkdir()
        db_path = db_dir / "e2e.db"

        import sqlite3
        conn = sqlite3.connect(db_path)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS e2e_runs (
                id INTEGER PRIMARY KEY,
                repo_root TEXT NOT NULL,
                orchestrator_id TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                status TEXT NOT NULL,
                exit_code INTEGER,
                pytest_args TEXT NOT NULL DEFAULT '[]',
                commit_sha TEXT,
                branch TEXT,
                retry_of INTEGER,
                is_retry_run INTEGER DEFAULT 0,
                duration_seconds REAL,
                note TEXT,
                log_path TEXT,
                artifacts_dir TEXT,
                worker_pid INTEGER,
                total_tests INTEGER,
                current_test TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS e2e_test_results (
                id INTEGER PRIMARY KEY,
                run_id INTEGER NOT NULL,
                nodeid TEXT NOT NULL,
                outcome TEXT NOT NULL,
                duration_seconds REAL,
                longrepr TEXT,
                retry_outcome TEXT,
                is_quarantined INTEGER DEFAULT 0,
                updated_at TEXT NOT NULL
            )
        """)
        # Insert a run without log_path
        conn.execute("""
            INSERT INTO e2e_runs (repo_root, orchestrator_id, started_at, status, pytest_args, log_path)
            VALUES (?, 'test-orch', '2024-01-01T00:00:00', 'completed', '[]', NULL)
        """, (str(tmp_path),))
        conn.commit()
        conn.close()

        response = e2e_client.get(
            "/control/e2e/logs/1",
            params={"repo_root": str(tmp_path)}
        )
        assert response.status_code == 404
        assert response.json()["error"] == "no_logs"

    def test_logs_returns_content_successfully(self, e2e_client, tmp_path):
        """Valid run with log file should return content."""
        db_dir = tmp_path / ".issue-orchestrator"
        db_dir.mkdir()
        db_path = db_dir / "e2e.db"

        # Create a log file
        log_file = tmp_path / "test.log"
        log_file.write_text("Line 1\nLine 2\nLine 3\n")

        import sqlite3
        conn = sqlite3.connect(db_path)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS e2e_runs (
                id INTEGER PRIMARY KEY,
                repo_root TEXT NOT NULL,
                orchestrator_id TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                status TEXT NOT NULL,
                exit_code INTEGER,
                pytest_args TEXT NOT NULL DEFAULT '[]',
                commit_sha TEXT,
                branch TEXT,
                retry_of INTEGER,
                is_retry_run INTEGER DEFAULT 0,
                duration_seconds REAL,
                note TEXT,
                log_path TEXT,
                artifacts_dir TEXT,
                worker_pid INTEGER,
                total_tests INTEGER,
                current_test TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS e2e_test_results (
                id INTEGER PRIMARY KEY,
                run_id INTEGER NOT NULL,
                nodeid TEXT NOT NULL,
                outcome TEXT NOT NULL,
                duration_seconds REAL,
                longrepr TEXT,
                retry_outcome TEXT,
                is_quarantined INTEGER DEFAULT 0,
                updated_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            INSERT INTO e2e_runs (repo_root, orchestrator_id, started_at, status, pytest_args, log_path)
            VALUES (?, 'test-orch', '2024-01-01T00:00:00', 'completed', '[]', ?)
        """, (str(tmp_path), str(log_file)))
        conn.commit()
        conn.close()

        response = e2e_client.get(
            "/control/e2e/logs/1",
            params={"repo_root": str(tmp_path), "tail": 10}
        )
        assert response.status_code == 200
        data = response.json()
        assert data["total_lines"] == 3
        assert data["returned_lines"] == 3
        assert "Line 1" in data["content"]
        assert "Line 3" in data["content"]


# --- Test: E2E Summary Endpoint ---


class TestE2ESummaryEndpoint:
    """Test the /control/e2e/summary/{run_id} endpoint."""

    @pytest.fixture
    def e2e_client(self):
        """Create a test client for E2E endpoints (no orchestrator needed)."""
        return TestClient(control_app)

    def test_summary_returns_400_for_invalid_repo_root(self, e2e_client):
        """Invalid repo_root should return 400."""
        response = e2e_client.get(
            "/control/e2e/summary/1",
            params={"repo_root": "../invalid/path"}
        )
        assert response.status_code == 400
        assert response.json()["error"] == "Invalid repo_root"

    def test_summary_returns_404_when_db_not_found(self, e2e_client, tmp_path):
        """Missing E2E database should return 404."""
        response = e2e_client.get(
            "/control/e2e/summary/1",
            params={"repo_root": str(tmp_path)}
        )
        assert response.status_code == 404
        assert response.json()["error"] == "not_found"

    def test_summary_returns_test_counts(self, e2e_client, tmp_path):
        """Valid run should return test summary with counts."""
        db_dir = tmp_path / ".issue-orchestrator"
        db_dir.mkdir()
        db_path = db_dir / "e2e.db"

        import sqlite3
        conn = sqlite3.connect(db_path)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS e2e_runs (
                id INTEGER PRIMARY KEY,
                repo_root TEXT NOT NULL,
                orchestrator_id TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                status TEXT NOT NULL,
                exit_code INTEGER,
                pytest_args TEXT NOT NULL DEFAULT '[]',
                commit_sha TEXT,
                branch TEXT,
                retry_of INTEGER,
                is_retry_run INTEGER DEFAULT 0,
                duration_seconds REAL,
                note TEXT,
                log_path TEXT,
                artifacts_dir TEXT,
                worker_pid INTEGER,
                total_tests INTEGER,
                current_test TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS e2e_test_results (
                id INTEGER PRIMARY KEY,
                run_id INTEGER NOT NULL,
                nodeid TEXT NOT NULL,
                outcome TEXT NOT NULL,
                duration_seconds REAL,
                longrepr TEXT,
                retry_outcome TEXT,
                is_quarantined INTEGER DEFAULT 0,
                updated_at TEXT NOT NULL
            )
        """)
        # Insert a run
        conn.execute("""
            INSERT INTO e2e_runs (repo_root, orchestrator_id, started_at, status, pytest_args)
            VALUES (?, 'test-orch', '2024-01-01T00:00:00', 'completed', '[]')
        """, (str(tmp_path),))
        # Insert test results
        conn.execute("""
            INSERT INTO e2e_test_results (run_id, nodeid, outcome, updated_at)
            VALUES (1, 'test_a.py::test_pass', 'passed', '2024-01-01T00:00:00')
        """)
        conn.execute("""
            INSERT INTO e2e_test_results (run_id, nodeid, outcome, longrepr, updated_at)
            VALUES (1, 'test_b.py::test_fail', 'failed', 'AssertionError', '2024-01-01T00:00:00')
        """)
        conn.execute("""
            INSERT INTO e2e_test_results (run_id, nodeid, outcome, updated_at)
            VALUES (1, 'test_c.py::test_skip', 'skipped', '2024-01-01T00:00:00')
        """)
        conn.commit()
        conn.close()

        response = e2e_client.get(
            "/control/e2e/summary/1",
            params={"repo_root": str(tmp_path)}
        )
        assert response.status_code == 200
        data = response.json()
        assert "counts" in data
        counts = data["counts"]
        assert counts["passed"] == 1
        assert counts["failed"] == 1
        assert counts["skipped"] == 1
        assert counts["total"] == 3
        # Check failed tests list
        assert len(data["failed"]) == 1
        assert data["failed"][0]["nodeid"] == "test_b.py::test_fail"


# --- Test: Triage Endpoint ---


class TestE2ETriageEndpoint:
    """Test the /control/e2e/triage/{run_id} endpoint."""

    @pytest.fixture
    def e2e_client(self):
        """Create a test client for E2E endpoints (no orchestrator needed)."""
        return TestClient(control_app)

    def test_triage_returns_400_for_invalid_repo_root(self, e2e_client):
        """Invalid repo_root should return 400."""
        response = e2e_client.get(
            "/control/e2e/triage/1",
            params={"repo_root": "../invalid/path"}
        )
        assert response.status_code == 400
        assert response.json()["error"] == "Invalid repo_root"

    def test_triage_returns_404_when_db_not_found(self, e2e_client, tmp_path):
        """Missing E2E database should return 404."""
        response = e2e_client.get(
            "/control/e2e/triage/1",
            params={"repo_root": str(tmp_path)}
        )
        assert response.status_code == 404
        assert response.json()["error"] == "not_found"

    def test_triage_returns_failures_with_metadata(self, e2e_client, tmp_path):
        """Triage should return failures with flake counts and existing issue info."""
        from issue_orchestrator.infra.e2e_db import E2EDB

        db_dir = tmp_path / ".issue-orchestrator"
        db_dir.mkdir()
        db_path = db_dir / "e2e.db"

        # Create DB using E2EDB to get proper schema
        db = E2EDB(db_path)

        # Start a run
        run_id = db.start_run(
            repo_root=str(tmp_path),
            orchestrator_id="test-orch",
            pytest_args=["tests/e2e"],
            commit_sha="abc123",
        )

        # Add test results
        db.upsert_test_result(
            run_id=run_id,
            nodeid="test_a.py::test_pass",
            outcome="passed",
        )
        db.upsert_test_result(
            run_id=run_id,
            nodeid="test_b.py::test_fail",
            outcome="failed",
            longrepr="AssertionError: expected True",
        )

        # Finish run
        db.finish_run(run_id, status="failed", exit_code=1)

        response = e2e_client.get(
            f"/control/e2e/triage/{run_id}",
            params={"repo_root": str(tmp_path)}
        )
        assert response.status_code == 200
        data = response.json()

        # Check structure
        assert "run" in data
        assert "failures" in data
        assert "has_parent_issue" in data
        assert "flake_threshold" in data

        # Check run info
        assert data["run"]["id"] == run_id
        assert data["run"]["commit_sha"] == "abc123"

        # Check failures
        failures = data["failures"]
        assert len(failures) == 1
        assert failures[0]["nodeid"] == "test_b.py::test_fail"
        assert failures[0]["longrepr"] == "AssertionError: expected True"
        assert failures[0]["existing_issue"] is None
        assert failures[0]["flake_count"] == 0
        assert failures[0]["is_likely_flaky"] is False

        # No parent issue yet
        assert data["has_parent_issue"] is False
        assert data["parent_issue_number"] is None

        # Issue status fields should have defaults
        assert data["parent_issue_url"] is None
        assert data["parent_issue_closed"] is False
        assert data["sub_issues"] == []
        assert data["sub_issues_summary"] == {"total": 0, "resolved": 0}

    def test_triage_returns_issue_status_when_parent_exists(self, tmp_path):
        """Triage should return issue URLs and sub-issue details when issues exist."""
        from issue_orchestrator.infra.e2e_db import E2EDB

        db_dir = tmp_path / ".issue-orchestrator"
        db_dir.mkdir()
        db_path = db_dir / "e2e.db"

        # Create DB and add test data
        db = E2EDB(db_path)
        run_id = db.start_run(
            repo_root=str(tmp_path),
            orchestrator_id="test-orch",
            pytest_args=["tests/e2e"],
            commit_sha="def456",
        )

        # Add test results
        db.upsert_test_result(run_id=run_id, nodeid="test_a.py::test_one", outcome="failed")
        db.upsert_test_result(run_id=run_id, nodeid="test_b.py::test_two", outcome="failed")
        db.finish_run(run_id, status="failed", exit_code=1)

        # Create parent issue for the run
        db.record_run_issue(run_id=run_id, github_issue_number=100)

        # Create sub-issues for failures
        db.record_failure_issue(
            nodeid="test_a.py::test_one",
            github_issue_number=101,
            parent_issue_number=100,
            first_failing_run_id=run_id,
            first_failing_sha="def456",
        )
        db.record_failure_issue(
            nodeid="test_b.py::test_two",
            github_issue_number=102,
            parent_issue_number=100,
            first_failing_run_id=run_id,
            first_failing_sha="def456",
        )

        # Resolve one sub-issue
        db.resolve_failure_issue(nodeid="test_b.py::test_two", resolution="passed")

        # Create mock orchestrator with config.repo for URL generation
        mock_orch = create_mock_orchestrator()
        mock_orch.config.repo = "owner/repo"
        set_orchestrator(mock_orch)

        try:
            client = TestClient(control_app)
            response = client.get(
                f"/control/e2e/triage/{run_id}",
                params={"repo_root": str(tmp_path)}
            )
            assert response.status_code == 200
            data = response.json()

            # Verify parent issue info
            assert data["has_parent_issue"] is True
            assert data["parent_issue_number"] == 100
            assert data["parent_issue_url"] == "https://github.com/owner/repo/issues/100"
            assert data["parent_issue_closed"] is False

            # Verify sub-issues
            assert data["sub_issues_summary"] == {"total": 2, "resolved": 1}
            sub_issues = data["sub_issues"]
            assert len(sub_issues) == 2

            # Find sub-issues by nodeid
            sub_by_nodeid = {s["nodeid"]: s for s in sub_issues}

            # Check unresolved sub-issue
            sub1 = sub_by_nodeid["test_a.py::test_one"]
            assert sub1["issue_number"] == 101
            assert sub1["resolved"] is False
            assert sub1["resolution"] is None
            assert sub1["url"] == "https://github.com/owner/repo/issues/101"

            # Check resolved sub-issue
            sub2 = sub_by_nodeid["test_b.py::test_two"]
            assert sub2["issue_number"] == 102
            assert sub2["resolved"] is True
            assert sub2["resolution"] == "passed"
            assert sub2["url"] == "https://github.com/owner/repo/issues/102"
        finally:
            set_orchestrator(None)


class TestE2ESyncIssuesEndpoint:
    """Test the POST /control/e2e/sync-issues/{run_id} endpoint."""

    @pytest.fixture
    def mock_orchestrator_with_tracker(self):
        """Create a mock orchestrator with GitHub client for E2E issue tracking."""
        mock = create_mock_orchestrator()

        # Mock repository_host with http_client
        mock.repository_host = MagicMock()
        mock.repository_host.http_client = MagicMock()

        # Mock close_issue_with_comment behavior
        mock.repository_host.http_client.add_comment = MagicMock()
        mock.repository_host.http_client.update_issue_state = MagicMock()

        return mock

    @pytest.fixture
    def sync_client(self, mock_orchestrator_with_tracker):
        """Create a test client with orchestrator for sync endpoint."""
        set_orchestrator(mock_orchestrator_with_tracker)
        yield TestClient(control_app)
        set_orchestrator(None)

    def test_sync_returns_503_when_no_orchestrator(self, tmp_path):
        """Should return 503 when orchestrator is not running."""
        set_orchestrator(None)
        client = TestClient(control_app)
        response = client.post(
            "/control/e2e/sync-issues/1",
            params={"repo_root": str(tmp_path)}
        )
        assert response.status_code == 503
        assert response.json()["error"] == "Orchestrator not running"

    def test_sync_returns_400_for_invalid_repo_root(self, sync_client):
        """Invalid repo_root should return 400."""
        response = sync_client.post(
            "/control/e2e/sync-issues/1",
            params={"repo_root": "../invalid/path"}
        )
        assert response.status_code == 400
        assert response.json()["error"] == "Invalid repo_root"

    def test_sync_returns_404_when_db_not_found(self, sync_client, tmp_path):
        """Missing E2E database should return 404."""
        response = sync_client.post(
            "/control/e2e/sync-issues/1",
            params={"repo_root": str(tmp_path)}
        )
        assert response.status_code == 404
        assert response.json()["error"] == "not_found"

    def test_sync_returns_404_for_unknown_run(self, sync_client, tmp_path):
        """Unknown run_id should return 404."""
        from issue_orchestrator.infra.e2e_db import E2EDB

        db_dir = tmp_path / ".issue-orchestrator"
        db_dir.mkdir()
        E2EDB(db_dir / "e2e.db")

        response = sync_client.post(
            "/control/e2e/sync-issues/999",
            params={"repo_root": str(tmp_path)}
        )
        assert response.status_code == 404
        assert "not found" in response.json()["detail"]

    def test_sync_closes_issues_for_passing_tests(self, sync_client, tmp_path):
        """Sync should close issues for tests that now pass."""
        from issue_orchestrator.infra.e2e_db import E2EDB

        db_dir = tmp_path / ".issue-orchestrator"
        db_dir.mkdir()
        db = E2EDB(db_dir / "e2e.db")

        # Create a run where test_a failed
        run1_id = db.start_run(str(tmp_path), "test-orch", ["tests/e2e"], commit_sha="sha1")
        db.upsert_test_result(run1_id, "test_a.py::test_failing", "failed", longrepr="Error")
        db.finish_run(run1_id, "failed", exit_code=1)

        # Record a failure issue for test_a
        db.record_failure_issue(
            nodeid="test_a.py::test_failing",
            github_issue_number=101,
            parent_issue_number=100,
            first_failing_run_id=run1_id,
            first_failing_sha="sha1",
        )
        db.record_run_issue(run1_id, 100)

        # Create a new run where test_a now passes
        run2_id = db.start_run(str(tmp_path), "test-orch", ["tests/e2e"], commit_sha="sha2")
        db.upsert_test_result(run2_id, "test_a.py::test_failing", "passed")
        db.finish_run(run2_id, "passed", exit_code=0)

        response = sync_client.post(
            f"/control/e2e/sync-issues/{run2_id}",
            params={"repo_root": str(tmp_path)}
        )
        assert response.status_code == 200
        data = response.json()

        assert data["status"] == "synced"
        assert len(data["closed_issues"]) == 1
        assert data["closed_issues"][0]["number"] == 101
        assert data["closed_issues"][0]["nodeid"] == "test_a.py::test_failing"
        # Parent should also be closed since all sub-issues are resolved
        assert 100 in data["closed_parent_issues"]

    def test_sync_does_not_close_still_failing_tests(self, sync_client, tmp_path):
        """Sync should not close issues for tests that still fail."""
        from issue_orchestrator.infra.e2e_db import E2EDB

        db_dir = tmp_path / ".issue-orchestrator"
        db_dir.mkdir()
        db = E2EDB(db_dir / "e2e.db")

        # Create a run where test_a failed
        run1_id = db.start_run(str(tmp_path), "test-orch", ["tests/e2e"], commit_sha="sha1")
        db.upsert_test_result(run1_id, "test_a.py::test_failing", "failed", longrepr="Error")
        db.finish_run(run1_id, "failed", exit_code=1)

        db.record_failure_issue(
            nodeid="test_a.py::test_failing",
            github_issue_number=101,
            parent_issue_number=100,
            first_failing_run_id=run1_id,
            first_failing_sha="sha1",
        )

        # Create a new run where test_a STILL fails
        run2_id = db.start_run(str(tmp_path), "test-orch", ["tests/e2e"], commit_sha="sha2")
        db.upsert_test_result(run2_id, "test_a.py::test_failing", "failed", longrepr="Error")
        db.finish_run(run2_id, "failed", exit_code=1)

        response = sync_client.post(
            f"/control/e2e/sync-issues/{run2_id}",
            params={"repo_root": str(tmp_path)}
        )
        assert response.status_code == 200
        data = response.json()

        assert data["status"] == "synced"
        assert len(data["closed_issues"]) == 0
        assert len(data["closed_parent_issues"]) == 0


class TestE2EQuarantineModifyEndpoint:
    """Test the POST /control/e2e/quarantine endpoint."""

    @pytest.fixture
    def quarantine_client(self):
        """Create a test client with mock orchestrator for quarantine endpoint."""
        mock = create_mock_orchestrator()
        # Set up e2e config with default quarantine file path
        mock.config.e2e.quarantine_file = "tests/e2e/quarantine.txt"
        set_orchestrator(mock)
        yield TestClient(control_app)
        set_orchestrator(None)

    def test_quarantine_modify_returns_400_for_invalid_repo_root(self, quarantine_client):
        """Invalid repo_root should return 400."""
        response = quarantine_client.post(
            "/control/e2e/quarantine",
            params={"repo_root": "../invalid/path"},
            json={"action": "add", "nodeids": ["test::foo"]}
        )
        assert response.status_code == 400
        assert response.json()["error"] == "Invalid repo_root"

    def test_quarantine_modify_requires_action(self, quarantine_client, tmp_path):
        """Missing action should return 400."""
        response = quarantine_client.post(
            "/control/e2e/quarantine",
            params={"repo_root": str(tmp_path)},
            json={"nodeids": ["test::foo"]}
        )
        assert response.status_code == 400
        assert "action" in response.json()["error"]

    def test_quarantine_modify_requires_nodeids(self, quarantine_client, tmp_path):
        """Empty nodeids should return 400."""
        response = quarantine_client.post(
            "/control/e2e/quarantine",
            params={"repo_root": str(tmp_path)},
            json={"action": "add", "nodeids": []}
        )
        assert response.status_code == 400
        assert "nodeids" in response.json()["error"]

    def test_quarantine_add_tests(self, quarantine_client, tmp_path):
        """Should add tests to quarantine file."""
        # Create empty quarantine file
        quarantine_dir = tmp_path / "tests" / "e2e"
        quarantine_dir.mkdir(parents=True)
        quarantine_file = quarantine_dir / "quarantine.txt"
        quarantine_file.write_text("# Header\n")

        response = quarantine_client.post(
            "/control/e2e/quarantine",
            params={"repo_root": str(tmp_path)},
            json={"action": "add", "nodeids": ["test::foo", "test::bar"]}
        )
        assert response.status_code == 200
        data = response.json()

        assert len(data["added"]) == 2
        assert "test::foo" in data["tests"]
        assert "test::bar" in data["tests"]
        assert data["count"] == 2

        # Verify file was updated
        content = quarantine_file.read_text()
        assert "test::foo" in content
        assert "test::bar" in content

    def test_quarantine_remove_tests(self, quarantine_client, tmp_path):
        """Should remove tests from quarantine file."""
        # Create quarantine file with tests
        quarantine_dir = tmp_path / "tests" / "e2e"
        quarantine_dir.mkdir(parents=True)
        quarantine_file = quarantine_dir / "quarantine.txt"
        quarantine_file.write_text("# Header\ntest::foo\ntest::bar\ntest::baz\n")

        response = quarantine_client.post(
            "/control/e2e/quarantine",
            params={"repo_root": str(tmp_path)},
            json={"action": "remove", "nodeids": ["test::foo", "test::bar"]}
        )
        assert response.status_code == 200
        data = response.json()

        assert len(data["removed"]) == 2
        assert "test::foo" not in data["tests"]
        assert "test::bar" not in data["tests"]
        assert "test::baz" in data["tests"]
        assert data["count"] == 1


class TestE2EFlakyTestsEndpoint:
    """Test the GET /control/e2e/flaky-tests endpoint."""

    @pytest.fixture
    def flaky_client(self):
        """Create a test client with mock orchestrator for flaky tests endpoint."""
        mock = create_mock_orchestrator()
        # Set up e2e config with default quarantine file path
        mock.config.e2e.quarantine_file = "tests/e2e/quarantine.txt"
        set_orchestrator(mock)
        yield TestClient(control_app)
        set_orchestrator(None)

    def test_flaky_returns_400_for_invalid_repo_root(self, flaky_client):
        """Invalid repo_root should return 400."""
        response = flaky_client.get(
            "/control/e2e/flaky-tests",
            params={"repo_root": "../invalid/path"}
        )
        assert response.status_code == 400
        assert response.json()["error"] == "Invalid repo_root"

    def test_flaky_returns_404_when_db_not_found(self, flaky_client, tmp_path):
        """Missing E2E database should return 404."""
        response = flaky_client.get(
            "/control/e2e/flaky-tests",
            params={"repo_root": str(tmp_path)}
        )
        assert response.status_code == 404
        assert response.json()["error"] == "not_found"

    def test_flaky_returns_empty_when_no_flaky_tests(self, flaky_client, tmp_path):
        """Should return empty list when no flaky tests."""
        from issue_orchestrator.infra.e2e_db import E2EDB

        db_dir = tmp_path / ".issue-orchestrator"
        db_dir.mkdir()
        E2EDB(db_dir / "e2e.db")

        response = flaky_client.get(
            "/control/e2e/flaky-tests",
            params={"repo_root": str(tmp_path)}
        )
        assert response.status_code == 200
        data = response.json()

        assert data["flaky_tests"] == []
        assert data["threshold"] == 20
        assert data["window"] == 10

    def test_flaky_returns_tests_above_threshold(self, flaky_client, tmp_path):
        """Should return tests that exceed flip-rate threshold."""
        from issue_orchestrator.infra.e2e_db import E2EDB

        db_dir = tmp_path / ".issue-orchestrator"
        db_dir.mkdir()
        db = E2EDB(db_dir / "e2e.db")

        # Create alternating pass/fail runs for flaky_one (100% flip rate)
        # and stable runs for stable_test (0% flip rate)
        for i in range(6):
            run_id = db.start_run(f"{tmp_path}/repo{i}", "test-orch", ["tests/e2e"])
            # Alternating: flaky
            db.upsert_test_result(run_id, "test::flaky_one", "passed" if i % 2 == 0 else "failed")
            # Always passing: stable
            db.upsert_test_result(run_id, "test::stable_test", "passed")
            db.finish_run(run_id, "passed" if i % 2 == 0 else "failed")

        response = flaky_client.get(
            "/control/e2e/flaky-tests",
            params={"repo_root": str(tmp_path), "threshold": 20}
        )
        assert response.status_code == 200
        data = response.json()

        # test::flaky_one has 100% flip rate, exceeds threshold
        # test::stable_test has 0% flip rate, below threshold
        nodeids = [t["nodeid"] for t in data["flaky_tests"]]
        assert "test::flaky_one" in nodeids
        assert "test::stable_test" not in nodeids

        # Check new response fields
        flaky_entry = next(t for t in data["flaky_tests"] if t["nodeid"] == "test::flaky_one")
        assert "flip_rate" in flaky_entry
        assert "flip_rate_percent" in flaky_entry
        assert "category" in flaky_entry
        assert flaky_entry["category"] == "flaky"
        assert flaky_entry["flip_rate_percent"] == 100.0
        # Backward compat alias
        assert "flake_count" in flaky_entry


# --- Test: E2E Test Detail Endpoint ---


class TestE2ETestDetailEndpoint:
    """Test the GET /control/e2e/test/{run_id} endpoint."""

    @pytest.fixture
    def e2e_client(self):
        """Create a test client for E2E endpoints (no orchestrator needed)."""
        return TestClient(control_app)

    def test_returns_400_for_invalid_repo_root(self, e2e_client):
        """Invalid repo_root should return 400."""
        response = e2e_client.get(
            "/control/e2e/test/1",
            params={"repo_root": "../invalid/path", "nodeid": "test::foo"}
        )
        assert response.status_code == 400
        assert response.json()["error"] == "Invalid repo_root"

    def test_returns_404_when_db_not_found(self, e2e_client, tmp_path):
        """Missing E2E database should return 404."""
        response = e2e_client.get(
            "/control/e2e/test/1",
            params={"repo_root": str(tmp_path), "nodeid": "test::foo"}
        )
        assert response.status_code == 404

    def test_returns_404_when_test_not_found(self, e2e_client, tmp_path):
        """Test not found should return 404."""
        from issue_orchestrator.infra.e2e_db import E2EDB

        db_dir = tmp_path / ".issue-orchestrator"
        db_dir.mkdir()
        db_path = db_dir / "e2e.db"
        db = E2EDB(db_path)

        run_id = db.start_run(
            repo_root=str(tmp_path),
            orchestrator_id="test-orch",
            pytest_args=["tests/e2e"],
        )
        db.finish_run(run_id, status="passed")

        response = e2e_client.get(
            f"/control/e2e/test/{run_id}",
            params={"repo_root": str(tmp_path), "nodeid": "test::nonexistent"}
        )
        assert response.status_code == 404
        assert response.json()["error"] == "not_found"

    def test_returns_test_detail_with_history(self, e2e_client, tmp_path):
        """Should return test details including history."""
        from issue_orchestrator.infra.e2e_db import E2EDB

        db_dir = tmp_path / ".issue-orchestrator"
        db_dir.mkdir()
        db_path = db_dir / "e2e.db"
        db = E2EDB(db_path)

        # Create first run with a failure
        run1_id = db.start_run(
            repo_root=str(tmp_path),
            orchestrator_id="test-orch",
            pytest_args=["tests/e2e"],
        )
        db.upsert_test_result(
            run_id=run1_id,
            nodeid="test_foo.py::test_bar",
            outcome="failed",
            longrepr="AssertionError: expected 1, got 2",
            duration_seconds=1.5,
        )
        db.finish_run(run1_id, status="failed")

        # Create second run with same test passing
        run2_id = db.start_run(
            repo_root=str(tmp_path),
            orchestrator_id="test-orch",
            pytest_args=["tests/e2e"],
        )
        db.upsert_test_result(
            run_id=run2_id,
            nodeid="test_foo.py::test_bar",
            outcome="passed",
            duration_seconds=1.2,
        )
        db.finish_run(run2_id, status="passed")

        # Query the first run's failure
        response = e2e_client.get(
            f"/control/e2e/test/{run1_id}",
            params={"repo_root": str(tmp_path), "nodeid": "test_foo.py::test_bar"}
        )
        assert response.status_code == 200
        data = response.json()

        # Check test details
        assert data["test"]["nodeid"] == "test_foo.py::test_bar"
        assert data["test"]["outcome"] == "failed"
        assert "AssertionError" in data["test"]["longrepr"]
        assert data["test"]["duration_seconds"] == 1.5

        # Check history includes both runs
        assert len(data["history"]) == 2
        assert data["history_summary"]["total"] == 2
        assert data["history_summary"]["passed"] == 1
        assert data["history_summary"]["failed"] == 1


# --- Test: E2E Status Attention Fields ---


class TestE2EStatusAttentionFields:
    """Test needs_attention and untriaged_count in /control/e2e/status."""

    @pytest.fixture
    def e2e_client(self):
        """Create a test client for E2E endpoints."""
        return TestClient(control_app)

    def test_needs_attention_true_when_failed_run_with_no_issues(self, e2e_client, tmp_path):
        """Failed run with untriaged failures should set needs_attention=True."""
        from issue_orchestrator.infra.e2e_db import E2EDB

        # Create config with correct name (default.yaml)
        config_dir = tmp_path / ".issue-orchestrator" / "config"
        config_dir.mkdir(parents=True)
        config_file = config_dir / "default.yaml"
        config_file.write_text("""
repo:
  name: test/repo
e2e:
  enabled: true
  pytest_paths: ["tests/e2e"]
""")

        db_dir = tmp_path / ".issue-orchestrator"
        db_path = db_dir / "e2e.db"
        db = E2EDB(db_path)

        # Use directory name as orchestrator_id (matches config.orchestrator_id)
        orchestrator_id = tmp_path.name

        # Create a failed run with a failing test
        run_id = db.start_run(
            repo_root=str(tmp_path),
            orchestrator_id=orchestrator_id,
            pytest_args=["tests/e2e"],
        )
        db.upsert_test_result(
            run_id=run_id,
            nodeid="test_foo.py::test_bar",
            outcome="failed",
            longrepr="AssertionError",
        )
        db.finish_run(run_id, status="failed")

        response = e2e_client.get(
            "/control/e2e/status",
            params={"repo_root": str(tmp_path)}
        )
        assert response.status_code == 200
        data = response.json()

        # Failed run with no issues created should need attention
        assert data["needs_attention"] is True
        assert data["untriaged_count"] == 1

    def test_needs_attention_false_when_issues_created(self, e2e_client, tmp_path):
        """Failed run with existing issues should set needs_attention=False."""
        from issue_orchestrator.infra.e2e_db import E2EDB

        # Create config
        config_dir = tmp_path / ".issue-orchestrator" / "config"
        config_dir.mkdir(parents=True)
        config_file = config_dir / "default.yaml"
        config_file.write_text("""
repo:
  name: test/repo
e2e:
  enabled: true
  pytest_paths: ["tests/e2e"]
""")

        db_dir = tmp_path / ".issue-orchestrator"
        db_path = db_dir / "e2e.db"
        db = E2EDB(db_path)

        # Use directory name as orchestrator_id (matches config.orchestrator_id)
        orchestrator_id = tmp_path.name

        # Create a failed run
        run_id = db.start_run(
            repo_root=str(tmp_path),
            orchestrator_id=orchestrator_id,
            pytest_args=["tests/e2e"],
        )
        db.upsert_test_result(
            run_id=run_id,
            nodeid="test_foo.py::test_bar",
            outcome="failed",
            longrepr="AssertionError",
        )
        db.finish_run(run_id, status="failed")

        # Record that an issue exists for this failure
        db.record_failure_issue(
            nodeid="test_foo.py::test_bar",
            github_issue_number=123,
            parent_issue_number=100,
            first_failing_run_id=run_id,
            first_failing_sha="abc123",
        )

        response = e2e_client.get(
            "/control/e2e/status",
            params={"repo_root": str(tmp_path)}
        )
        assert response.status_code == 200
        data = response.json()

        # All failures have issues, so no attention needed
        assert data["needs_attention"] is False
        assert data["untriaged_count"] == 0

    def test_needs_attention_false_for_passing_run(self, e2e_client, tmp_path):
        """Passing run should not need attention."""
        from issue_orchestrator.infra.e2e_db import E2EDB

        # Create config
        config_dir = tmp_path / ".issue-orchestrator" / "config"
        config_dir.mkdir(parents=True)
        config_file = config_dir / "default.yaml"
        config_file.write_text("""
repo:
  name: test/repo
e2e:
  enabled: true
  pytest_paths: ["tests/e2e"]
""")

        db_dir = tmp_path / ".issue-orchestrator"
        db_path = db_dir / "e2e.db"
        db = E2EDB(db_path)

        # Use directory name as orchestrator_id (matches config.orchestrator_id)
        orchestrator_id = tmp_path.name

        # Create a passing run
        run_id = db.start_run(
            repo_root=str(tmp_path),
            orchestrator_id=orchestrator_id,
            pytest_args=["tests/e2e"],
        )
        db.upsert_test_result(
            run_id=run_id,
            nodeid="test_foo.py::test_bar",
            outcome="passed",
        )
        db.finish_run(run_id, status="passed")

        response = e2e_client.get(
            "/control/e2e/status",
            params={"repo_root": str(tmp_path)}
        )
        assert response.status_code == 200
        data = response.json()

        # Passing run doesn't need attention
        assert data["needs_attention"] is False
        assert data["untriaged_count"] == 0


# --- Test: Retry Issue Endpoint ---


class TestRetryIssueEndpoint:
    """Test the POST /api/issues/{issue_number}/retry endpoint."""

    def test_retry_returns_503_when_orchestrator_not_initialized(
        self, client_without_orchestrator
    ):
        """Returns 503 when orchestrator is None."""
        response = client_without_orchestrator.post("/api/issues/123/retry")

        assert response.status_code == 503
        assert response.json()["error"] == "Orchestrator not initialized"

    def test_retry_removes_blocked_labels(self, client_with_orchestrator):
        """Retry removes blocked-related labels from the issue."""
        client, mock_orch = client_with_orchestrator

        # Mock the repository_host to track remove_label calls
        removed_labels = []

        def track_remove_label(issue_number: int, label: str):
            removed_labels.append((issue_number, label))

        mock_orch.repository_host = MagicMock()
        mock_orch.repository_host.remove_label = MagicMock(
            side_effect=track_remove_label
        )

        response = client.post("/api/issues/123/retry")

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert "retry" in data["message"].lower()

        # Verify correct labels were targeted for removal
        removed_issue_numbers = [num for num, _ in removed_labels]
        assert all(num == 123 for num in removed_issue_numbers)
        # Should attempt to remove blocked, blocked-needs-human, blocked-failed
        assert len(removed_labels) == 3

    def test_retry_handles_label_removal_failure_gracefully(
        self, client_with_orchestrator
    ):
        """Retry continues even when label removal fails (label may not exist)."""
        client, mock_orch = client_with_orchestrator

        # Mock the repository_host to raise exception on label removal
        mock_orch.repository_host = MagicMock()
        mock_orch.repository_host.remove_label = MagicMock(
            side_effect=Exception("Label not found")
        )

        response = client.post("/api/issues/123/retry")

        # Should still succeed (silent exception handling is acceptable for missing labels)
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True


# --- Test: Dismiss Issue Endpoint ---


class TestDismissIssueEndpoint:
    """Test the POST /api/issues/{issue_number}/dismiss endpoint."""

    def test_dismiss_returns_503_when_orchestrator_not_initialized(
        self, client_without_orchestrator
    ):
        """Returns 503 when orchestrator is None."""
        response = client_without_orchestrator.post("/api/issues/123/dismiss")

        assert response.status_code == 503
        assert response.json()["error"] == "Orchestrator not initialized"

    def test_dismiss_removes_labels_and_session_history(self, client_with_orchestrator):
        """Dismiss removes blocked and in-progress labels, plus session history entry."""
        client, mock_orch = client_with_orchestrator

        # Set up session history with an entry for issue 123
        from issue_orchestrator.domain.models import SessionHistoryEntry

        history_entry = SessionHistoryEntry(
            issue_number=123,
            title="Test Issue",
            agent_type="agent:claude",
            status="needs_human",
            runtime_minutes=10,
        )
        mock_orch.state.session_history = [history_entry]

        # Mock the repository_host
        removed_labels = []

        def track_remove_label(issue_number: int, label: str):
            removed_labels.append((issue_number, label))

        mock_orch.repository_host = MagicMock()
        mock_orch.repository_host.remove_label = MagicMock(
            side_effect=track_remove_label
        )

        response = client.post("/api/issues/123/dismiss")

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert "dismiss" in data["message"].lower()

        # Verify labels were targeted for removal (blocked, blocked-needs-human, blocked-failed, in-progress)
        removed_issue_numbers = [num for num, _ in removed_labels]
        assert all(num == 123 for num in removed_issue_numbers)
        assert len(removed_labels) == 4  # blocked, blocked-needs-human, blocked-failed, in-progress

        # Verify session history entry was removed
        assert len(mock_orch.state.session_history) == 0

    def test_dismiss_handles_missing_session_history(self, client_with_orchestrator):
        """Dismiss succeeds even when issue not in session history."""
        client, mock_orch = client_with_orchestrator

        # Empty session history
        mock_orch.state.session_history = []
        mock_orch.repository_host = MagicMock()
        mock_orch.repository_host.remove_label = MagicMock()

        response = client.post("/api/issues/456/dismiss")

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True

    def test_dismiss_handles_label_removal_failure_gracefully(
        self, client_with_orchestrator
    ):
        """Dismiss continues even when label removal fails (label may not exist)."""
        client, mock_orch = client_with_orchestrator

        mock_orch.state.session_history = []
        mock_orch.repository_host = MagicMock()
        mock_orch.repository_host.remove_label = MagicMock(
            side_effect=Exception("Label not found")
        )

        response = client.post("/api/issues/123/dismiss")

        # Should still succeed (silent exception handling is acceptable for missing labels)
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
