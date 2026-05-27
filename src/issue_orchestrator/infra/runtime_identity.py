"""Resolve the identity of the running orchestrator process."""

from __future__ import annotations

from .. import __version__
from ..domain.runtime_identity import RuntimeIdentity
from .static_version import resolve_cc_commit_sha


def resolve_runtime_identity() -> RuntimeIdentity:
    """Return package and source identity for the running orchestrator."""
    return RuntimeIdentity(
        package_version=__version__,
        source_commit_sha=resolve_cc_commit_sha(),
    )


__all__ = ["RuntimeIdentity", "resolve_runtime_identity"]
