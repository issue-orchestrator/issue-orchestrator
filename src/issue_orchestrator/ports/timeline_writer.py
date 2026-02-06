"""Timeline writer port for recording issue history."""

from __future__ import annotations

from typing import Protocol

from .event_sink import TraceEvent


class TimelineWriter(Protocol):
    """Port for recording timeline entries."""

    def record(self, event: TraceEvent) -> None:
        """Record a trace event in the timeline."""
        ...


class NullTimelineWriter:
    """No-op timeline writer for tests and disabled configurations."""

    def record(self, event: TraceEvent) -> None:  # noqa: ARG002
        return None
