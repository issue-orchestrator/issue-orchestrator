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
        TimelineRecord(
            event_id="e3",
            timestamp="2026-02-06T00:02:00Z",
            event="session.completed",
            data={
                "issue_number": 123,
                "completion_path_absolute": "/tmp/worktree/.issue-orchestrator/completion.json",
            },
        ),
    ]

    timeline = build_issue_timeline(123, records)
    events = timeline["events"]

    assert events[0]["phase"] == "in_progress"
    assert events[0]["step"] == "started"
    assert events[1]["phase"] == "pr_pending"
    assert events[1]["step"] == "pr_created"
    assert events[1]["artifacts"][0]["type"] == "pull_request"
    assert events[2]["artifacts"][0]["type"] == "completion_record"

    stream = TimelineStream.from_records(123, records)
    grouped = stream.group_by_phase()
    assert grouped["in_progress"][0].step == "started"


def test_build_issue_timeline_status_mapping():
    records = [
        TimelineRecord(
            event_id="e1",
            timestamp="2026-02-06T00:00:00Z",
            event="review.queued",
            data={"issue_number": 123},
        ),
        TimelineRecord(
            event_id="e2",
            timestamp="2026-02-06T00:01:00Z",
            event="review.escalated",
            data={"issue_number": 123},
        ),
        TimelineRecord(
            event_id="e3",
            timestamp="2026-02-06T00:02:00Z",
            event="issue.pr_rejected",
            data={"issue_number": 123},
        ),
        TimelineRecord(
            event_id="e4",
            timestamp="2026-02-06T00:03:00Z",
            event="session.validation_failed",
            data={"issue_number": 123},
        ),
        TimelineRecord(
            event_id="e5",
            timestamp="2026-02-06T00:04:00Z",
            event="review.skipped",
            data={"issue_number": 123},
        ),
        TimelineRecord(
            event_id="e6",
            timestamp="2026-02-06T00:05:00Z",
            event="rework.escalating",
            data={"issue_number": 123},
        ),
    ]

    timeline = build_issue_timeline(123, records)
    statuses = [event["status"] for event in timeline["events"]]

    assert statuses == ["started", "failed", "failed", "failed", "completed", "started"]
