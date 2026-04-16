"""Dependency wiring for Control API issue action routes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Callable, Protocol, TypeVar

from fastapi import Depends, FastAPI, Request

_ISSUE_DEPENDENCIES_STATE_KEY = "control_api_issue_dependencies"

if TYPE_CHECKING:
    from ..infra.orchestrator import Orchestrator

T = TypeVar("T")


class StateLockFn(Protocol):
    def __call__(self, fn: Callable[[], T], /) -> T: ...


@dataclass(frozen=True)
class ControlApiIssueDependencies:
    """Dependency hooks needed by issue action routes."""

    get_orchestrator: Callable[[], Orchestrator | None]
    with_state_lock: StateLockFn


def install_control_api_issue_dependencies(
    app: FastAPI,
    deps: ControlApiIssueDependencies,
) -> None:
    """Install shared dependencies for issue action routes."""
    setattr(app.state, _ISSUE_DEPENDENCIES_STATE_KEY, deps)


def get_control_api_issue_dependencies(request: Request) -> ControlApiIssueDependencies:
    """Resolve issue route dependencies from the FastAPI application state."""
    deps = getattr(request.app.state, _ISSUE_DEPENDENCIES_STATE_KEY, None)
    if deps is None:
        raise RuntimeError("Control API issue dependencies not configured")
    return deps


ControlApiIssueDependency = Annotated[
    ControlApiIssueDependencies,
    Depends(get_control_api_issue_dependencies),
]


__all__ = [
    "ControlApiIssueDependencies",
    "ControlApiIssueDependency",
    "StateLockFn",
    "get_control_api_issue_dependencies",
    "install_control_api_issue_dependencies",
]
