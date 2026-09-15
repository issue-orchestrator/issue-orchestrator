"""Timeline store port for issue event traces."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class TimelineRecord:
    event_id: str
    timestamp: str
    event: str
    data: dict[str, Any]
    source_event: str = ""  # internal event name before fan-out
    instance_id: str = ""  # orchestrator instance UUID (restart boundary)


class TimelineStore(Protocol):
    """Port for persisting and reading per-issue timeline records."""

    def append(self, issue_number: int, record: TimelineRecord) -> None:
        """Append a record for an issue."""
        ...

    def read(self, issue_number: int, limit: int | None = None) -> list[TimelineRecord]:
        """Read timeline records for an issue."""
        ...

    def delete(self, issue_number: int) -> int:
        """Delete all timeline records for an issue. Returns count deleted."""
        ...

    def latest_event_timestamps(
        self, event_names: Sequence[str]
    ) -> Mapping[str, str]:
        """Newest recorded timestamp for each of ``event_names``, across issues.

        The store is keyed by issue, but "when did this KIND of thing last
        happen anywhere" is a real question about subsystem health that no
        per-issue read can answer -- #7080's write-death was invisible for ten
        days precisely because nothing asked it. Names with no recorded row are
        OMITTED rather than mapped to a sentinel, so a caller must decide what
        "never" means instead of receiving a timestamp that looks like an
        answer.
        """
        ...


class NullTimelineStore:
    """No-op timeline store for tests and disabled configurations."""

    def append(self, issue_number: int, record: TimelineRecord) -> None:  # noqa: ARG002
        return None

    def read(self, issue_number: int, limit: int | None = None) -> list[TimelineRecord]:  # noqa: ARG002
        return []

    def delete(self, issue_number: int) -> int:  # noqa: ARG002
        return 0

    def latest_event_timestamps(self, event_names: Sequence[str]) -> Mapping[str, str]:
        """No records, so no requested name has a newest timestamp.

        The argument is discarded explicitly rather than suppressed with a
        ``noqa``: the port's contract is that a name with no rows is OMITTED,
        and an empty mapping is that answer for every name.
        """
        del event_names
        return {}
