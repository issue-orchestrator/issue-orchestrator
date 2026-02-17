"""Integration tests for DB-backed timeline end-to-end behavior."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from issue_orchestrator.entrypoints import web
from issue_orchestrator.events import EventName
from issue_orchestrator.execution.timeline_reader import DefaultTimelineReader
from issue_orchestrator.execution.timeline_store import SqliteTimelineStore, TimelineStoreConfig
from issue_orchestrator.execution.timeline_writer import DefaultTimelineWriter
from issue_orchestrator.ports.event_sink import TraceEvent
from issue_orchestrator.domain.models import Issue

from tests.conftest import MockEventSink, MockSessionRunner, build_test_orchestrator_deps


def _build_orchestrator_with_sqlite_timeline(sample_config, mock_repository_host):
    """Build a real orchestrator object with SQLite timeline services wired in."""
    from issue_orchestrator.infra.orchestrator import Orchestrator
    from issue_orchestrator.execution.worktree_adapter import GitWorktreeManager
    from issue_orchestrator.execution.git_working_copy import GitWorkingCopy

    timeline_store = SqliteTimelineStore(
        sample_config.repo_root / ".issue-orchestrator" / "state" / "timeline.sqlite",
        config=TimelineStoreConfig(max_records=5000, max_total_records=20000),
    )
    timeline_reader = DefaultTimelineReader(timeline_store)
    timeline_writer = DefaultTimelineWriter(timeline_store)

    runner = MockSessionRunner()
    runner.plugin.session_exists_override = False

    deps = build_test_orchestrator_deps(
        sample_config,
        mock_repository_host,
        MockEventSink(),
        runner,
        GitWorktreeManager(),
        working_copy=GitWorkingCopy(),
        timeline_reader=timeline_reader,
        timeline_writer=timeline_writer,
    )
    return Orchestrator(config=sample_config, deps=deps), timeline_writer


def test_timeline_and_issue_detail_read_from_sqlite_store(sample_config, mock_repository_host):
    """`/api/timeline` and `/api/issue-detail` should project DB timeline data."""
    orch, timeline_writer = _build_orchestrator_with_sqlite_timeline(sample_config, mock_repository_host)
    issue_number = 4057
    orch.state.cached_queue_issues = [
        Issue(number=issue_number, title="Timeline DB Integration", labels=["agent:web"]),
    ]

    timeline_writer.record(TraceEvent(EventName.SESSION_STARTED, {
        "issue_number": issue_number,
        "run_id": "run-code-1",
        "run_dir": "/tmp/run-code-1",
        "task": "code",
        "agent": "agent:backend",
        "rework_cycle": 0,
    }))
    timeline_writer.record(TraceEvent(EventName.SESSION_COMPLETED, {
        "issue_number": issue_number,
        "run_id": "run-code-1",
        "run_dir": "/tmp/run-code-1",
        "task": "code",
        "agent": "agent:backend",
        "rework_cycle": 0,
    }))
    timeline_writer.record(TraceEvent(EventName.REVIEW_STARTED, {
        "issue_number": issue_number,
        "run_id": "run-review-1",
        "run_dir": "/tmp/run-review-1",
        "task": "review",
        "agent": "agent:reviewer",
        "rework_cycle": 0,
    }))
    timeline_writer.record(TraceEvent(EventName.REVIEW_APPROVED, {
        "issue_number": issue_number,
        "run_id": "run-review-1",
        "run_dir": "/tmp/run-review-1",
        "task": "review",
        "agent": "agent:reviewer",
        "rework_cycle": 0,
    }))

    web.set_orchestrator(orch)
    try:
        client = TestClient(web.app)
        timeline_response = client.get(f"/api/timeline/{issue_number}")
        assert timeline_response.status_code == 200
        timeline_payload = timeline_response.json()
        assert len(timeline_payload["events"]) == 4
        assert timeline_payload["events"][0]["event"] == "session.started"
        assert timeline_payload["events"][-1]["event"] == "review.approved"

        issue_detail_response = client.get(f"/api/issue-detail/{issue_number}")
        assert issue_detail_response.status_code == 200
        issue_detail_payload = issue_detail_response.json()
        assert issue_detail_payload["run_count"] == 1
        latest_run = issue_detail_payload["runs"][-1]
        assert latest_run["session_run_ids"] == ["run-code-1", "run-review-1"]
    finally:
        web.set_orchestrator(None)


def test_timeline_store_migrates_legacy_jsonl_and_serves_via_api(sample_config, mock_repository_host):
    """Legacy JSONL timeline should migrate into SQLite and remain API-visible."""
    issue_number = 4058
    state_dir = sample_config.repo_root / ".issue-orchestrator" / "state"
    legacy_dir = state_dir / "timeline"
    legacy_dir.mkdir(parents=True, exist_ok=True)
    legacy_file = legacy_dir / f"issue-{issue_number}.jsonl"
    legacy_records = [
        {
            "event_id": "legacy-1",
            "timestamp": "2026-02-17T10:00:00Z",
            "event": "session.started",
            "data": {
                "issue_number": issue_number,
                "run_id": "legacy-run-1",
                "run_dir": "/tmp/legacy-run-1",
                "rework_cycle": 0,
            },
        },
        {
            "event_id": "legacy-2",
            "timestamp": "2026-02-17T10:10:00Z",
            "event": "session.completed",
            "data": {
                "issue_number": issue_number,
                "run_id": "legacy-run-1",
                "run_dir": "/tmp/legacy-run-1",
                "rework_cycle": 0,
            },
        },
    ]
    legacy_file.write_text(
        "\n".join(json.dumps(record, sort_keys=True) for record in legacy_records) + "\n",
        encoding="utf-8",
    )

    orch, _timeline_writer = _build_orchestrator_with_sqlite_timeline(sample_config, mock_repository_host)
    orch.state.cached_queue_issues = [
        Issue(number=issue_number, title="Legacy Timeline Migration", labels=["agent:web"]),
    ]

    web.set_orchestrator(orch)
    try:
        client = TestClient(web.app)
        response = client.get(f"/api/timeline/{issue_number}")
        assert response.status_code == 200
        payload = response.json()
        assert [event["event"] for event in payload["events"]] == [
            "session.started",
            "session.completed",
        ]
    finally:
        web.set_orchestrator(None)
