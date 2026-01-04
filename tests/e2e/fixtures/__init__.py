"""E2E test fixtures split from conftest.py for better organization."""

from .inflight_tracker import (
    trigger_refresh,
    register_inflight_issue,
    ensure_inflight_refresh,
    reset_inflight_tracker,
    get_control_api_port,
)
from .orchestrator_process import (
    OrchestratorProcess,
    E2E_LOG_DIR,
    _keep_artifacts,
    _keep_remote_artifacts,
)
from .github_client import (
    _github_adapter,
    get_issue_comments,
)
from .assertions import (
    wait_with_process_check,
    FATAL_ERROR_PATTERNS,
)
from .data_factory import (
    inflight_create,
    inflight_update,
    inflight_close,
)

__all__ = [
    # inflight_tracker
    "trigger_refresh",
    "register_inflight_issue",
    "ensure_inflight_refresh",
    "reset_inflight_tracker",
    "get_control_api_port",
    # orchestrator_process
    "OrchestratorProcess",
    "E2E_LOG_DIR",
    "_keep_artifacts",
    "_keep_remote_artifacts",
    # github_client
    "_github_adapter",
    "get_issue_comments",
    # assertions
    "wait_with_process_check",
    "FATAL_ERROR_PATTERNS",
    # data_factory
    "inflight_create",
    "inflight_update",
    "inflight_close",
]
