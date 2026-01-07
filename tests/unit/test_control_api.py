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
)
from issue_orchestrator.domain.models import OrchestratorState
from issue_orchestrator.infra.config import Config


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

    # Mock event context for snapshot
    mock._event_context = MagicMock()
    mock._event_context.tick_id = 0

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

    def test_health_always_returns_ok(self):
        """Health endpoint returns ok regardless of orchestrator state."""
        # Health should work even without orchestrator
        set_orchestrator(None)
        client = TestClient(control_app)

        response = client.get("/api/health")

        assert response.status_code == 200
        assert response.json() == {"status": "ok"}


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
        mock_orch._event_context.tick_id = 10

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
        assert server._server is None
        assert server._task is None

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
        server._server = MagicMock()
        # Create a completed task to avoid timeout issues
        server._task = asyncio.create_task(asyncio.sleep(0))
        await server._task  # Complete the task

        await server.stop()

        assert server._server.should_exit is True

    @pytest.mark.asyncio
    async def test_stop_handles_missing_server(self, mock_orchestrator):
        """Stopping when server is None does not raise."""
        from issue_orchestrator.entrypoints.control_api import ControlAPIServer

        server = ControlAPIServer(mock_orchestrator)
        server._server = None
        server._task = None

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
        assert data["port"] == 8080

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
