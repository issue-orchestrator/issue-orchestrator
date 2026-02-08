"""Issue detail view model builder."""

from __future__ import annotations

from typing import Any


def build_issue_detail_view_model(
    issue_number: int,
    title: str,
    issue_url: str,
    events: list[dict[str, Any]],
    phase_toc: list[dict[str, Any]],
    loops: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build issue detail payload used by the dashboard drawer."""
    return {
        "issue_number": issue_number,
        "title": title,
        "issue_url": issue_url,
        "phase_toc": phase_toc,
        "loops": loops,
        "events": events,
        "summary": _summary(events),
        "actions": [
            {"id": "focus", "label": "Focus"},
            {"id": "github", "label": "GitHub ↗", "url": issue_url},
        ],
    }


def _summary(events: list[dict[str, Any]]) -> dict[str, Any]:
    if not events:
        return {"status": "unknown", "last_event": "", "event_count": 0}
    last = events[-1]
    return {
        "status": str(last.get("status") or "unknown"),
        "last_event": str(last.get("event") or ""),
        "event_count": len(events),
    }
