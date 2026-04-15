"""Dependency wiring for Control Center orchestrator route modules."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Annotated, Callable

from fastapi import Depends, FastAPI, Request

_ORCHESTRATOR_DEPENDENCIES_STATE_KEY = "control_api_orchestrator_dependencies"

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


def install_control_api_orchestrator_dependencies(
    app: FastAPI,
    deps: ControlApiOrchestratorDependencies,
) -> None:
    """Install shared dependencies for Control Center orchestrator routes."""
    setattr(app.state, _ORCHESTRATOR_DEPENDENCIES_STATE_KEY, deps)


def get_control_api_orchestrator_dependencies(request: Request) -> ControlApiOrchestratorDependencies:
    """Resolve router dependencies from the FastAPI application state."""
    deps = getattr(request.app.state, _ORCHESTRATOR_DEPENDENCIES_STATE_KEY, None)
    if deps is None:
        raise RuntimeError("Control Center orchestrator dependencies not configured")
    return deps


ControlApiOrchestratorDependency = Annotated[
    ControlApiOrchestratorDependencies,
    Depends(get_control_api_orchestrator_dependencies),
]


__all__ = [
    "ControlApiOrchestratorDependency",
    "ControlApiOrchestratorDependencies",
    "get_control_api_orchestrator_dependencies",
    "install_control_api_orchestrator_dependencies",
]
