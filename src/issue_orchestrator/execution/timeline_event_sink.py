"""EventSink adapter that records timeline events."""

from __future__ import annotations

from ..ports.event_sink import EventSink, TraceEvent
from ..ports.timeline_writer import TimelineWriter


class TimelineEventSink(EventSink):
    """Persist trace events to the timeline writer."""

    def __init__(self, writer: TimelineWriter):
        self._writer = writer

    def publish(self, event: TraceEvent) -> None:
        self._writer.record(event)
