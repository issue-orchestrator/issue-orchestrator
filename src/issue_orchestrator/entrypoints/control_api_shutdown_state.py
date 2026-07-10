"""Shutdown operation state for Control Center APIs."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import threading
import time
from typing import Any, Literal, TypedDict

from ..infra.shutdown_timing import (
    DEFAULT_ENGINE_GRACEFUL_TIMEOUT_SECONDS,
    StopPolicySnapshot,
)


@dataclass(frozen=True)
class GlobalShutdownConflict:
    """Result returned when a global shutdown is already in progress."""

    operation_id: str | None


@dataclass(frozen=True)
class ShutdownPolicyUpdate:
    """Current policy after applying a shutdown update request."""

    graceful_timeout_seconds: int
    force_orchestrators: bool


@dataclass(frozen=True)
class GlobalShutdownStopPolicy:
    """Live policy owner bound to one global shutdown operation."""

    operation_id: str

    def snapshot(self) -> StopPolicySnapshot:
        """Read force, abort, and timeout changes under the operation lock."""
        with _shutdown_ops_lock:
            op = _global_shutdown_operation
            if (
                not op
                or op.get("operation_id") != self.operation_id
                or op.get("state") != "in_progress"
            ):
                return StopPolicySnapshot(
                    graceful_timeout_seconds=DEFAULT_ENGINE_GRACEFUL_TIMEOUT_SECONDS,
                    abort=True,
                )
            return StopPolicySnapshot(
                graceful_timeout_seconds=coerce_graceful_timeout_seconds(
                    op.get("graceful_timeout_seconds")
                ),
                force=bool(
                    op.get("force_orchestrators")
                    or op.get("force_now_requested")
                ),
                abort=bool(op.get("abort_requested")),
            )


class GlobalShutdownOperation(TypedDict):
    """Mutable state for a Control Center-wide shutdown operation."""

    operation_id: str
    state: Literal["in_progress", "completed", "failed", "aborted"]
    started_at_epoch: float
    stop_orchestrators: bool
    force_orchestrators: bool
    graceful_timeout_seconds: int
    superseded_engine_shutdowns: list[str]
    current_repo: str | None
    total_repos: int
    completed_repos: int
    stopped_orchestrators: list[str]
    failed_orchestrators: list[str]
    abort_requested: bool
    force_now_requested: bool


class EngineShutdownOperation(TypedDict):
    """Mutable state for a single repository-engine shutdown operation."""

    repo_root: str
    state: Literal["in_progress"]
    started_at_epoch: float
    force: bool
    force_if_timeout: bool
    graceful_timeout_seconds: int


_shutdown_ops_lock = threading.Lock()
_global_shutdown_operation: GlobalShutdownOperation | None = None
_engine_shutdown_operations: dict[str, EngineShutdownOperation] = {}


def coerce_graceful_timeout_seconds(
    raw: object,
    default: int = DEFAULT_ENGINE_GRACEFUL_TIMEOUT_SECONDS,
) -> int:
    """Parse graceful timeout from API payload with safe bounds."""
    if raw is None:
        return default
    if not isinstance(raw, (bool, int, float, str)):
        return default
    try:
        parsed = int(float(raw))
    except (TypeError, ValueError):
        return default
    return min(max(parsed, 2), 3600)


def global_shutdown_in_progress() -> bool:
    """Return whether a global shutdown operation is currently active."""
    with _shutdown_ops_lock:
        op = _global_shutdown_operation
        if op is None:
            return False
        return op.get("state") == "in_progress"


def snapshot_shutdown_ops() -> dict[str, Any]:
    """Return a copy of current global and per-engine shutdown operations."""
    with _shutdown_ops_lock:
        return {
            "global_shutdown": dict(_global_shutdown_operation) if _global_shutdown_operation else None,
            "engine_shutdowns": [dict(op) for op in _engine_shutdown_operations.values()],
        }


def begin_engine_shutdown_operation(
    repo_root: Path,
    force: bool,
    force_if_timeout: bool,
    graceful_timeout_seconds: int,
) -> None:
    """Record that a single repository engine shutdown is in progress."""
    repo_key = str(repo_root)
    with _shutdown_ops_lock:
        _engine_shutdown_operations[repo_key] = {
            "repo_root": repo_key,
            "state": "in_progress",
            "started_at_epoch": time.time(),
            "force": force,
            "force_if_timeout": force_if_timeout,
            "graceful_timeout_seconds": graceful_timeout_seconds,
        }


def finish_engine_shutdown_operation(repo_root: Path) -> None:
    """Clear the operation state for a completed single-engine shutdown."""
    repo_key = str(repo_root)
    with _shutdown_ops_lock:
        _engine_shutdown_operations.pop(repo_key, None)


def begin_global_shutdown_operation(
    *,
    stop_orchestrators: bool,
    force_orchestrators: bool,
    graceful_timeout_seconds: int,
) -> tuple[str, list[str]] | GlobalShutdownConflict:
    """Create global shutdown operation state or report an existing operation."""
    global _global_shutdown_operation

    superseded_engine_shutdowns: list[str] = []
    global_op_id = f"shutdown-{int(time.time() * 1000)}"
    with _shutdown_ops_lock:
        if _global_shutdown_operation and _global_shutdown_operation.get("state") == "in_progress":
            return GlobalShutdownConflict(
                operation_id=_global_shutdown_operation.get("operation_id"),
            )
        if stop_orchestrators and _engine_shutdown_operations:
            superseded_engine_shutdowns = sorted(_engine_shutdown_operations.keys())
            _engine_shutdown_operations.clear()
        _global_shutdown_operation = {
            "operation_id": global_op_id,
            "state": "in_progress",
            "started_at_epoch": time.time(),
            "stop_orchestrators": bool(stop_orchestrators),
            "force_orchestrators": bool(force_orchestrators),
            "graceful_timeout_seconds": graceful_timeout_seconds,
            "superseded_engine_shutdowns": superseded_engine_shutdowns,
            "current_repo": None,
            "total_repos": 0,
            "completed_repos": 0,
            "stopped_orchestrators": [],
            "failed_orchestrators": [],
            "abort_requested": False,
            "force_now_requested": False,
        }
    return global_op_id, superseded_engine_shutdowns


def mark_global_shutdown_completed_without_orchestrators() -> None:
    """Mark a shutdown complete when no repository engines need to be stopped."""
    with _shutdown_ops_lock:
        if _global_shutdown_operation:
            _global_shutdown_operation["state"] = "completed"


def set_shutdown_total_repos(*, operation_id: str, total_repos: int) -> None:
    """Record how many repositories a global shutdown worker will inspect."""
    with _shutdown_ops_lock:
        op = _global_shutdown_operation
        if op and op.get("operation_id") == operation_id:
            op["total_repos"] = total_repos


def begin_shutdown_repo_stop(
    *, operation_id: str, repo_path: str
) -> GlobalShutdownStopPolicy | None:
    """Bind the live global operation policy to the current repository stop."""
    with _shutdown_ops_lock:
        op = _global_shutdown_operation
        if not op or op.get("operation_id") != operation_id:
            return None
        if op.get("abort_requested"):
            return None
        op["current_repo"] = repo_path
    return GlobalShutdownStopPolicy(operation_id=operation_id)


def increment_shutdown_completed_repos(*, operation_id: str) -> None:
    """Increment completed repository count for a global shutdown operation."""
    with _shutdown_ops_lock:
        op = _global_shutdown_operation
        if not op or op.get("operation_id") != operation_id:
            return
        op["completed_repos"] = int(op.get("completed_repos", 0)) + 1


def record_shutdown_abort(*, operation_id: str, stopped_repos: list[str], failed_repos: list[str]) -> None:
    """Record a user-requested global shutdown abort."""
    with _shutdown_ops_lock:
        op = _global_shutdown_operation
        if not op or op.get("operation_id") != operation_id:
            return
        op["state"] = "aborted"
        op["current_repo"] = None
        op["stopped_orchestrators"] = list(stopped_repos)
        op["failed_orchestrators"] = list(failed_repos)


def record_shutdown_completion(*, operation_id: str, stopped_repos: list[str], failed_repos: list[str]) -> bool:
    """Record global shutdown completion and return whether Control Center should exit."""
    with _shutdown_ops_lock:
        op = _global_shutdown_operation
        if not op or op.get("operation_id") != operation_id:
            return False
        op["stopped_orchestrators"] = list(stopped_repos)
        op["failed_orchestrators"] = list(failed_repos)
        op["current_repo"] = None
        if op.get("state") == "in_progress":
            op["state"] = "failed" if failed_repos else "completed"
    return not failed_repos


def record_shutdown_failure(*, operation_id: str) -> None:
    """Record an unexpected global shutdown worker failure."""
    with _shutdown_ops_lock:
        op = _global_shutdown_operation
        if not op or op.get("operation_id") != operation_id:
            return
        op["state"] = "failed"
        op["current_repo"] = None


def request_shutdown_abort() -> bool:
    """Request abort for an active global shutdown operation."""
    with _shutdown_ops_lock:
        op = _global_shutdown_operation
        if not op or op.get("state") != "in_progress":
            return False
        op["abort_requested"] = True
    return True


def update_shutdown_policy(
    *,
    graceful_timeout_seconds: int,
    force_orchestrators: bool | None,
) -> ShutdownPolicyUpdate | None:
    """Update timeout/force policy for an active global shutdown operation."""
    with _shutdown_ops_lock:
        op = _global_shutdown_operation
        if not op or op.get("state") != "in_progress":
            return None
        op["graceful_timeout_seconds"] = graceful_timeout_seconds
        if force_orchestrators is not None:
            op["force_orchestrators"] = force_orchestrators
        return ShutdownPolicyUpdate(
            graceful_timeout_seconds=graceful_timeout_seconds,
            force_orchestrators=bool(op.get("force_orchestrators", False)),
        )


def request_shutdown_force_now() -> bool:
    """Request force escalation for an active global shutdown operation."""
    with _shutdown_ops_lock:
        op = _global_shutdown_operation
        if not op or op.get("state") != "in_progress":
            return False
        op["force_now_requested"] = True
        op["force_orchestrators"] = True
    return True


def reset_shutdown_operations_for_testing() -> None:
    """Clear shutdown operation state for unit tests."""
    global _global_shutdown_operation

    with _shutdown_ops_lock:
        _global_shutdown_operation = None
        _engine_shutdown_operations.clear()
