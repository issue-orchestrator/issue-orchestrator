"""Execution adapter for writing timeline entries to storage."""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any
from uuid import uuid4

from ..domain.logical_event_semantics import enrich_logical_semantics
from ..events.catalog import EVENT_SCHEMA_VERSION
from ..timeline import TIMELINE_SCHEMA_VERSION
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
        previous_records = self._store.read(issue_number, limit=1)
        previous_record = previous_records[-1] if previous_records else None
        previous_name = previous_record.event if previous_record else None
        previous_data = previous_record.data if previous_record else None
        semantics = enrich_logical_semantics(
            event_name=event.name,
            event_data=safe_data if isinstance(safe_data, dict) else {},
            previous_event_name=previous_name,
            previous_data=previous_data if isinstance(previous_data, dict) else None,
        )
        if isinstance(safe_data, dict) and "schema" not in safe_data:
            safe_data["schema"] = EVENT_SCHEMA_VERSION
        if isinstance(safe_data, dict) and "timeline_schema_version" not in safe_data:
            safe_data["timeline_schema_version"] = TIMELINE_SCHEMA_VERSION
        if isinstance(safe_data, dict):
            safe_data["event_intent"] = semantics.event_intent
            safe_data["review_oriented"] = semantics.review_oriented
            safe_data["logical_run"] = semantics.logical_run
            safe_data["logical_cycle"] = semantics.logical_cycle
            safe_data["logical_phase"] = semantics.logical_phase
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
