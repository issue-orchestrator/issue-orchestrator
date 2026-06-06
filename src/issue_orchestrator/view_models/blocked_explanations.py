"""Drawer-facing blocked explanation helpers."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def invalid_or_validation_blocked_explanation(
    *,
    event_name: str,
    event_summary: str,
    labels: tuple[str, ...],
    event: Mapping[str, Any] | None = None,
) -> str | None:
    if _is_invalid_completion_event(event_name, event):
        return f"Completion record rejected \u2014 {_invalid_completion_reason(event_summary, event)}"
    if event_name == "session.validation_failed" or "validation-failed" in labels:
        reason = event_summary or "project tests did not pass"
        return f"Validation failed \u2014 {reason}"
    return None


def _is_invalid_completion_event(
    event_name: str,
    event: Mapping[str, Any] | None,
) -> bool:
    return event_name == "session.invalid_completion_record" or (
        event_name == "session.failed"
        and event is not None
        and event.get("failure_kind") == "invalid_completion_record"
    )


def _invalid_completion_reason(
    event_summary: str,
    event: Mapping[str, Any] | None,
) -> str:
    if event_summary:
        return event_summary.removeprefix("Completion record rejected:").strip()
    if event is not None:
        parse_error = event.get("completion_parse_error")
        if isinstance(parse_error, str) and parse_error.strip():
            return parse_error.strip()
    return "completion JSON did not pass validation"
