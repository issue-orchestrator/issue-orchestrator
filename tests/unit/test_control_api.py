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

import json
import os
from collections import Counter
from dataclasses import replace
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import MagicMock, patch, AsyncMock

import pytest
from fastapi.testclient import TestClient

from issue_orchestrator.entrypoints.control_api import (
    control_app,
    get_supervisor,
    set_orchestrator,
    get_orchestrator,
    set_control_actions,
    set_supervisor,
)
from issue_orchestrator.entrypoints import control_api_shutdown_state
from issue_orchestrator.execution.control_center_actions import ActionResult, ControlCenterActions
from issue_orchestrator.domain.models import OrchestratorState
from issue_orchestrator.infra.config import Config
from issue_orchestrator.infra.repo_guardrails import RepoGuardrailsError
from issue_orchestrator.infra.supervisor import (
    DefaultSupervisorOps,
    MultiInstanceStatus,
    SupervisorOps,
    SupervisorStatus,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
CONTROL_CENTER_JS = REPO_ROOT / "src/issue_orchestrator/static/js/control_center.js"


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
    mock.deps.services.instance_id = "test-instance-id"

    # Configure label_manager to return real strings (not MagicMock)
    lm = mock.deps.label_manager
    lm.blocked = "blocked"
    lm.needs_human = "blocked-needs-human"
    lm.blocked_failed = "blocked-failed"
    lm.blocked_pr_closed = "blocked:pr-closed"
    lm.pr_pending = "pr-pending"
    lm.in_progress = "in-progress"
    lm.get_blocking = MagicMock(
        side_effect=lambda labels: [
            label for label in labels
            if label in {"blocked", "blocked-needs-human", "blocked-failed"}
        ],
    )
    lm.is_pr_pending = MagicMock(side_effect=lambda labels: "pr-pending" in labels)

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


@pytest.fixture
def supervisor_client():
    """Create a test client for supervisor endpoints (no orchestrator needed)."""
    return TestClient(control_app)


@pytest.fixture
def mock_supervisor():
    """Inject a mock SupervisorOps into the control API."""
    mock = MagicMock(spec=SupervisorOps)
    mock.status.return_value = SupervisorStatus(state="stopped")
    mock.status_all_instances.return_value = MultiInstanceStatus(repo_root="", expected_count=1, instances=[])
    mock.stop.return_value = True
    mock.stop_by_port.return_value = True
    set_supervisor(mock)
    yield mock
    set_supervisor(DefaultSupervisorOps())
