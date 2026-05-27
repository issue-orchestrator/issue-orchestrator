"""Resolve the identity of the running orchestrator process."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

from ..domain.runtime_identity import RuntimeIdentity
from .static_version import resolve_cc_commit_sha


def resolve_runtime_identity() -> RuntimeIdentity:
    """Return package and source identity for the running orchestrator."""
    return RuntimeIdentity(
        package_version=_resolve_package_version(),
        source_commit_sha=resolve_cc_commit_sha(),
    )


def _resolve_package_version() -> str:
    try:
        return version("issue-orchestrator")
    except PackageNotFoundError:
        return "0+unknown"


__all__ = ["RuntimeIdentity", "resolve_runtime_identity"]

