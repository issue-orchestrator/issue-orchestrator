"""Integration tests for DB-backed timeline end-to-end behavior."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient

from issue_orchestrator.entrypoints import web
from issue_orchestrator.events import EventName
from issue_orchestrator.execution.timeline_reader import DefaultTimelineReader
from issue_orchestrator.execution.timeline_store import SqliteTimelineStore, TimelineStoreConfig
from issue_orchestrator.execution.timeline_writer import DefaultTimelineWriter
from issue_orchestrator.execution.session_output_adapter import FileSystemSessionOutput
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


def _start_run_with_artifacts(
    repo_root: Path,
    *,
    issue_number: int,
    session_name: str,
) -> str:
    """Create a real run directory with required artifacts for strict timeline actions."""
    session_output = FileSystemSessionOutput()
    worktree = repo_root / f"wt-{issue_number}-{session_name}"
    worktree.mkdir(parents=True, exist_ok=True)
    run = session_output.start_run(worktree, session_name, issue_number=issue_number)
    (run.run_dir / "session.log").write_text("agent output\n", encoding="utf-8")
    claude_log = run.run_dir / "claude.jsonl"
    claude_log.write_text('{"type":"assistant","content":"ok"}\n', encoding="utf-8")
    completion_record = run.run_dir / "completion-agent_backend.json"
    completion_record.write_text('{"outcome":"completed"}\n', encoding="utf-8")
    session_output.update_manifest(run.run_dir, {"claude_log_path": str(claude_log)})
    return str(run.run_dir)


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
    timeline_db = sample_config.repo_root / ".issue-orchestrator" / "state" / "timeline.sqlite"
    run_dir_code = _start_run_with_artifacts(
        sample_config.repo_root, issue_number=issue_number, session_name="issue-4057-code"
    )
    run_dir_review = _start_run_with_artifacts(
        sample_config.repo_root, issue_number=issue_number, session_name="review-4057"
    )
    orch.state.cached_queue_issues = [
        Issue(number=issue_number, title="Timeline DB Integration", labels=["agent:web"]),
    ]

    timeline_writer.record(TraceEvent(EventName.SESSION_STARTED, {
        "issue_number": issue_number,
        "run_id": "run-code-1",
        "run_dir": run_dir_code,
        "task": "code",
        "agent": "agent:backend",
        "rework_cycle": 0,
    }))
    timeline_writer.record(TraceEvent(EventName.SESSION_COMPLETED, {
        "issue_number": issue_number,
        "run_id": "run-code-1",
        "run_dir": run_dir_code,
        "completion_path_absolute": str(Path(run_dir_code) / "completion-agent_backend.json"),
        "task": "code",
        "agent": "agent:backend",
        "rework_cycle": 0,
    }))
    timeline_writer.record(TraceEvent(EventName.REVIEW_STARTED, {
        "issue_number": issue_number,
        "run_id": "run-review-1",
        "run_dir": run_dir_review,
        "task": "review",
        "agent": "agent:reviewer",
        "rework_cycle": 0,
    }))
    timeline_writer.record(TraceEvent(EventName.REVIEW_APPROVED, {
        "issue_number": issue_number,
        "run_id": "run-review-1",
        "run_dir": run_dir_review,
        "task": "review",
        "agent": "agent:reviewer",
        "rework_cycle": 0,
    }))

    with sqlite3.connect(str(timeline_db)) as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM timeline_events WHERE issue_number = ?",
            (issue_number,),
        ).fetchone()
    assert row is not None
    assert int(row[0]) == 4

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
    run_dir_code = _start_run_with_artifacts(
        sample_config.repo_root, issue_number=issue_number, session_name="issue-4057-code-main"
    )
    run_dir_review = _start_run_with_artifacts(
        sample_config.repo_root, issue_number=issue_number, session_name="review-4057-main"
    )
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
        if event_name == EventName.SESSION_COMPLETED:
            payload["completion_path_absolute"] = str(Path(run_dir) / "completion-agent_backend.json")
        timeline_writer.record(TraceEvent(event_name, payload))

    record(
        EventName.SESSION_STARTED,
        run_id=code_run_id,
        run_dir=run_dir_code,
        task="code",
        agent="agent:backend",
    )
    record(
        EventName.SESSION_VALIDATION_PASSED,
        run_id=code_run_id,
        run_dir=run_dir_code,
        task="code",
        agent="agent:backend",
    )
    record(
        EventName.SESSION_COMPLETED,
        run_id=code_run_id,
        run_dir=run_dir_code,
        task="code",
        agent="agent:backend",
        summary="Implementation completed",
    )
    record(
        EventName.REVIEW_STARTED,
        run_id=review_run_id,
        run_dir=run_dir_review,
        task="review",
        agent="agent:reviewer",
    )
    record(
        EventName.REVIEW_APPROVED,
        run_id=review_run_id,
        run_dir=run_dir_review,
        task="review",
        agent="agent:reviewer",
    )
    record(
        EventName.ISSUE_PR_CREATED,
        run_id=code_run_id,
        run_dir=run_dir_code,
        task="code",
        agent="agent:backend",
        summary="PR #4124 created",
    )
    record(
        EventName.REVIEW_COMMENT_ADDED,
        run_id=review_run_id,
        run_dir=run_dir_review,
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
    run_dir_code = _start_run_with_artifacts(
        sample_config.repo_root, issue_number=issue_number, session_name="issue-4057-code-split"
    )
    run_dir_review = _start_run_with_artifacts(
        sample_config.repo_root, issue_number=issue_number, session_name="review-4057-split"
    )
    run_dir_requeue = _start_run_with_artifacts(
        sample_config.repo_root, issue_number=issue_number, session_name="rework-4057-split"
    )
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
        if event_name == EventName.SESSION_COMPLETED:
            payload["completion_path_absolute"] = str(Path(run_dir) / "completion-agent_backend.json")
        timeline_writer.record(TraceEvent(event_name, payload))

    # Run 1: coding + review approved
    record(
        EventName.SESSION_STARTED,
        run_id=code_run_id,
        run_dir=run_dir_code,
        task="code",
        agent="agent:backend",
    )
    record(
        EventName.SESSION_COMPLETED,
        run_id=code_run_id,
        run_dir=run_dir_code,
        task="code",
        agent="agent:backend",
        summary="Implementation completed",
    )
    record(
        EventName.REVIEW_STARTED,
        run_id=review_run_id,
        run_dir=run_dir_review,
        task="review",
        agent="agent:reviewer",
    )
    record(
        EventName.REVIEW_APPROVED,
        run_id=review_run_id,
        run_dir=run_dir_review,
        task="review",
        agent="agent:reviewer",
    )
    record(
        EventName.ISSUE_PR_CREATED,
        run_id=code_run_id,
        run_dir=run_dir_code,
        task="code",
        agent="agent:backend",
        summary="PR #4124 created",
    )
    record(
        EventName.REVIEW_COMMENT_ADDED,
        run_id=review_run_id,
        run_dir=run_dir_review,
        task="review",
        agent="agent:reviewer",
        summary="Posted review comment",
    )

    # Boundary signal: manual label mutation removed pr-pending.
    record(
        EventName.ISSUE_LABELS_CHANGED,
        run_id=code_run_id,
        run_dir=run_dir_code,
        task="orchestrator",
        agent="agent:backend",
        removed=["pr-pending"],
    )

    # Run 2: fresh requeue/retry starts a new logical run.
    record(
        EventName.REWORK_STARTED,
        run_id=requeue_run_id,
        run_dir=run_dir_requeue,
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
    run_dir_rework = _start_run_with_artifacts(
        sample_config.repo_root, issue_number=issue_number, session_name="rework-4057-exchange"
    )
    run_dir_review = _start_run_with_artifacts(
        sample_config.repo_root, issue_number=issue_number, session_name="review-4057-exchange"
    )

    timeline_writer.record(TraceEvent(EventName.REWORK_STARTED, {
        "issue_number": issue_number,
        "run_id": "run-4057-rework-1",
        "run_dir": run_dir_rework,
        "task": "rework",
        "agent": "agent:backend",
        "rework_cycle": 1,
    }))
    timeline_writer.record(TraceEvent(EventName.REVIEW_STARTED, {
        "issue_number": issue_number,
        "run_id": "run-4057-review-1",
        "run_dir": run_dir_review,
        "task": "review",
        "agent": "agent:reviewer",
    }))
    timeline_writer.record(TraceEvent(EventName.REVIEW_EXCHANGE_STARTED, {
        "issue_number": issue_number,
        "run_id": "run-4057-review-1",
        "run_dir": run_dir_review,
        "task": "review",
        "agent": "agent:reviewer",
    }))
    timeline_writer.record(TraceEvent(EventName.REVIEW_EXCHANGE_ROUND_STARTED, {
        "issue_number": issue_number,
        "run_id": "run-4057-review-1",
        "run_dir": run_dir_review,
        "task": "review",
        "agent": "agent:reviewer",
    }))
    timeline_writer.record(TraceEvent(EventName.REVIEW_EXCHANGE_ROUND_COMPLETED, {
        "issue_number": issue_number,
        "run_id": "run-4057-review-1",
        "run_dir": run_dir_review,
        "task": "review",
        "agent": "agent:reviewer",
    }))
    timeline_writer.record(TraceEvent(EventName.SESSION_STARTED, {
        "issue_number": issue_number,
        "run_id": "run-4057-rework-1",
        "run_dir": run_dir_rework,
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


def test_run_scoped_artifact_usability_enforces_non_empty_log_and_run_dir(
    sample_config,
    mock_repository_host,
):
    """Timeline actions should only offer run-scoped log actions for usable logs."""
    orch, timeline_writer = _build_orchestrator_with_sqlite_timeline(sample_config, mock_repository_host)
    issue_number = 4061
    run_dir = _start_run_with_artifacts(
        sample_config.repo_root, issue_number=issue_number, session_name="issue-4061-code"
    )
    orch.state.cached_queue_issues = [
        Issue(number=issue_number, title="Run-scoped artifact usability", labels=["agent:backend"]),
    ]

    timeline_writer.record(TraceEvent(EventName.SESSION_STARTED, {
        "issue_number": issue_number,
        "run_id": "run-4061-code-1",
        "run_dir": run_dir,
        "task": "code",
        "agent": "agent:backend",
        "rework_cycle": 0,
    }))

    web.set_orchestrator(orch)
    try:
        client = TestClient(web.app)
        log_response = client.get(f"/api/log/local/{issue_number}?run_dir={run_dir}")
        assert log_response.status_code == 200
        log_payload = log_response.json()
        assert any(len(str(line).strip()) >= 8 for line in log_payload.get("lines", []))

        detail_response = client.get(f"/api/issue-detail/{issue_number}")
        assert detail_response.status_code == 200
        detail_payload = detail_response.json()
        latest_run = _latest_run(detail_payload)
        cycle = _first_cycle(latest_run)
        steps = cycle.get("steps")
        assert isinstance(steps, list) and steps
        actions = steps[0].get("actions")
        assert isinstance(actions, list) and actions

        run_scoped = [
            action for action in actions
            if action.get("type") in {"open_agent_log", "view_claude_log", "open_orchestrator_log", "open_session_diagnostics"}
        ]
        assert run_scoped, "Expected run-scoped actions for usable run artifacts"
        assert all(action.get("run_dir") == run_dir for action in run_scoped)
    finally:
        web.set_orchestrator(None)


def test_run_scoped_log_action_not_offered_for_empty_session_log(
    sample_config,
    mock_repository_host,
):
    """Empty/near-empty session logs should not advertise run-scoped session-log actions."""
    orch, timeline_writer = _build_orchestrator_with_sqlite_timeline(sample_config, mock_repository_host)
    issue_number = 4062
    run_dir = _start_run_with_artifacts(
        sample_config.repo_root, issue_number=issue_number, session_name="issue-4062-code"
    )
    Path(run_dir, "session.log").write_text("", encoding="utf-8")
    orch.state.cached_queue_issues = [
        Issue(number=issue_number, title="Empty log action guardrail", labels=["agent:backend"]),
    ]

    timeline_writer.record(TraceEvent(EventName.SESSION_STARTED, {
        "issue_number": issue_number,
        "run_id": "run-4062-code-1",
        "run_dir": run_dir,
        "task": "code",
        "agent": "agent:backend",
        "rework_cycle": 0,
    }))

    web.set_orchestrator(orch)
    try:
        client = TestClient(web.app)
        detail_response = client.get(f"/api/issue-detail/{issue_number}")
        assert detail_response.status_code == 200
        detail_payload = detail_response.json()
        latest_run = _latest_run(detail_payload)
        cycle = _first_cycle(latest_run)
        steps = cycle.get("steps")
        assert isinstance(steps, list) and steps
        actions = steps[0].get("actions") or []
        assert isinstance(actions, list)
        assert all(action.get("type") != "open_agent_log" for action in actions)
    finally:
        web.set_orchestrator(None)


def test_session_diagnostics_dialog_integration_exposes_existing_paths_and_run_scope(
    sample_config,
    mock_repository_host,
):
    """Diagnostics dialog actions should carry valid filesystem paths and run context."""
    orch, _timeline_writer = _build_orchestrator_with_sqlite_timeline(sample_config, mock_repository_host)
    issue_number = 4063
    run_dir = _start_run_with_artifacts(
        sample_config.repo_root, issue_number=issue_number, session_name="issue-4063-code"
    )
    run_dir_path = Path(run_dir)
    worktree = run_dir_path.parents[2]
    validation_rel = ".issue-orchestrator/validation/run-4063.json"
    diagnostic_rel = ".issue-orchestrator/diagnostics/run-4063.json"
    validation_abs = worktree / validation_rel
    diagnostic_abs = worktree / diagnostic_rel
    validation_abs.parent.mkdir(parents=True, exist_ok=True)
    diagnostic_abs.parent.mkdir(parents=True, exist_ok=True)
    validation_abs.write_text('{"ok": true}\n', encoding="utf-8")
    diagnostic_abs.write_text('{"diagnostic": "ok"}\n', encoding="utf-8")

    session_output = FileSystemSessionOutput()
    session_output.update_manifest(
        run_dir_path,
        {
            "validation_record_path": validation_rel,
            "diagnostic_path": diagnostic_rel,
        },
    )

    web.set_orchestrator(orch)
    try:
        client = TestClient(web.app)
        response = client.get(f"/api/dialog/session-diagnostics/{issue_number}?run_dir={run_dir}")
        assert response.status_code == 200
        payload = response.json()
        actions = payload.get("actions")
        assert isinstance(actions, list) and actions

        path_actions = [
            action for action in actions
            if action.get("type") == "open_path" and action.get("label") in {"Open Session Dir", "Open Validation", "Open Diagnostic"}
        ]
        assert path_actions, "Expected diagnostics path actions"
        for action in path_actions:
            path_value = action.get("path")
            assert isinstance(path_value, str) and path_value
            assert Path(path_value).exists(), f"Expected action path to exist: {path_value}"

        run_scoped_actions = [
            action for action in actions
            if action.get("type") in {"open_agent_log", "open_orchestrator_log", "view_claude_log"}
        ]
        assert run_scoped_actions
        assert all(action.get("run_dir") == run_dir for action in run_scoped_actions)
    finally:
        web.set_orchestrator(None)


def test_latest_run_without_review_events_is_not_projected_as_approved_or_completed(
    sample_config,
    mock_repository_host,
):
    """Latest run lacking review events must not appear approved/completed."""
    orch, timeline_writer = _build_orchestrator_with_sqlite_timeline(sample_config, mock_repository_host)
    issue_number = 4064
    run_dir_code = _start_run_with_artifacts(
        sample_config.repo_root, issue_number=issue_number, session_name="issue-4064-code"
    )
    run_dir_review = _start_run_with_artifacts(
        sample_config.repo_root, issue_number=issue_number, session_name="review-4064"
    )
    run_dir_rework = _start_run_with_artifacts(
        sample_config.repo_root, issue_number=issue_number, session_name="rework-4064"
    )
    orch.state.cached_queue_issues = [
        Issue(number=issue_number, title="Latest run review invariant", labels=["agent:backend"]),
    ]

    # Earlier run with review approval.
    timeline_writer.record(TraceEvent(EventName.SESSION_STARTED, {
        "issue_number": issue_number,
        "run_id": "run-4064-code-1",
        "run_dir": run_dir_code,
        "task": "code",
        "agent": "agent:backend",
        "rework_cycle": 0,
    }))
    timeline_writer.record(TraceEvent(EventName.SESSION_COMPLETED, {
        "issue_number": issue_number,
        "run_id": "run-4064-code-1",
        "run_dir": run_dir_code,
        "task": "code",
        "agent": "agent:backend",
        "completion_path_absolute": str(Path(run_dir_code) / "completion-agent_backend.json"),
        "rework_cycle": 0,
    }))
    timeline_writer.record(TraceEvent(EventName.REVIEW_STARTED, {
        "issue_number": issue_number,
        "run_id": "run-4064-review-1",
        "run_dir": run_dir_review,
        "task": "review",
        "agent": "agent:reviewer",
        "rework_cycle": 0,
    }))
    timeline_writer.record(TraceEvent(EventName.REVIEW_APPROVED, {
        "issue_number": issue_number,
        "run_id": "run-4064-review-1",
        "run_dir": run_dir_review,
        "task": "review",
        "agent": "agent:reviewer",
        "rework_cycle": 0,
    }))

    # Boundary + latest run without review events.
    timeline_writer.record(TraceEvent(EventName.ISSUE_LABELS_CHANGED, {
        "issue_number": issue_number,
        "run_id": "run-4064-code-1",
        "run_dir": run_dir_code,
        "task": "orchestrator",
        "agent": "agent:backend",
        "removed": ["pr-pending"],
        "rework_cycle": 0,
    }))
    timeline_writer.record(TraceEvent(EventName.REWORK_STARTED, {
        "issue_number": issue_number,
        "run_id": "run-4064-rework-1",
        "run_dir": run_dir_rework,
        "task": "rework",
        "agent": "agent:backend",
        "rework_cycle": 1,
    }))
    timeline_writer.record(TraceEvent(EventName.SESSION_STARTED, {
        "issue_number": issue_number,
        "run_id": "run-4064-rework-1",
        "run_dir": run_dir_rework,
        "task": "code",
        "agent": "agent:backend",
        "rework_cycle": 1,
    }))
    timeline_writer.record(TraceEvent(EventName.SESSION_COMPLETED, {
        "issue_number": issue_number,
        "run_id": "run-4064-rework-1",
        "run_dir": run_dir_rework,
        "task": "code",
        "agent": "agent:backend",
        "completion_path_absolute": str(Path(run_dir_rework) / "completion-agent_backend.json"),
        "rework_cycle": 1,
    }))

    web.set_orchestrator(orch)
    try:
        client = TestClient(web.app)
        response = client.get(f"/api/issue-detail/{issue_number}")
        assert response.status_code == 200
        payload = response.json()
        assert int(payload.get("run_count") or 0) >= 2
        latest_run = _latest_run(payload)
        latest_outcome = str(latest_run.get("outcome") or "").lower()
        assert "approved" not in latest_outcome
        assert "completed" not in latest_outcome

        latest_cycle = _first_cycle(latest_run)
        latest_events = _step_events(latest_cycle)
        assert not any(evt.startswith("review.") for evt in latest_events)
    finally:
        web.set_orchestrator(None)
