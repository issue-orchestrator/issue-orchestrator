"""Configured repository lookup for Control Center policy owners."""

from typing import Protocol

from ..domain.control_center_recovery import ConfiguredRepository


class ConfiguredRepositoryRegistry(Protocol):
    def resolve(self, repo_key: str) -> ConfiguredRepository | None:
        """Resolve an opaque public key without accepting caller filesystem scope."""
        ...
