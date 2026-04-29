"""Generated E2E run facsimiles for endpoint and integration tests.

The fixtures in ``tests/fixtures/e2e_runs`` intentionally preserve raw captured
SQLite data. This module covers the other side of the tradeoff: build realistic
run layouts through current persistence APIs so integration tests can exercise
4057-style flows without committing another full database snapshot.
"""

from __future__ import annotations

import base64
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from xml.sax.saxutils import escape

from issue_orchestrator.contracts.run_manifest import validate_run_manifest_payload
from issue_orchestrator.domain.timeline_key import TimelineKey
from issue_orchestrator.events import EventName
from issue_orchestrator.execution.timeline_store import SqliteTimelineStore
from issue_orchestrator.execution.timeline_writer import DefaultTimelineWriter
from issue_orchestrator.infra.e2e_db import E2EDB
from issue_orchestrator.infra.e2e_reports import E2ERunArtifactRecord
from issue_orchestrator.ports.event_sink import TraceEvent
from issue_orchestrator.ports.timeline_store import TimelineRecord

ISSUE_4057_NODEID = (
    "tests/e2e/test_issue_4057_production_flow.py"
    "::test_4057_production_real_agents_publish_gate_and_diagnostics"
)


@dataclass(frozen=True)
class FacsimileIssueRecipe:
    issue_number: int
    title: str
    branch_name: str
    pr_number: int


@dataclass(frozen=True)
class FacsimileTestRecipe:
    nodeid: str
    outcome: str = "passed"
    duration_seconds: float = 4.2
    issue_numbers: tuple[int, ...] = ()

    @property
    def display_name(self) -> str:
        return self.nodeid.rsplit("::", 1)[-1]

    @property
    def suite_name(self) -> str:
        return self.nodeid.rsplit("::", 1)[0]


@dataclass(frozen=True)
class E2EFacsimileRecipe:
    orchestrator_id: str
    branch: str
    commit_sha: str
    tests: tuple[FacsimileTestRecipe, ...]
    issues: tuple[FacsimileIssueRecipe, ...]

    def issue_by_number(self, issue_number: int) -> FacsimileIssueRecipe:
        for issue in self.issues:
            if issue.issue_number == issue_number:
                return issue
        raise KeyError(f"facsimile recipe has no issue {issue_number}")


@dataclass(frozen=True)
class MaterializedE2EFacsimile:
    repo_root: Path
    worktree_root: Path
    e2e_db_path: Path
    base_timeline_path: Path
    worktree_timeline_path: Path
    run_id: int
    recipe: E2EFacsimileRecipe


class _TimelineClock:
    def __init__(self, start: datetime) -> None:
        self._current = start

    def tick(self, seconds: int = 1) -> datetime:
        self._current = self._current + timedelta(seconds=seconds)
        return self._current

    def iso(self, seconds: int = 1) -> str:
        return self.tick(seconds).isoformat()


class _EventIds:
    def __init__(self) -> None:
        self._next = 0

    def next_record_id(self, prefix: str) -> str:
        self._next += 1
        return f"{prefix}-{self._next:04d}"

    def next_trace_id(self) -> int:
        self._next += 1
        return self._next


def default_4057_facsimile_recipe() -> E2EFacsimileRecipe:
    """Return a compact but realistic 4057-style E2E run recipe."""
    return E2EFacsimileRecipe(
        orchestrator_id="facsimile-orchestrator",
        branch="facsimile/4057-local-review-loop",
        commit_sha="facsimile4057",
        tests=(
            FacsimileTestRecipe(
                nodeid=ISSUE_4057_NODEID,
                issue_numbers=(4057, 4058),
                duration_seconds=9.8,
            ),
            FacsimileTestRecipe(
                nodeid="tests/e2e/test_dashboard_smoke.py::test_control_center_loads",
                duration_seconds=1.1,
            ),
        ),
        issues=(
            FacsimileIssueRecipe(
                issue_number=4057,
                title="UI: Surface provider circuit breaker status",
                branch_name="4057-ui-surface-provider-circuit-breaker-status-test",
                pr_number=7057,
            ),
            FacsimileIssueRecipe(
                issue_number=4058,
                title="UI: Add retry diagnostics to provider status",
                branch_name="4058-ui-surface-provider-diagnostics-test",
                pr_number=7058,
            ),
        ),
    )


def materialize_e2e_4057_facsimile(
    tmp_path: Path,
    *,
    recipe: E2EFacsimileRecipe | None = None,
) -> MaterializedE2EFacsimile:
    """Create a fully materialized generated E2E run under ``tmp_path``."""
    recipe = recipe or default_4057_facsimile_recipe()
    repo_root = tmp_path / "repo"
    worktree_root = tmp_path / "repo-e2e-worktree"
    repo_state = repo_root / ".issue-orchestrator" / "state"
    worktree_state = worktree_root / ".issue-orchestrator" / "state"
    repo_state.mkdir(parents=True)
    worktree_state.mkdir(parents=True)

    e2e_db_path = repo_root / ".issue-orchestrator" / "e2e.db"
    base_timeline_path = repo_state / "timeline.sqlite"
    worktree_timeline_path = worktree_state / "timeline.sqlite"
    db = E2EDB(e2e_db_path)
    base_store = SqliteTimelineStore(base_timeline_path)
    worktree_store = SqliteTimelineStore(worktree_timeline_path)
    worktree_writer = DefaultTimelineWriter(worktree_store)
    event_ids = _EventIds()

    command = ["pytest", *sorted({test.suite_name for test in recipe.tests})]
    run_id = db.start_run(
        repo_root=str(repo_root),
        orchestrator_id=recipe.orchestrator_id,
        pytest_args=command[1:],
        commit_sha=recipe.commit_sha,
        branch=recipe.branch,
        command=command,
        runner_kind="pytest",
    )

    clock = _TimelineClock(datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc))
    run_started_at = clock.iso()
    run_artifacts_dir = repo_root / ".issue-orchestrator" / "e2e-artifacts" / f"run-{run_id}"
    run_artifacts_dir.mkdir(parents=True)
    run_log = run_artifacts_dir / "e2e-output.log"
    run_log.write_text("facsimile 4057 e2e run completed\n", encoding="utf-8")
    junit_xml = run_artifacts_dir / "junit.xml"
    junit_xml.write_text(_junit_xml_for(recipe.tests), encoding="utf-8")

    db.update_progress(run_id, total_tests=len(recipe.tests))
    db.replace_run_artifacts(
        run_id,
        [
            E2ERunArtifactRecord(
                kind="junit_xml",
                label="JUnit XML",
                path=str(junit_xml),
            )
        ],
    )

    _append_e2e_event(
        base_store,
        run_id=run_id,
        event_ids=event_ids,
        event_name="e2e.run_started",
        timestamp=clock.iso(),
        data={
            "pytest_args": command[1:],
            "command": command,
            "runner_kind": "pytest",
            "commit_sha": recipe.commit_sha,
            "branch": recipe.branch,
            "quarantined_count": 0,
            "is_resume": False,
        },
    )
    _append_e2e_event(
        base_store,
        run_id=run_id,
        event_ids=event_ids,
        event_name="e2e.tests_collected",
        timestamp=clock.iso(),
        data={
            "total": len(recipe.tests),
            "nodeids": [test.nodeid for test in recipe.tests],
        },
    )

    for test in recipe.tests:
        db.update_progress(run_id, current_test=test.nodeid)
        _append_e2e_event(
            base_store,
            run_id=run_id,
            event_ids=event_ids,
            event_name="e2e.test_started",
            timestamp=clock.iso(),
            data={"nodeid": test.nodeid},
        )
        for issue_number in test.issue_numbers:
            issue = recipe.issue_by_number(issue_number)
            _emit_issue_lifecycle(
                writer=worktree_writer,
                worktree_root=worktree_root,
                event_ids=event_ids,
                clock=clock,
                issue=issue,
                e2e_run_id=run_id,
            )
        db.upsert_test_result(
            run_id,
            nodeid=test.nodeid,
            outcome=test.outcome,
            duration_seconds=test.duration_seconds,
            display_name=test.display_name,
            suite_name=test.suite_name,
            result_source="runtime",
        )
        _append_e2e_event(
            base_store,
            run_id=run_id,
            event_ids=event_ids,
            event_name="e2e.test_completed",
            timestamp=clock.iso(),
            data={
                "nodeid": test.nodeid,
                "outcome": test.outcome,
                "duration_seconds": test.duration_seconds,
                "is_quarantined": False,
            },
        )

    _append_e2e_event(
        base_store,
        run_id=run_id,
        event_ids=event_ids,
        event_name="e2e.run_finished",
        timestamp=clock.iso(),
        data={
            "status": "passed",
            "exit_code": 0,
            "duration_seconds": 16.4,
        },
    )
    run_finished_at = clock.iso()
    db.finish_run(
        run_id,
        status="passed",
        exit_code=0,
        duration_seconds=16.4,
        log_path=str(run_log),
        artifacts_dir=str(run_artifacts_dir),
    )
    _rewrite_run_window(
        e2e_db_path,
        run_id=run_id,
        started_at=run_started_at,
        finished_at=run_finished_at,
    )

    return MaterializedE2EFacsimile(
        repo_root=repo_root,
        worktree_root=worktree_root,
        e2e_db_path=e2e_db_path,
        base_timeline_path=base_timeline_path,
        worktree_timeline_path=worktree_timeline_path,
        run_id=run_id,
        recipe=recipe,
    )


def _append_e2e_event(
    store: SqliteTimelineStore,
    *,
    run_id: int,
    event_ids: _EventIds,
    event_name: str,
    timestamp: str,
    data: dict[str, object],
) -> None:
    record = TimelineRecord(
        event_id=event_ids.next_record_id("e2e"),
        timestamp=timestamp,
        event=event_name,
        source_event=event_name,
        data={**data, "e2e_run_id": run_id},
    )
    store.append(TimelineKey.for_e2e_run(run_id).to_store_key(), record)


def _emit_issue_lifecycle(
    *,
    writer: DefaultTimelineWriter,
    worktree_root: Path,
    event_ids: _EventIds,
    clock: _TimelineClock,
    issue: FacsimileIssueRecipe,
    e2e_run_id: int,
) -> None:
    coding_run_id = f"facsimile-{e2e_run_id}-{issue.issue_number}-code"
    review_run_id = f"facsimile-{e2e_run_id}-{issue.issue_number}-review"
    coding_run_dir = _write_run_dir(
        worktree_root,
        session_name=f"issue-{issue.issue_number}-code",
        run_id=coding_run_id,
        issue=issue,
        phase="coding",
        agent="agent:web",
        started_at=clock.tick(),
    )
    review_run_dir = _write_run_dir(
        worktree_root,
        session_name=f"review-{issue.issue_number}",
        run_id=review_run_id,
        issue=issue,
        phase="review",
        agent="agent:reviewer",
        started_at=clock.tick(),
    )

    coding_payload = _event_payload(
        issue=issue,
        run_id=coding_run_id,
        run_dir=coding_run_dir,
        worktree_root=worktree_root,
        task="code",
        agent="agent:web",
    )
    review_payload = _event_payload(
        issue=issue,
        run_id=review_run_id,
        run_dir=review_run_dir,
        worktree_root=worktree_root,
        task="review",
        agent="agent:reviewer",
    )

    _record(
        writer,
        event_ids=event_ids,
        event_name=EventName.SESSION_STARTED,
        timestamp=clock.tick(),
        data=coding_payload,
    )
    _record(
        writer,
        event_ids=event_ids,
        event_name=EventName.OBSERVATION_COMPLETION_DETECTED,
        timestamp=clock.tick(),
        data={
            **coding_payload,
            "completion_path_absolute": str(coding_run_dir / "completion-record.json"),
            "summary": "Facsimile coding session completed",
        },
    )
    _record(
        writer,
        event_ids=event_ids,
        event_name=EventName.SESSION_VALIDATION_PASSED,
        timestamp=clock.tick(),
        data={
            **coding_payload,
            "validation_source": "subprocess",
            "validation_cmd": "pytest tests/unit/test_dashboard_view_model.py -q",
            "validation_record_path": str(coding_run_dir / "validation-record.json"),
            "summary": "Focused validation passed",
        },
    )
    _record(
        writer,
        event_ids=event_ids,
        event_name=EventName.REVIEW_STARTED,
        timestamp=clock.tick(),
        data=review_payload,
    )
    _record(
        writer,
        event_ids=event_ids,
        event_name=EventName.REVIEW_EXCHANGE_STARTED,
        timestamp=clock.tick(),
        data={**review_payload, "rounds": 1},
    )
    _record(
        writer,
        event_ids=event_ids,
        event_name=EventName.REVIEW_EXCHANGE_ROUND_STARTED,
        timestamp=clock.tick(),
        data={**review_payload, "round_index": 1, "rounds": 1},
    )
    _record(
        writer,
        event_ids=event_ids,
        event_name=EventName.REVIEW_EXCHANGE_ROUND_COMPLETED,
        timestamp=clock.tick(),
        data={
            **review_payload,
            "round_index": 1,
            "rounds": 1,
            "reviewer_response_type": "approved",
            "reviewer_response_text": "Approved facsimile implementation.",
        },
    )
    _record(
        writer,
        event_ids=event_ids,
        event_name=EventName.REVIEW_EXCHANGE_COMPLETED,
        timestamp=clock.tick(),
        data={**review_payload, "round_index": 1, "rounds": 1, "status": "approved"},
    )
    _record(
        writer,
        event_ids=event_ids,
        event_name=EventName.REVIEW_APPROVED,
        timestamp=clock.tick(),
        data={
            **review_payload,
            "round_index": 1,
            "rounds": 1,
            "summary": "Review approved after one local-loop round",
            "risk": "low",
        },
    )
    _record(
        writer,
        event_ids=event_ids,
        event_name=EventName.ISSUE_PR_CREATED,
        timestamp=clock.tick(),
        data={
            **coding_payload,
            "pr_number": issue.pr_number,
            "pr_url": f"https://example.invalid/pull/{issue.pr_number}",
            "summary": f"PR #{issue.pr_number} created",
        },
    )
    _record(
        writer,
        event_ids=event_ids,
        event_name=EventName.ISSUE_COMPLETED,
        timestamp=clock.tick(),
        data={**coding_payload, "summary": "Issue completed"},
    )


def _event_payload(
    *,
    issue: FacsimileIssueRecipe,
    run_id: str,
    run_dir: Path,
    worktree_root: Path,
    task: str,
    agent: str,
) -> dict[str, object]:
    return {
        "issue_number": issue.issue_number,
        "run_id": run_id,
        "run_dir": str(run_dir),
        "session_id": run_id,
        "session_name": run_dir.parent.name,
        "task": task,
        "agent": agent,
        "branch_name": issue.branch_name,
        "worktree_path": str(worktree_root),
        "session_prompt_path": str(run_dir / "session-prompt.md"),
        "completion_path_absolute": str(run_dir / "completion-record.json"),
        "rework_cycle": 0,
    }


def _record(
    writer: DefaultTimelineWriter,
    *,
    event_ids: _EventIds,
    event_name: EventName,
    timestamp: datetime,
    data: dict[str, object],
) -> None:
    writer.record(
        TraceEvent(
            event_name,
            data,
            timestamp=timestamp,
            event_id=event_ids.next_trace_id(),
        )
    )


def _write_run_dir(
    worktree_root: Path,
    *,
    session_name: str,
    run_id: str,
    issue: FacsimileIssueRecipe,
    phase: str,
    agent: str,
    started_at: datetime,
) -> Path:
    run_dir = worktree_root / ".issue-orchestrator" / "sessions" / session_name / run_id
    review_dir = run_dir / "review-exchange"
    round_reviewer_dir = review_dir / "round-001" / "reviewer"
    round_coder_dir = review_dir / "round-001" / "coder"
    run_dir.mkdir(parents=True)
    round_reviewer_dir.mkdir(parents=True)
    round_coder_dir.mkdir(parents=True)

    _write_terminal_recording(
        run_dir / "terminal-recording.jsonl",
        f"{phase} session for issue {issue.issue_number}\n",
    )
    _write_terminal_recording(
        round_reviewer_dir / "terminal-recording.jsonl",
        f"reviewer round 1 for issue {issue.issue_number}\n",
    )
    _write_terminal_recording(
        round_coder_dir / "terminal-recording.jsonl",
        f"coder round 1 for issue {issue.issue_number}\n",
    )
    (run_dir / "session-prompt.md").write_text(
        f"Work on issue #{issue.issue_number}: {issue.title}\n",
        encoding="utf-8",
    )
    (run_dir / "completion-record.json").write_text(
        json.dumps(
            {
                "status": "completed",
                "outcome": "completed",
                "implementation": f"Facsimile {phase} work for issue {issue.issue_number}.",
                "problems": "None",
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    (run_dir / "validation-record.json").write_text(
        json.dumps(
            {
                "status": "passed",
                "command": "pytest tests/unit/test_dashboard_view_model.py -q",
                "exit_code": 0,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    (run_dir / "validation-output.log").write_text("1 passed\n", encoding="utf-8")
    (run_dir / "claude.jsonl").write_text(
        json.dumps({"type": "assistant", "content": "facsimile complete"}) + "\n",
        encoding="utf-8",
    )
    (review_dir / "summary.json").write_text(
        json.dumps({"status": "approved", "rounds": 1}, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (review_dir / "transcript.log").write_text(
        "reviewer: approved facsimile implementation\n",
        encoding="utf-8",
    )
    manifest_payload = {
        "session_name": session_name,
        "run_id": run_id,
        "run_dir": str(run_dir),
        "issue_number": issue.issue_number,
        "agent_label": agent,
        "backend": "facsimile",
        "worktree": str(worktree_root),
        "started_at": started_at.isoformat(),
        "completion_path": str(run_dir / "completion-record.json"),
        "completion_record_path": str(run_dir / "completion-record.json"),
        "validation_record_path": str(run_dir / "validation-record.json"),
        "validation_stdout": str(run_dir / "validation-output.log"),
        "claude_log_path": str(run_dir / "claude.jsonl"),
        "review_exchange_summary_path": str(review_dir / "summary.json"),
        "review_exchange_transcript_path": str(review_dir / "transcript.log"),
        "artifacts": {
            "terminal_recording": {
                "kind": "terminal_recording",
                "path": str(run_dir / "terminal-recording.jsonl"),
                "content_type": "application/x-ndjson",
            },
            "completion_record": {
                "kind": "completion_record",
                "path": str(run_dir / "completion-record.json"),
                "content_type": "application/json",
            },
            "validation_record": {
                "kind": "validation_record",
                "path": str(run_dir / "validation-record.json"),
                "content_type": "application/json",
            },
            "review_exchange_transcript": {
                "kind": "review_exchange_transcript",
                "path": str(review_dir / "transcript.log"),
                "content_type": "text/plain",
            },
        },
    }
    validated = validate_run_manifest_payload(
        manifest_payload,
        strict_required_artifacts=True,
    )
    (run_dir / "manifest.json").write_text(
        json.dumps(validated, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return run_dir


def _write_terminal_recording(path: Path, text: str) -> None:
    payload = base64.b64encode(text.encode("utf-8")).decode("ascii")
    events = [
        {"event_type": "resize", "offset_ms": 0, "rows": 30, "cols": 120},
        {"event_type": "output", "offset_ms": 20, "data_b64": payload},
    ]
    path.write_text(
        "\n".join(json.dumps(event, sort_keys=True) for event in events) + "\n",
        encoding="utf-8",
    )


def _junit_xml_for(tests: tuple[FacsimileTestRecipe, ...]) -> str:
    failures = sum(1 for test in tests if test.outcome == "failed")
    errors = sum(1 for test in tests if test.outcome == "error")
    skipped = sum(1 for test in tests if test.outcome == "skipped")
    cases: list[str] = []
    for test in tests:
        attrs = (
            f'classname="{escape(test.suite_name)}" '
            f'name="{escape(test.display_name)}" '
            f'time="{test.duration_seconds:.3f}"'
        )
        if test.outcome == "failed":
            cases.append(f'    <testcase {attrs}><failure message="failed"/></testcase>')
        elif test.outcome == "error":
            cases.append(f'    <testcase {attrs}><error message="error"/></testcase>')
        elif test.outcome == "skipped":
            cases.append(f'    <testcase {attrs}><skipped/></testcase>')
        else:
            cases.append(f"    <testcase {attrs}/>")
    total_time = sum(test.duration_seconds for test in tests)
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        f'<testsuite tests="{len(tests)}" failures="{failures}" errors="{errors}" '
        f'skipped="{skipped}" time="{total_time:.3f}">\n'
        + "\n".join(cases)
        + "\n</testsuite>\n"
    )


def _rewrite_run_window(
    db_path: Path,
    *,
    run_id: int,
    started_at: str,
    finished_at: str,
) -> None:
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            """
            UPDATE e2e_runs
            SET started_at = ?, finished_at = ?, current_test = NULL
            WHERE id = ?
            """,
            (started_at, finished_at, run_id),
        )
        conn.commit()


__all__ = [
    "E2EFacsimileRecipe",
    "FacsimileIssueRecipe",
    "FacsimileTestRecipe",
    "ISSUE_4057_NODEID",
    "MaterializedE2EFacsimile",
    "default_4057_facsimile_recipe",
    "materialize_e2e_4057_facsimile",
]
