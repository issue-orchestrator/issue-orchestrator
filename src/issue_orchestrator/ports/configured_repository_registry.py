"""Configured repository lookup for Control Center policy owners."""

from typing import Protocol

from ..domain.control_center_recovery import ConfiguredRepository


class SelectedRepositoryConfigMissingError(FileNotFoundError):
    """The registered repository's selected configuration is no longer present."""


class ConfiguredRepositoryRegistry(Protocol):
    def resolve(self, repo_key: str) -> ConfiguredRepository | None:
        """Resolve an opaque public key without accepting caller filesystem scope."""
        ...
