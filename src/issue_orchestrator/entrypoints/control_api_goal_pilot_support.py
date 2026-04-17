"""Dependency wiring for Control Center Goal Pilot routes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Callable

from fastapi import Depends, FastAPI, Request

_GOAL_PILOT_DEPENDENCIES_STATE_KEY = "control_api_goal_pilot_dependencies"

if TYPE_CHECKING:
    from ..control.goal_pilot import GoalPilot
    from ..infra.orchestrator import Orchestrator


@dataclass(frozen=True)
class ControlApiGoalPilotDependencies:
    """Dependency hooks needed by Control Center Goal Pilot routes."""

    get_orchestrator: Callable[[], Orchestrator | None]
    get_goal_pilot: Callable[[], GoalPilot]


def install_control_api_goal_pilot_dependencies(
    app: FastAPI,
    deps: ControlApiGoalPilotDependencies,
) -> None:
    """Install shared dependencies for Control Center Goal Pilot routes."""
    setattr(app.state, _GOAL_PILOT_DEPENDENCIES_STATE_KEY, deps)


def get_control_api_goal_pilot_dependencies(request: Request) -> ControlApiGoalPilotDependencies:
    """Resolve Goal Pilot route dependencies from the FastAPI application state."""
    deps = getattr(request.app.state, _GOAL_PILOT_DEPENDENCIES_STATE_KEY, None)
    if deps is None:
        raise RuntimeError("Control Center Goal Pilot dependencies not configured")
    return deps


ControlApiGoalPilotDependency = Annotated[
    ControlApiGoalPilotDependencies,
    Depends(get_control_api_goal_pilot_dependencies),
]


__all__ = [
    "ControlApiGoalPilotDependencies",
    "ControlApiGoalPilotDependency",
    "get_control_api_goal_pilot_dependencies",
    "install_control_api_goal_pilot_dependencies",
]
