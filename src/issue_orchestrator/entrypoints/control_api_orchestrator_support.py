"""Dependency wiring for Control Center orchestrator route modules."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


@dataclass(frozen=True)
class ControlApiOrchestratorDependencies:
    """Dependency hooks needed by Control Center orchestrator routes."""

    get_supervisor: Callable[[], Any]
    get_control_actions: Callable[[], Any]
    validate_repo_root: Callable[[str | None], Path | None]
    track_launched_pids: Callable[[dict[str, Any]], None]
    coerce_graceful_timeout_seconds: Callable[[Any, int], int]
    global_shutdown_in_progress: Callable[[], bool]
    begin_engine_shutdown_operation: Callable[[Path, bool, bool, int], None]
    finish_engine_shutdown_operation: Callable[[Path], None]


_deps: ControlApiOrchestratorDependencies | None = None


def configure_control_api_orchestrator_dependencies(
    deps: ControlApiOrchestratorDependencies,
) -> None:
    """Install shared dependencies for Control Center orchestrator routes."""
    global _deps
    _deps = deps


def _require_deps() -> ControlApiOrchestratorDependencies:
    if _deps is None:
        raise RuntimeError("Control Center orchestrator dependencies not configured")
    return _deps


def get_control_api_orchestrator_supervisor() -> Any:
    return _require_deps().get_supervisor()


def get_control_api_control_actions() -> Any:
    return _require_deps().get_control_actions()


def validate_control_api_orchestrator_repo_root(repo_root: str | None) -> Path | None:
    return _require_deps().validate_repo_root(repo_root)


def track_control_api_launched_pids(supervisor_data: dict[str, Any]) -> None:
    _require_deps().track_launched_pids(supervisor_data)


def coerce_control_api_graceful_timeout_seconds(raw: Any, default: int = 2) -> int:
    return _require_deps().coerce_graceful_timeout_seconds(raw, default)


def control_api_global_shutdown_in_progress() -> bool:
    return _require_deps().global_shutdown_in_progress()


def begin_control_api_engine_shutdown_operation(
    repo_root: Path,
    *,
    force: bool,
    force_if_timeout: bool,
    graceful_timeout_seconds: int,
) -> None:
    _require_deps().begin_engine_shutdown_operation(
        repo_root,
        force,
        force_if_timeout,
        graceful_timeout_seconds,
    )


def finish_control_api_engine_shutdown_operation(repo_root: Path) -> None:
    _require_deps().finish_engine_shutdown_operation(repo_root)


__all__ = [
    "ControlApiOrchestratorDependencies",
    "begin_control_api_engine_shutdown_operation",
    "coerce_control_api_graceful_timeout_seconds",
    "configure_control_api_orchestrator_dependencies",
    "control_api_global_shutdown_in_progress",
    "finish_control_api_engine_shutdown_operation",
    "get_control_api_control_actions",
    "get_control_api_orchestrator_supervisor",
    "track_control_api_launched_pids",
    "validate_control_api_orchestrator_repo_root",
]
