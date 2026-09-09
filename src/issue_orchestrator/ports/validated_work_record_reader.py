"""Cold retained-work discovery without a live Repository Engine."""

from typing import Protocol

from ..domain.validated_work_discovery import (
    ValidatedWorkDiscovery,
    ValidatedWorkSnapshot,
)


class ValidatedWorkRecordReader(Protocol):
    def discover_repository(self, repo_root: str) -> ValidatedWorkDiscovery:
        """Read all unresolved or owned records in one supported-schema snapshot."""
        ...

    def snapshot_record(
        self, repo_root: str, record_id: str
    ) -> ValidatedWorkSnapshot | None:
        """Return None only when a successful read finds no such record."""
        ...
