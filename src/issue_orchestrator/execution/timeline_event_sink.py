"""EventSink adapter that records timeline events."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from ..ports.event_sink import EventSink, TraceEvent
from ..ports.timeline_store import TimelineRecord, TimelineStore


class TimelineEventSink(EventSink):
    """Persist trace events to a timeline store by issue number."""

    def __init__(self, store: TimelineStore):
        self._store = store

    def publish(self, event: TraceEvent) -> None:
        issue_number = event.data.get("issue_number")
        if not isinstance(issue_number, int):
            return
        record = TimelineRecord(
            event_id=str(uuid4()),
            timestamp=datetime.now(timezone.utc).isoformat(),
            event=event.name,
            data=event.data,
        )
        self._store.append(issue_number, record)
