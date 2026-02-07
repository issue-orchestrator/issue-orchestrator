"""Execution adapter for reading timeline streams from storage."""

from __future__ import annotations

from ..ports.timeline_reader import TimelineReader
from ..ports.timeline_store import TimelineStore
from ..timeline import TimelineStream


class DefaultTimelineReader(TimelineReader):
    """Read timeline streams from a TimelineStore."""

    def __init__(self, store: TimelineStore):
        self._store = store

    def read(self, issue_number: int, limit: int | None = None) -> TimelineStream:
        records = self._store.read(issue_number, limit=limit)
        return TimelineStream.from_records(issue_number, records)
