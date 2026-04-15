"""Shared dependency wiring for Control Center E2E routers."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


@dataclass(frozen=True)
class ControlApiE2EDependencies:
    """Dependency hooks needed by Control Center E2E route modules."""

    get_orchestrator: Callable[[], Any | None]
    load_config_by_name: Callable[[Path, str], Any]
    validate_repo_root: Callable[[str | None], Path | None]


_deps: ControlApiE2EDependencies | None = None


def configure_control_api_e2e_dependencies(
    deps: ControlApiE2EDependencies,
) -> None:
    """Install shared dependencies for Control Center E2E routers."""
    global _deps
    _deps = deps


def _require_deps() -> ControlApiE2EDependencies:
    if _deps is None:
        raise RuntimeError("Control Center E2E dependencies not configured")
    return _deps


def get_control_api_orchestrator() -> Any | None:
    """Return the configured control API orchestrator accessor."""
    return _require_deps().get_orchestrator()


def load_control_api_config_by_name(repo_root: Path, config_name: str) -> Any:
    """Load control API config through the configured dependency hook."""
    return _require_deps().load_config_by_name(repo_root, config_name)


def validate_control_api_repo_root(repo_root: str | None) -> Path | None:
    """Validate repo roots through the configured dependency hook."""
    return _require_deps().validate_repo_root(repo_root)


__all__ = [
    "ControlApiE2EDependencies",
    "configure_control_api_e2e_dependencies",
    "get_control_api_orchestrator",
    "load_control_api_config_by_name",
    "validate_control_api_repo_root",
]
