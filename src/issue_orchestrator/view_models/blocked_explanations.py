"""Drawer-facing blocked explanation helpers."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

_INVALID_RECORD_KIND = "invalid_completion_record"
_INVALID_RECORD_SUMMARY_PREFIX = "Completion record rejected:"


def invalid_or_validation_blocked_explanation(
    *,
    event_name: str,
    event_summary: str,
    labels: tuple[str, ...],
    event: Mapping[str, Any] | None = None,
) -> str | None:
    if _is_bad_record_event(event_name, event_summary, event):
        return f"Completion record rejected \u2014 {_invalid_completion_reason(event_summary, event)}"
    if _is_gate_block(event_name, labels):
        detail_text = event_summary or "project tests did not pass"
        return f"Validation failed \u2014 {detail_text}"
    return None


def _is_bad_record_event(
    event_name: str,
    event_summary: str,
    event: Mapping[str, Any] | None,
) -> bool:
    return event_name == "session.invalid_completion_record" or (
        event_name == "session.failed"
        and (
            _event_marks_invalid_record(event)
            or event_summary.startswith(_INVALID_RECORD_SUMMARY_PREFIX)
        )
    )


def _is_gate_block(event_name: str, labels: tuple[str, ...]) -> bool:
    return event_name == "session.validation_failed" or "validation-failed" in labels


def _event_marks_invalid_record(event: Mapping[str, Any] | None) -> bool:
    return event is not None and event.get("failure_kind") == _INVALID_RECORD_KIND


def _invalid_completion_reason(
    event_summary: str,
    event: Mapping[str, Any] | None,
) -> str:
    if event_summary:
        detail_text = event_summary.removeprefix(_INVALID_RECORD_SUMMARY_PREFIX).strip()
        if detail_text:
            return detail_text
    if event is not None:
        parse_error = event.get("completion_parse_error")
        if isinstance(parse_error, str) and parse_error.strip():
            return parse_error.strip()
    return "completion JSON did not pass validation"
