"""Execution adapter for writing timeline entries to storage."""

from __future__ import annotations

import logging
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from ..domain.logical_event_semantics import enrich_logical_semantics
from ..events.catalog import EVENT_SCHEMA_VERSION
from ..events.view_registry import fan_out
from ..infra.timeline_trace import is_timeline_trace_enabled
from ..timeline import TIMELINE_SCHEMA_VERSION
from .timeline_artifact_expectations import validate_event_artifact_expectations
from ..ports.event_sink import TraceEvent
from ..ports.timeline_store import TimelineRecord, TimelineStore
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

        # Fan out: one internal event -> N external timeline records
        external_events = fan_out(event.name)
        for i, view_event in enumerate(external_events):
            record_data = dict(safe_data)
            record_data["views"] = sorted(view_event.views)
            if view_event.narrative:
                record_data["narrative"] = _enrich_narrative(
                    view_event.narrative, event.name, safe_data,
                )
            if view_event.phase:
                enriched_phase = record_data.get("logical_phase", "system")
                # Don't override if enrichment already promoted the phase
                # (e.g. coding→rework for session.started in a rework cycle).
                if enriched_phase != "rework" or view_event.phase != "coding":
                    record_data["logical_phase"] = view_event.phase

            record_id = base_event_id if i == 0 else f"{base_event_id}-{i}"
            record = TimelineRecord(
                event_id=record_id,
                timestamp=ts_iso,
                event=view_event.name,
                data=record_data,
                source_event=event.name,
            )
            self._store.append(issue_number, record)


def _enrich_narrative(
    narrative: str,
    internal_event: str,
    data: dict[str, Any],
) -> str:
    """Enrich a static narrative with dynamic event data.

    Injects round numbers, PR numbers, and review round counts into
    the narrative at write time so the stored timeline is self-describing.
    """
    enricher = _NARRATIVE_ENRICHERS.get(internal_event)
    if enricher is not None:
        return enricher(data) or narrative
    return narrative


def _enrich_round_started(data: dict[str, Any]) -> str | None:
    ri = data.get("round_index")
    return f"Review round {ri} started" if isinstance(ri, int) else None


def _enrich_round_completed(data: dict[str, Any]) -> str | None:
    ri = data.get("round_index")
    if not isinstance(ri, int):
        return None
    verdict = data.get("reviewer_response_type")
    suffix = f" — {verdict}" if isinstance(verdict, str) and verdict else ""
    return f"Review round {ri} completed{suffix}"


def _enrich_session_started(data: dict[str, Any]) -> str | None:
    if data.get("reset_from_scratch"):
        return "Scratch coding agent started"
    return None


def _enrich_issue_unblocked(data: dict[str, Any]) -> str | None:
    if data.get("from_scratch"):
        return "Scratch reset requested"
    return None


def _enrich_review_started(data: dict[str, Any]) -> str | None:
    if data.get("cached"):
        return "Cached review result reused for unchanged commit"
    return None


def _enrich_review_approved(data: dict[str, Any]) -> str | None:
    if data.get("cached"):
        return "Cached review approval reused for unchanged commit"
    rounds = data.get("rounds")
    return f"Review approved after {rounds} rounds" if isinstance(rounds, int) and rounds > 1 else None


def _enrich_review_rework_started(data: dict[str, Any]) -> str | None:
    ri = data.get("round_index")
    return f"Coder started rework for review round {ri}" if isinstance(ri, int) else None


def _enrich_review_rework_completed(data: dict[str, Any]) -> str | None:
    ri = data.get("round_index")
    return f"Coder completed rework for review round {ri}" if isinstance(ri, int) else None


def _enrich_changes_requested(data: dict[str, Any]) -> str | None:
    if data.get("cached"):
        return "Cached changes-requested verdict reused for unchanged commit"
    rounds = data.get("rounds")
    return f"Reviewer requested changes (round {rounds})" if isinstance(rounds, int) else None


def _enrich_pr_created(data: dict[str, Any]) -> str | None:
    pr = data.get("pr_number")
    return f"PR #{pr} created" if isinstance(pr, int) else None


def _enrich_exchange_completed(data: dict[str, Any]) -> str | None:
    rounds = data.get("rounds")
    return f"Review exchange completed ({rounds} rounds)" if isinstance(rounds, int) else None


_NARRATIVE_ENRICHERS: dict[str, Callable[[dict[str, Any]], str | None]] = {
    "session.started": _enrich_session_started,
    "issue.unblocked": _enrich_issue_unblocked,
    "review.started": _enrich_review_started,
    "review_exchange.round_started": _enrich_round_started,
    "review_exchange.round_completed": _enrich_round_completed,
    "review.rework_started": _enrich_review_rework_started,
    "review.rework_completed": _enrich_review_rework_completed,
    "review.approved": _enrich_review_approved,
    "review.changes_requested": _enrich_changes_requested,
    "issue.pr_created": _enrich_pr_created,
    "review_exchange.completed": _enrich_exchange_completed,
}


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
