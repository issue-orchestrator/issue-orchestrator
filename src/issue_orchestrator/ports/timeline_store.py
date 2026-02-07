"""Timeline store port for issue event traces."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class TimelineRecord:
    event_id: str
    timestamp: str
    event: str
    data: dict[str, Any]


class TimelineStore(Protocol):
    """Port for persisting and reading per-issue timeline records."""

    def append(self, issue_number: int, record: TimelineRecord) -> None:
        """Append a record for an issue."""
        ...

    def read(self, issue_number: int, limit: int | None = None) -> list[TimelineRecord]:
        """Read timeline records for an issue."""
        ...


class NullTimelineStore:
    """No-op timeline store for tests and disabled configurations."""

    def append(self, issue_number: int, record: TimelineRecord) -> None:  # noqa: ARG002
        return None

    def read(self, issue_number: int, limit: int | None = None) -> list[TimelineRecord]:  # noqa: ARG002
        return []
