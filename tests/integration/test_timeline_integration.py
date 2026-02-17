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


def _phase_group_labels(cycle: dict[str, object]) -> list[str]:
    """Return phase-group labels for a cycle payload."""
    groups = cycle.get("phase_groups")
    if not isinstance(groups, list):
        return []
    return [
        str(group.get("label"))
        for group in groups
        if isinstance(group, dict) and group.get("label")
    ]


def _step_events(cycle: dict[str, object]) -> list[str]:
    """Return step event names for a cycle payload."""
    steps = cycle.get("steps")
    if not isinstance(steps, list):
        return []
    return [
        str(step.get("event"))
        for step in steps
        if isinstance(step, dict) and step.get("event")
    ]


def _latest_run(payload: dict[str, object]) -> dict[str, object]:
    """Return the latest run from issue-detail payload."""
    runs = payload.get("runs")
    assert isinstance(runs, list) and runs, "expected at least one run"
    latest = runs[-1]
    assert isinstance(latest, dict), "run payload must be an object"
    return latest


def _first_cycle(run: dict[str, object]) -> dict[str, object]:
    """Return the first cycle from a run payload."""
    cycles = run.get("cycles")
    assert isinstance(cycles, list) and cycles, "expected at least one cycle"
    first = cycles[0]
    assert isinstance(first, dict), "cycle payload must be an object"
    return first


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
        assert issue_detail_payload["timeline_steps"], "Expected timeline steps from persisted DB events"
        assert issue_detail_payload.get("summary", {}).get("timeline_diagnostic") is None
        latest_run = _latest_run(issue_detail_payload)
        assert latest_run["session_run_ids"] == ["run-code-1", "run-review-1"]
    finally:
        web.set_orchestrator(None)


def test_timeline_store_does_not_migrate_legacy_jsonl_via_api(sample_config, mock_repository_host):
    """Legacy JSONL timeline should be ignored by forward-only SQLite store."""
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
        Issue(number=issue_number, title="Legacy Timeline Ignored", labels=["agent:web"]),
    ]

    web.set_orchestrator(orch)
    try:
        client = TestClient(web.app)
        response = client.get(f"/api/timeline/{issue_number}")
        assert response.status_code == 200
        payload = response.json()
        assert payload["events"] == []
    finally:
        web.set_orchestrator(None)


def test_issue_detail_4057_like_projection_stays_semantically_correct(sample_config, mock_repository_host):
    """4057-like local-loop flow should project coding->review->publish ordering."""
    orch, timeline_writer = _build_orchestrator_with_sqlite_timeline(sample_config, mock_repository_host)
    issue_number = 4057
    code_run_id = "run-4057-code-1"
    review_run_id = "run-4057-review-1"
    orch.state.cached_queue_issues = [
        Issue(
            number=issue_number,
            title="UI: Surface provider circuit breaker status",
            labels=["agent:backend", "pr-pending"],
        ),
    ]

    def record(
        event_name: EventName,
        *,
        run_id: str,
        run_dir: str,
        task: str,
        agent: str,
        summary: str | None = None,
    ) -> None:
        payload: dict[str, object] = {
            "issue_number": issue_number,
            "run_id": run_id,
            "run_dir": run_dir,
            "task": task,
            "agent": agent,
            "rework_cycle": 0,
        }
        if summary:
            payload["summary"] = summary
        timeline_writer.record(TraceEvent(event_name, payload))

    record(
        EventName.SESSION_STARTED,
        run_id=code_run_id,
        run_dir="/tmp/run-4057-code-1",
        task="code",
        agent="agent:backend",
    )
    record(
        EventName.SESSION_VALIDATION_PASSED,
        run_id=code_run_id,
        run_dir="/tmp/run-4057-code-1",
        task="code",
        agent="agent:backend",
    )
    record(
        EventName.SESSION_COMPLETED,
        run_id=code_run_id,
        run_dir="/tmp/run-4057-code-1",
        task="code",
        agent="agent:backend",
        summary="Implementation completed",
    )
    record(
        EventName.REVIEW_STARTED,
        run_id=review_run_id,
        run_dir="/tmp/run-4057-review-1",
        task="review",
        agent="agent:reviewer",
    )
    record(
        EventName.REVIEW_APPROVED,
        run_id=review_run_id,
        run_dir="/tmp/run-4057-review-1",
        task="review",
        agent="agent:reviewer",
    )
    record(
        EventName.ISSUE_PR_CREATED,
        run_id=code_run_id,
        run_dir="/tmp/run-4057-code-1",
        task="code",
        agent="agent:backend",
        summary="PR #4124 created",
    )
    record(
        EventName.REVIEW_COMMENT_ADDED,
        run_id=review_run_id,
        run_dir="/tmp/run-4057-review-1",
        task="review",
        agent="agent:reviewer",
        summary="Posted review comment",
    )

    web.set_orchestrator(orch)
    try:
        client = TestClient(web.app)
        response = client.get(f"/api/issue-detail/{issue_number}")
        assert response.status_code == 200
        payload = response.json()

        assert payload["run_count"] == 1
        run = _latest_run(payload)
        assert run["session_run_ids"] == [code_run_id, review_run_id]
        assert run["outcome"] == "Approved"
        assert len(run["cycles"]) == 1

        cycle = _first_cycle(run)
        assert cycle["outcome"] == "Approved"
        assert cycle["session_run_ids"] == [code_run_id, review_run_id]
        assert _phase_group_labels(cycle) == [
            "Coding",
            "Orchestrator",
            "Coding",
            "Review",
            "Orchestrator",
            "Review",
        ]
        assert _step_events(cycle) == [
            "session.started",
            "session.validation_passed",
            "session.completed",
            "review.started",
            "review.approved",
            "issue.pr_created",
            "review.comment_added",
        ]

        review_comment_step = next(
            step for step in cycle["steps"] if step.get("event") == "review.comment_added"
        )
        assert any(
            action.get("type") == "open_review_feedback"
            for action in review_comment_step.get("actions", [])
        )
    finally:
        web.set_orchestrator(None)


def test_issue_detail_latest_run_splits_after_pr_pending_removed_and_requeue(
    sample_config,
    mock_repository_host,
):
    """Approved cycle followed by explicit requeue must become a new logical run."""
    orch, timeline_writer = _build_orchestrator_with_sqlite_timeline(sample_config, mock_repository_host)
    issue_number = 4057
    code_run_id = "run-4057-code-1"
    review_run_id = "run-4057-review-1"
    requeue_run_id = "run-4057-requeue-1"
    orch.state.cached_queue_issues = [
        Issue(
            number=issue_number,
            title="UI: Surface provider circuit breaker status",
            labels=["agent:backend", "in-progress", "rework-cycle-1"],
        ),
    ]

    def record(
        event_name: EventName,
        *,
        run_id: str,
        run_dir: str,
        task: str,
        agent: str,
        removed: list[str] | None = None,
        summary: str | None = None,
        rework_cycle: int = 0,
    ) -> None:
        payload: dict[str, object] = {
            "issue_number": issue_number,
            "run_id": run_id,
            "run_dir": run_dir,
            "task": task,
            "agent": agent,
            "rework_cycle": rework_cycle,
        }
        if removed is not None:
            payload["removed"] = removed
        if summary:
            payload["summary"] = summary
        timeline_writer.record(TraceEvent(event_name, payload))

    # Run 1: coding + review approved
    record(
        EventName.SESSION_STARTED,
        run_id=code_run_id,
        run_dir="/tmp/run-4057-code-1",
        task="code",
        agent="agent:backend",
    )
    record(
        EventName.SESSION_COMPLETED,
        run_id=code_run_id,
        run_dir="/tmp/run-4057-code-1",
        task="code",
        agent="agent:backend",
        summary="Implementation completed",
    )
    record(
        EventName.REVIEW_STARTED,
        run_id=review_run_id,
        run_dir="/tmp/run-4057-review-1",
        task="review",
        agent="agent:reviewer",
    )
    record(
        EventName.REVIEW_APPROVED,
        run_id=review_run_id,
        run_dir="/tmp/run-4057-review-1",
        task="review",
        agent="agent:reviewer",
    )
    record(
        EventName.ISSUE_PR_CREATED,
        run_id=code_run_id,
        run_dir="/tmp/run-4057-code-1",
        task="code",
        agent="agent:backend",
        summary="PR #4124 created",
    )
    record(
        EventName.REVIEW_COMMENT_ADDED,
        run_id=review_run_id,
        run_dir="/tmp/run-4057-review-1",
        task="review",
        agent="agent:reviewer",
        summary="Posted review comment",
    )

    # Boundary signal: manual label mutation removed pr-pending.
    record(
        EventName.ISSUE_LABELS_CHANGED,
        run_id=code_run_id,
        run_dir="/tmp/run-4057-code-1",
        task="orchestrator",
        agent="agent:backend",
        removed=["pr-pending"],
    )

    # Run 2: fresh requeue/retry starts a new logical run.
    record(
        EventName.REWORK_STARTED,
        run_id=requeue_run_id,
        run_dir="/tmp/run-4057-requeue-1",
        task="rework",
        agent="agent:backend",
        rework_cycle=1,
    )

    web.set_orchestrator(orch)
    try:
        client = TestClient(web.app)
        response = client.get(f"/api/issue-detail/{issue_number}")
        assert response.status_code == 200
        payload = response.json()

        assert payload["run_count"] == 2
        first_run = payload["runs"][0]
        latest_run = _latest_run(payload)

        assert first_run["outcome"] == "Approved"
        assert len(first_run["cycles"]) == 1
        first_run_cycle = _first_cycle(first_run)
        assert first_run_cycle["outcome"] == "Approved"

        assert latest_run["outcome"] == "In progress"
        assert len(latest_run["cycles"]) == 1
        latest_cycle = _first_cycle(latest_run)
        assert latest_cycle["outcome"] == "In progress"
        assert _phase_group_labels(latest_cycle)[0] == "Rework"

        assert latest_cycle["lifecycle"] > first_run_cycle["lifecycle"]
    finally:
        web.set_orchestrator(None)


def test_issue_detail_rework_review_exchange_without_signal_stays_in_single_cycle(
    sample_config,
    mock_repository_host,
):
    """review_exchange.* events without rework_cycle must stay in the active rework cycle."""
    orch, timeline_writer = _build_orchestrator_with_sqlite_timeline(sample_config, mock_repository_host)
    issue_number = 4057
    orch.state.cached_queue_issues = [
        Issue(
            number=issue_number,
            title="UI: Surface provider circuit breaker status",
            labels=["agent:backend", "rework-cycle-1"],
        ),
    ]

    timeline_writer.record(TraceEvent(EventName.REWORK_STARTED, {
        "issue_number": issue_number,
        "run_id": "run-4057-rework-1",
        "run_dir": "/tmp/run-4057-rework-1",
        "task": "rework",
        "agent": "agent:backend",
        "rework_cycle": 1,
    }))
    timeline_writer.record(TraceEvent(EventName.REVIEW_STARTED, {
        "issue_number": issue_number,
        "run_id": "run-4057-review-1",
        "run_dir": "/tmp/run-4057-review-1",
        "task": "review",
        "agent": "agent:reviewer",
    }))
    timeline_writer.record(TraceEvent(EventName.REVIEW_EXCHANGE_STARTED, {
        "issue_number": issue_number,
        "run_id": "run-4057-review-1",
        "run_dir": "/tmp/run-4057-review-1",
        "task": "review",
        "agent": "agent:reviewer",
    }))
    timeline_writer.record(TraceEvent(EventName.REVIEW_EXCHANGE_ROUND_STARTED, {
        "issue_number": issue_number,
        "run_id": "run-4057-review-1",
        "run_dir": "/tmp/run-4057-review-1",
        "task": "review",
        "agent": "agent:reviewer",
    }))
    timeline_writer.record(TraceEvent(EventName.REVIEW_EXCHANGE_ROUND_COMPLETED, {
        "issue_number": issue_number,
        "run_id": "run-4057-review-1",
        "run_dir": "/tmp/run-4057-review-1",
        "task": "review",
        "agent": "agent:reviewer",
    }))
    timeline_writer.record(TraceEvent(EventName.SESSION_STARTED, {
        "issue_number": issue_number,
        "run_id": "run-4057-rework-1",
        "run_dir": "/tmp/run-4057-rework-1",
        "task": "code",
        "agent": "agent:backend",
    }))

    web.set_orchestrator(orch)
    try:
        client = TestClient(web.app)
        response = client.get(f"/api/issue-detail/{issue_number}")
        assert response.status_code == 200
        payload = response.json()
        latest_run = _latest_run(payload)
        assert len(latest_run["cycles"]) == 1
        cycle = _first_cycle(latest_run)
        assert cycle["iteration"] == 2
        labels = _phase_group_labels(cycle)
        assert labels[:2] == ["Rework", "Review"]
        assert "Coding" not in labels
        assert "review_exchange.started" in _step_events(cycle)
    finally:
        web.set_orchestrator(None)
