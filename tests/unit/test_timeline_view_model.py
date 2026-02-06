"""Unit tests for timeline view model."""

from issue_orchestrator.ports.timeline_store import TimelineRecord
from issue_orchestrator.view_models.timeline import TimelineStream, build_issue_timeline


def test_build_issue_timeline_maps_phase_and_step():
    records = [
        TimelineRecord(
            event_id="e1",
            timestamp="2026-02-06T00:00:00Z",
            event="session.started",
            data={"issue_number": 123, "session_id": "issue-123"},
        ),
        TimelineRecord(
            event_id="e2",
            timestamp="2026-02-06T00:01:00Z",
            event="issue.pr_created",
            data={"issue_number": 123, "pr_url": "https://example/pr/1"},
        ),
    ]

    timeline = build_issue_timeline(123, records)
    events = timeline["events"]

    assert events[0]["phase"] == "in_progress"
    assert events[0]["step"] == "started"
    assert events[1]["phase"] == "pr_pending"
    assert events[1]["step"] == "pr_created"
    assert events[1]["artifacts"][0]["type"] == "pull_request"

    stream = TimelineStream.from_records(123, records)
    grouped = stream.group_by_phase()
    assert grouped["in_progress"][0].step == "started"
