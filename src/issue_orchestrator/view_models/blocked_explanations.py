"""Drawer-facing blocked explanation helpers."""

from __future__ import annotations


def invalid_or_validation_blocked_explanation(
    *,
    event_name: str,
    event_summary: str,
    labels: tuple[str, ...],
) -> str | None:
    if event_name == "session.invalid_completion_record":
        reason = (
            event_summary.removeprefix("Completion record rejected:").strip()
            if event_summary
            else "completion JSON did not pass validation"
        )
        return f"Completion record rejected \u2014 {reason}"
    if event_name == "session.validation_failed" or "validation-failed" in labels:
        reason = event_summary or "project tests did not pass"
        return f"Validation failed \u2014 {reason}"
    return None
