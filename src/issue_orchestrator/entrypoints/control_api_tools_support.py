"""Dependency wiring for Control Center tool routes."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Callable

from fastapi import Depends, FastAPI, Request

_TOOLS_DEPENDENCIES_STATE_KEY = "control_api_tools_dependencies"

if TYPE_CHECKING:
    from ..execution.control_center_actions import ControlCenterActions


@dataclass(frozen=True)
class ControlApiToolsDependencies:
    """Dependency hooks needed by Control Center tool routes."""

    get_control_actions: Callable[[], ControlCenterActions]
    validate_repo_root: Callable[[str | None], Path | None]


def install_control_api_tools_dependencies(
    app: FastAPI,
    deps: ControlApiToolsDependencies,
) -> None:
    """Install shared dependencies for Control Center tool routes."""
    setattr(app.state, _TOOLS_DEPENDENCIES_STATE_KEY, deps)


def get_control_api_tools_dependencies(request: Request) -> ControlApiToolsDependencies:
    """Resolve tool route dependencies from the FastAPI application state."""
    deps = getattr(request.app.state, _TOOLS_DEPENDENCIES_STATE_KEY, None)
    if deps is None:
        raise RuntimeError("Control Center tool dependencies not configured")
    return deps


ControlApiToolsDependency = Annotated[
    ControlApiToolsDependencies,
    Depends(get_control_api_tools_dependencies),
]


__all__ = [
    "ControlApiToolsDependencies",
    "ControlApiToolsDependency",
    "get_control_api_tools_dependencies",
    "install_control_api_tools_dependencies",
]
