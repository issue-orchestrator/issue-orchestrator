"""Timeline reader port for higher-level issue history access."""

from __future__ import annotations

from typing import Protocol

from ..timeline import TimelineStream


class TimelineReader(Protocol):
    """Port for reading timeline streams."""

    def read(self, issue_number: int, limit: int | None = None) -> TimelineStream:
        """Return a timeline stream for an issue."""
        ...


class NullTimelineReader:
    """No-op timeline reader for tests and disabled configurations."""

    def read(self, issue_number: int, limit: int | None = None) -> TimelineStream:  # noqa: ARG002
        return TimelineStream(issue_number=issue_number, events=[])
