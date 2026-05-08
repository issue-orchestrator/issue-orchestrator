"""Execution adapter for writing timeline entries to storage."""

from __future__ import annotations

import logging
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any
from uuid import uuid4

from ..domain.logical_event_semantics import enrich_logical_semantics
from ..events.catalog import EVENT_SCHEMA_VERSION
from ..events.fan_out_pipeline import produce_external_records
from ..infra.timeline_trace import is_timeline_trace_enabled
from ..timeline import TIMELINE_SCHEMA_VERSION, validate_timeline_artifact_refs
from .timeline_artifact_expectations import validate_event_artifact_expectations
from ..ports.event_sink import TraceEvent
from ..ports.timeline_store import TimelineStore
from ..ports.timeline_writer import TimelineWriter

logger = logging.getLogger(__name__)


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
        if not isinstance(safe_data, dict):
            safe_data = {"raw_event_data": safe_data}

        # Enrichment uses source_event from the previous record for cycle
        # boundary detection, so fan-out doesn't break the state machine.
        previous_records = self._store.read(issue_number, limit=1)
        previous_record = previous_records[-1] if previous_records else None
        previous_source = (
            (previous_record.source_event or previous_record.event)
            if previous_record else None
        )
        previous_data = previous_record.data if previous_record else None
        # Instance ID for restart boundary detection
        current_instance_id = getattr(self._store, "instance_id", "")
        previous_instance_id = previous_record.instance_id if previous_record else ""
        semantics = enrich_logical_semantics(
            event_name=event.name,
            event_data=safe_data,
            previous_event_name=previous_source,
            previous_data=previous_data if isinstance(previous_data, dict) else None,
            current_instance_id=current_instance_id,
            previous_instance_id=previous_instance_id,
        )
        safe_data["schema"] = EVENT_SCHEMA_VERSION
        safe_data["timeline_schema_version"] = TIMELINE_SCHEMA_VERSION
        safe_data["event_intent"] = semantics.event_intent
        safe_data["review_oriented"] = semantics.review_oriented
        safe_data["logical_run"] = semantics.logical_run
        safe_data["logical_cycle"] = semantics.logical_cycle
        safe_data["logical_phase"] = semantics.logical_phase
        safe_data["_logical_restart_pending"] = semantics.restart_pending
        safe_data["_logical_rework_driven"] = semantics.rework_driven
        validate_timeline_artifact_refs(safe_data)
        validate_event_artifact_expectations(event.name, safe_data)

        base_event_id = str(event.event_id) if event.event_id is not None else str(uuid4())
        ts_iso = timestamp.isoformat()

        if is_timeline_trace_enabled():
            logger.info(
                "[TIMELINE] writer.record issue=%s event=%s run_id=%s run_dir=%s "
                "intent=%s phase=%s cycle=%s run=%s previous_source=%s",
                issue_number,
                event.name,
                safe_data.get("run_id"),
                safe_data.get("run_dir"),
                safe_data.get("event_intent"),
                safe_data.get("logical_phase"),
                safe_data.get("logical_cycle"),
                safe_data.get("logical_run"),
                previous_source,
            )

        # Single canonical fan-out surface — used here for production and
        # by golden tests via the same `produce_external_records()`. Any
        # change to fan-out / narrative / phase-override policy lands in
        # one place.
        for record in produce_external_records(
            internal_event_name=event.name,
            enriched_data=safe_data,
            base_event_id=base_event_id,
            timestamp_iso=ts_iso,
        ):
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
