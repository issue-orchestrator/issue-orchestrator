"""State/event-based artifact expectations for timeline records."""

from __future__ import annotations

from pathlib import Path
from typing import Any

RUN_SCOPED_TIMELINE_EVENTS: frozenset[str] = frozenset({
    "session.started",
    "session.processing_completed",
    "session.validation_passed",
    "session.validation_retry_needed",
    "session.validation_failed",
    "review.started",
    "rework.started",
})


def event_requires_run_dir(event_name: str) -> bool:
    """Return True if this event must carry run_dir."""
    return event_name in RUN_SCOPED_TIMELINE_EVENTS


def validate_event_artifact_expectations(event_name: str, payload: dict[str, Any]) -> None:
    """Validate required artifacts for key timeline events.

    This enforces workflow-level invariants. Resolver classes (e.g. ManifestAccessor)
    should not decide policy; they only resolve artifacts.
    """
    if event_requires_run_dir(event_name):
        _require_non_empty_field(event_name, payload, "run_dir")

    if event_name in {"session.started", "review.started", "rework.started"}:
        _require_run_dir_with_session_log(event_name, payload)
        return

    if event_name == "session.completed":
        _require_existing_path(event_name, payload, "completion_path_absolute")
        return

    if event_name == "review.comment_added":
        _require_review_feedback_reference(event_name, payload)
        return


def _require_run_dir_with_session_log(event_name: str, payload: dict[str, Any]) -> None:
    run_dir = _required_path_value(event_name, payload, "run_dir")
    if not run_dir.exists():
        raise RuntimeError(
            f"timeline artifact invariant failed: event={event_name} run_dir_missing={run_dir}"
        )
    session_log = run_dir / "session.log"
    if not session_log.exists():
        raise RuntimeError(
            f"timeline artifact invariant failed: event={event_name} session_log_missing={session_log}"
        )


def _require_existing_path(event_name: str, payload: dict[str, Any], field: str) -> None:
    path = _required_path_value(event_name, payload, field)
    if not path.exists():
        raise RuntimeError(
            f"timeline artifact invariant failed: event={event_name} field={field} missing_path={path}"
        )


def _required_path_value(event_name: str, payload: dict[str, Any], field: str) -> Path:
    raw = payload.get(field)
    if not isinstance(raw, str) or not raw.strip():
        raise RuntimeError(
            f"timeline artifact invariant failed: event={event_name} missing_field={field}"
        )
    return Path(raw)


def _require_non_empty_field(event_name: str, payload: dict[str, Any], field: str) -> None:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(
            f"timeline artifact invariant failed: event={event_name} missing_field={field}"
        )


def _require_review_feedback_reference(event_name: str, payload: dict[str, Any]) -> None:
    """Require either a GitHub review URL or in-app feedback text."""
    comment_url = payload.get("comment_url")
    summary = payload.get("summary")
    excerpt = payload.get("comment_excerpt")
    has_url = isinstance(comment_url, str) and bool(comment_url.strip())
    has_text = (
        (isinstance(summary, str) and bool(summary.strip()))
        or (isinstance(excerpt, str) and bool(excerpt.strip()))
    )
    if not has_url and not has_text:
        raise RuntimeError(
            "timeline artifact invariant failed: event="
            f"{event_name} missing_review_feedback_reference"
        )
