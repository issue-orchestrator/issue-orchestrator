"""Formatting and projection for historical issue-detail cycle cards."""

from datetime import datetime
from collections.abc import Mapping, Sequence
from typing import Any, TypedDict


class PreviousCycleSummary(TypedDict):
    cycle: Any
    duration_label: str
    outcome: str
    pr_url: str | None
    summary: str


def format_time_label(timestamp: Any, today: str = "") -> str:
    """Format a timestamp for compact issue-detail display."""
    if not timestamp:
        return ""
    value = str(timestamp)
    try:
        parsed = datetime.fromisoformat(value)
        time_part = parsed.strftime("%-I:%M:%S %p").lstrip("0")
        if today and value[:10] == today:
            return time_part
        return f"{parsed.strftime('%b %-d')}, {time_part}"
    except (ValueError, TypeError):
        if "T" in value and len(value) >= 19:
            return value[11:19]
        return value


def build_previous_cycles(
    cycles: Sequence[Mapping[str, Any]],
    today: str,
) -> list[PreviousCycleSummary]:
    """Build summary cards for cycles that completed before today."""
    previous: list[PreviousCycleSummary] = []
    for cycle_data in cycles:
        start = str(cycle_data.get("start") or "")
        if start[:10] >= today:
            continue
        cycle_events = cycle_data.get("events") or []
        previous.append(
            {
                "cycle": cycle_data.get("cycle", 0),
                "duration_label": _duration_label(
                    cycle_data.get("start"), cycle_data.get("end")
                ),
                "outcome": str(cycle_data.get("status") or "unknown"),
                "pr_url": _extract_pr_url(cycle_events),
                "summary": _last_summary(cycle_events),
            }
        )
    return previous


def _duration_label(start: Any, end: Any) -> str:
    if not start or not end:
        return ""
    try:
        delta = datetime.fromisoformat(str(end)) - datetime.fromisoformat(str(start))
    except (ValueError, TypeError):
        return ""
    minutes = int(delta.total_seconds() / 60)
    if minutes < 1:
        return "<1 min"
    if minutes < 60:
        return f"{minutes} min"
    hours, remaining = divmod(minutes, 60)
    return f"{hours}h" if remaining == 0 else f"{hours}h {remaining}m"


def _last_summary(events: Sequence[Mapping[str, Any]]) -> str:
    for event in reversed(events):
        summary = event.get("summary")
        if summary:
            return str(summary)
    return ""


def _extract_pr_url(events: Sequence[Mapping[str, Any]]) -> str | None:
    for event in reversed(events):
        for artifact in event.get("artifacts") or []:
            if isinstance(artifact, dict):
                value = str(artifact.get("value") or "")
                if "/pull/" in value:
                    return value
    return None


__all__ = ["build_previous_cycles", "format_time_label"]
