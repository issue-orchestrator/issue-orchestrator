"""Timeline presentation shaping tests."""

from __future__ import annotations

from issue_orchestrator.entrypoints.timeline_presentation import _decorate_timeline_events


def test_decorated_timeline_events_carry_typed_timestamp_detail_value_kinds() -> None:
    event = {
        "event": "e2e.test_completed",
        "timeline_schema_version": 4,
        "timestamp": "2026-05-12T10:00:00Z",
        "finished_at": "2026-05-12T10:05:00Z",
        "started_at": "2026-05-12T10:00:00",
        "summary": "test finished",
    }

    decorated = _decorate_timeline_events([event], issue_number=4057)

    assert decorated[0]["detail_value_kinds"] == {
        "timestamp": "timestamp",
        "finished_at": "timestamp",
    }
    assert "started_at" not in decorated[0]["detail_value_kinds"]
