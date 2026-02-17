"""Execution adapter for writing timeline entries to storage."""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any
from uuid import uuid4

from ..events.catalog import EVENT_SCHEMA_VERSION
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
        safe_data = _normalize_json(event.data)
        if isinstance(safe_data, dict) and "schema" not in safe_data:
            safe_data["schema"] = EVENT_SCHEMA_VERSION
        record_event_id = str(event.event_id) if event.event_id is not None else str(uuid4())
        record = TimelineRecord(
            event_id=record_event_id,
            timestamp=timestamp.isoformat(),
            event=event.name,
            data=safe_data,
        )
        self._store.append(issue_number, record)


def _normalize_json(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Enum):
        return str(value)
    if is_dataclass(value) and not isinstance(value, type):
        return _normalize_json(asdict(value))
    if isinstance(value, dict):
        return {str(k): _normalize_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_normalize_json(item) for item in value]
    return str(value)
