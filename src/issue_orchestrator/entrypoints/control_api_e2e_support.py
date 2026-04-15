"""Shared dependency wiring for Control Center E2E routers."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Annotated, Callable

from fastapi import Depends, FastAPI, Request

_E2E_DEPENDENCIES_STATE_KEY = "control_api_e2e_dependencies"

@dataclass(frozen=True)
class ControlApiE2EDependencies:
    """Dependency hooks needed by Control Center E2E route modules."""

    get_orchestrator: Callable[[], Any | None]
    load_config_by_name: Callable[[Path, str], Any]
    validate_repo_root: Callable[[str | None], Path | None]


def install_control_api_e2e_dependencies(
    app: FastAPI,
    deps: ControlApiE2EDependencies,
) -> None:
    """Install shared dependencies for Control Center E2E routers."""
    setattr(app.state, _E2E_DEPENDENCIES_STATE_KEY, deps)


def get_control_api_e2e_dependencies(request: Request) -> ControlApiE2EDependencies:
    """Resolve router dependencies from the FastAPI application state."""
    deps = getattr(request.app.state, _E2E_DEPENDENCIES_STATE_KEY, None)
    if deps is None:
        raise RuntimeError("Control Center E2E dependencies not configured")
    return deps


ControlApiE2EDependency = Annotated[
    ControlApiE2EDependencies,
    Depends(get_control_api_e2e_dependencies),
]


__all__ = [
    "ControlApiE2EDependency",
    "ControlApiE2EDependencies",
    "get_control_api_e2e_dependencies",
    "install_control_api_e2e_dependencies",
]
