"""Execution adapter for writing timeline entries to storage."""

from __future__ import annotations

from datetime import timezone
from uuid import uuid4

from ..ports.event_sink import TraceEvent
from ..ports.timeline_store import TimelineRecord, TimelineStore
from ..ports.timeline_writer import TimelineWriter


class DefaultTimelineWriter(TimelineWriter):
    """Record timeline entries in a TimelineStore."""

    def __init__(self, store: TimelineStore):
        self._store = store

    def record(self, event: TraceEvent) -> None:
        issue_number = event.data.get("issue_number")
        if not isinstance(issue_number, int):
            return
        timestamp = event.timestamp
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        timestamp = timestamp.astimezone(timezone.utc)
        record = TimelineRecord(
            event_id=str(uuid4()),
            timestamp=timestamp.isoformat(),
            event=event.name,
            data=event.data,
        )
        self._store.append(issue_number, record)
