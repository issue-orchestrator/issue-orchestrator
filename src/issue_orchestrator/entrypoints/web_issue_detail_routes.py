from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from ..contracts.ui_openapi_models import IssueDetailPayload
from ..execution.validation_failure_summary import load_validation_failure_summary
from ..infra.timeline_trace import is_timeline_trace_enabled
from ..view_models.dashboard import issue_url_for
from ..view_models.issue_detail import IssueStoryContext, build_issue_detail_view_model
from .e2e_affordances import _attach_issue_numbers_to_test_windows
from .timeline_presentation import (
    _build_phase_toc,
    _build_timeline_cycles,
    _decorate_timeline_events,
    _filter_timeline_events,
    _promote_e2e_test_event_fields,
    _retain_semantic_timeline_events,
)
from .web_session_context import (
    WebOrchestratorDependency,
    issue_title_for,
    resolve_issue_session_context,
)

logger = logging.getLogger(__name__)

web_issue_detail_router = APIRouter()
_VALID_DETAIL_VIEWS = {"user", "ops", "debug"}


def _normalize_detail_view(view: str) -> str:
    """Return a supported drawer view mode."""
    return view if view in _VALID_DETAIL_VIEWS else "user"


def _current_run_validation_diagnostic(
    orchestrator: Any,
    issue_number: int,
) -> dict[str, Any] | None:
    """Return a focused diagnostic for the latest/current run's validation state."""
    context = resolve_issue_session_context(orchestrator, issue_number)
    run_dir = context.run_dir
    if run_dir is None:
        return None
    summary = load_validation_failure_summary(run_dir)
    if summary is None:
        return None
    return {
        "state": "validation_failed",
        "run_dir": str(run_dir),
        "session_name": context.session_name,
        "reason": summary.reason,
        "suite": summary.suite,
        "command": summary.command,
        "exit_code": summary.exit_code,
        "failed_tests": list(summary.failed_tests),
        "failed_tests_preview": list(summary.failed_tests[:3]),
        "validation_record_path": summary.validation_record_path,
        "validation_stderr": summary.validation_stderr_path,
        "validation_stdout": summary.validation_stdout_path,
    }


def _apply_issue_detail_actions(
    orchestrator: Any,
    issue_number: int,
    payload: dict[str, Any],
) -> None:
    if orchestrator and orchestrator.deps.publish_recovery.can_retry_publish(
        issue_number,
        orchestrator.state,
    ):
        actions = payload.get("actions")
        if isinstance(actions, list):
            actions.insert(0, {"id": "retry_publish", "label": "Retry Publish"})


def _apply_issue_detail_run_diagnostic(payload: dict[str, Any], run_diagnostic: dict[str, Any]) -> None:
    actions = payload.get("actions")
    if isinstance(actions, list):
        actions.insert(
            0,
            {
                "id": "open_validation_failure",
                "label": "Validation Details",
                "run_dir": run_diagnostic.get("run_dir"),
            },
        )
    summary = payload.get("summary")
    if isinstance(summary, dict):
        summary["run_diagnostic"] = run_diagnostic
    failed_tests = run_diagnostic.get("failed_tests")
    if isinstance(failed_tests, list) and failed_tests:
        payload["status_explanation"] = (
            f"Current run failed validation: {len(failed_tests)} failing test(s) in "
            f"{run_diagnostic.get('command') or 'validation'}"
        )
        return
    payload["status_explanation"] = (
        f"Current run failed validation: {run_diagnostic.get('reason', 'validation failed')}"
    )


def _finalize_issue_detail_payload(
    *,
    orchestrator: Any,
    issue_number: int,
    payload: dict[str, Any],
    raw_events: list[dict[str, Any]],
    filtered_events: list[dict[str, Any]],
    events: list[dict[str, Any]],
    dropped_missing_semantics: int,
) -> dict[str, Any]:
    run_diagnostic = _current_run_validation_diagnostic(orchestrator, issue_number)
    _apply_issue_detail_actions(orchestrator, issue_number, payload)
    if run_diagnostic:
        _apply_issue_detail_run_diagnostic(payload, run_diagnostic)
    if is_timeline_trace_enabled():
        runs = payload.get("runs")
        run_count = len(runs) if isinstance(runs, list) else 0
        cycle_count = (
            sum(
                len(run.get("cycles", []))
                for run in runs
                if isinstance(run, dict) and isinstance(run.get("cycles"), list)
            )
            if isinstance(runs, list) else 0
        )
        logger.info(
            "[TIMELINE] api.issue_detail issue=%s raw=%s filtered=%s semantic=%s "
            "dropped_missing_semantics=%s runs=%s cycles=%s",
            issue_number,
            len(raw_events),
            len(filtered_events),
            len(events),
            dropped_missing_semantics,
            run_count,
            cycle_count,
        )
    diagnostic = _timeline_missing_diagnostic(
        orchestrator,
        issue_number,
        events,
        dropped_missing_semantics=dropped_missing_semantics,
    )
    if diagnostic:
        summary = payload.get("summary")
        if isinstance(summary, dict):
            summary["timeline_diagnostic"] = diagnostic
        if run_diagnostic is None:
            payload["status_explanation"] = (
                f"Timeline data missing ({', '.join(diagnostic.get('signals', []))})"
            )
    return payload


@web_issue_detail_router.get("/api/timeline/{issue_number}")
async def get_issue_timeline(
    issue_number: int,
    orchestrator: WebOrchestratorDependency,
) -> JSONResponse:
    """Get timeline events for an issue."""
    if not orchestrator:
        return JSONResponse({"error": "Orchestrator not running"}, status_code=503)

    reader = orchestrator.deps.timeline_reader
    try:
        stream = reader.read(issue_number, limit=2000)
    except RuntimeError as exc:
        logger.error("Timeline read failed for issue %d: %s", issue_number, exc)
        return JSONResponse(
            status_code=503,
            content={"error": "timeline_unavailable", "detail": str(exc)},
        )
    payload = stream.to_dict()
    raw_events = payload.get("events", [])
    filtered_events = _filter_timeline_events(raw_events)
    events, dropped_missing_semantics = _retain_semantic_timeline_events(filtered_events)
    events = _decorate_timeline_events(events, issue_number)
    payload["events"] = events
    payload["phase_toc"] = _build_phase_toc(events)
    payload["cycles"] = _build_timeline_cycles(events)
    if is_timeline_trace_enabled():
        logger.info(
            "[TIMELINE] api.timeline issue=%s raw=%s filtered=%s semantic=%s "
            "dropped_missing_semantics=%s cycles=%s",
            issue_number,
            len(raw_events),
            len(filtered_events),
            len(events),
            dropped_missing_semantics,
            len(payload["cycles"]) if isinstance(payload.get("cycles"), list) else 0,
        )
    diagnostic = _timeline_missing_diagnostic(
        orchestrator,
        issue_number,
        events,
        dropped_missing_semantics=dropped_missing_semantics,
    )
    if diagnostic:
        payload["diagnostic"] = diagnostic
    return JSONResponse(payload)


@web_issue_detail_router.get("/api/issue-detail/{issue_number}", response_model=IssueDetailPayload)
async def get_issue_detail(
    issue_number: int,
    orchestrator: WebOrchestratorDependency,
    view: str = "user",
) -> IssueDetailPayload | JSONResponse:
    """Get an issue-detail payload for drawer rendering."""
    if not orchestrator:
        return JSONResponse({"error": "Orchestrator not running"}, status_code=503)

    reader = orchestrator.deps.timeline_reader
    try:
        stream = reader.read(issue_number, limit=2000)
    except RuntimeError as exc:
        logger.error("Timeline read failed for issue %d: %s", issue_number, exc)
        return JSONResponse(
            status_code=503,
            content={"error": "timeline_unavailable", "detail": str(exc)},
        )
    timeline = stream.to_dict()
    raw_events = timeline.get("events", [])
    filtered_events = _filter_timeline_events(raw_events)
    events, dropped_missing_semantics = _retain_semantic_timeline_events(filtered_events)
    events = _decorate_timeline_events(events, issue_number)
    phase_toc = _build_phase_toc(events)
    cycles = _build_timeline_cycles(events)
    payload = build_issue_detail_view_model(
        issue_number=issue_number,
        title=issue_title_for(orchestrator, issue_number),
        issue_url=issue_url_for(orchestrator.config, issue_number),
        events=events,
        phase_toc=phase_toc,
        cycles=cycles,
        context=_build_issue_story_context(orchestrator, issue_number),
        view=_normalize_detail_view(view),
    )
    payload = _finalize_issue_detail_payload(
        orchestrator=orchestrator,
        issue_number=issue_number,
        payload=payload,
        raw_events=raw_events,
        filtered_events=filtered_events,
        events=events,
        dropped_missing_semantics=dropped_missing_semantics,
    )
    return IssueDetailPayload.model_validate(payload)


@web_issue_detail_router.get("/api/e2e-run-detail/{run_id}")
async def get_e2e_run_detail(
    run_id: int,
    orchestrator: WebOrchestratorDependency,
    view: str = "user",
) -> JSONResponse:
    """Get E2E run detail using the shared issue-detail timeline pipeline."""
    from ..domain.timeline_key import TimelineKey
    from ..timeline import TimelineStream

    if not orchestrator:
        return JSONResponse({"error": "Orchestrator not running"}, status_code=503)

    store_key = TimelineKey.for_e2e_run(run_id).to_store_key()
    try:
        records = orchestrator.deps.timeline_store.read(store_key, limit=5000)
    except RuntimeError as exc:
        logger.error("Timeline read failed for E2E run %d: %s", run_id, exc)
        return JSONResponse(
            status_code=503,
            content={"error": "timeline_unavailable", "detail": str(exc)},
        )

    if not records:
        return JSONResponse(
            {"error": "not_found", "detail": f"No timeline events for E2E run {run_id}"},
            status_code=404,
        )

    e2e_records = [record for record in records if record.event != "e2e.agent_snapshot"]
    snapshot_records = [record for record in records if record.event == "e2e.agent_snapshot"]

    stream = TimelineStream.from_records(store_key, e2e_records)
    raw_events = [event.to_dict() for event in stream.events]
    _promote_e2e_test_event_fields(
        raw_events,
        e2e_records,
        run_id=run_id,
        e2e_db_path=orchestrator.config.repo_root / ".issue-orchestrator" / "e2e.db",
    )
    e2e_events = _filter_timeline_events(raw_events)
    agent_events = [record.data for record in snapshot_records if isinstance(record.data, dict)]
    if not agent_events:
        agent_events = _load_orchestrator_events_for_run(orchestrator, run_id)

    matcher_view = _normalize_detail_view(view)
    events = _attach_issue_numbers_to_test_windows(
        e2e_events,
        agent_events,
        run_id=run_id,
        view=matcher_view,
    )
    payload = build_issue_detail_view_model(
        issue_number=store_key,
        title=f"E2E Run #{run_id}",
        issue_url="",
        events=events,
        phase_toc=_build_phase_toc(events),
        cycles=_build_timeline_cycles(events),
        context=None,
        view=matcher_view,
    )
    return JSONResponse(payload)


@web_issue_detail_router.get("/api/e2e-run/{run_id}/issue-detail/{issue_number}")
async def get_e2e_issue_detail(
    run_id: int,
    issue_number: int,
    orchestrator: WebOrchestratorDependency,
    view: str = "user",
) -> JSONResponse:
    """Return issue detail for an ephemeral E2E issue from a specific run."""
    from ..execution.timeline_store import SqliteTimelineStore
    from ..infra.e2e_db import E2EDB
    from ..infra.e2e_worktree import get_e2e_worktree_path
    from ..timeline import TimelineStream

    if not orchestrator:
        return JSONResponse({"error": "Orchestrator not running"}, status_code=503)

    repo_root = orchestrator.config.repo_root
    e2e_db_path = repo_root / ".issue-orchestrator" / "e2e.db"
    if not e2e_db_path.exists():
        return JSONResponse(
            {"error": "not_found", "detail": "E2E database not found"},
            status_code=404,
        )

    db = E2EDB(e2e_db_path)
    run = db.get_run(run_id)
    if run is None:
        return JSONResponse(
            {"error": "not_found", "detail": f"E2E run {run_id} not found"},
            status_code=404,
        )

    worktree_timeline = (
        get_e2e_worktree_path(repo_root)
        / ".issue-orchestrator"
        / "state"
        / "timeline.sqlite"
    )
    if not worktree_timeline.exists():
        return JSONResponse(
            {
                "error": "not_found",
                "detail": f"E2E worktree timeline missing for run {run_id}",
            },
            status_code=404,
        )

    try:
        worktree_store = SqliteTimelineStore(db_path=worktree_timeline)
        all_records = worktree_store.read(issue_number, limit=2000)
    except Exception as exc:
        logger.exception(
            "Failed to read e2e-worktree timeline for run %d issue %d",
            run_id,
            issue_number,
        )
        return JSONResponse(
            {"error": "db_error", "detail": str(exc)},
            status_code=500,
        )

    window_start = run.started_at
    window_end = run.finished_at or "9999-12-31T23:59:59Z"
    records = [
        record
        for record in all_records
        if window_start <= record.timestamp <= window_end
    ]
    if not records:
        return JSONResponse(
            {
                "error": "not_found",
                "detail": (
                    f"No events for issue {issue_number} in E2E run {run_id} "
                    f"worktree timeline (window {window_start} → {window_end})"
                ),
            },
            status_code=404,
        )

    timeline = TimelineStream.from_records(issue_number, records).to_dict()
    raw_events = timeline.get("events", [])
    filtered_events = _filter_timeline_events(raw_events)
    events, dropped_missing_semantics = _retain_semantic_timeline_events(filtered_events)
    events = _decorate_timeline_events(events, issue_number)
    payload = build_issue_detail_view_model(
        issue_number=issue_number,
        title=f"Issue #{issue_number}",
        issue_url=issue_url_for(orchestrator.config, issue_number),
        events=events,
        phase_toc=_build_phase_toc(events),
        cycles=_build_timeline_cycles(events),
        context=None,
        view=_normalize_detail_view(view),
    )
    payload = _finalize_issue_detail_payload(
        orchestrator=orchestrator,
        issue_number=issue_number,
        payload=payload,
        raw_events=raw_events,
        filtered_events=filtered_events,
        events=events,
        dropped_missing_semantics=dropped_missing_semantics,
    )
    return JSONResponse(payload)


def _load_orchestrator_events_for_run(
    orchestrator: Any,
    run_id: int,
) -> list[dict[str, Any]]:
    """Read orchestrator events from timeline.sqlite for an E2E run's time window."""
    from ..infra.e2e_timeline import read_orchestrator_events_by_window
    from ..infra.e2e_worktree import get_e2e_worktree_path

    if not orchestrator:
        return []

    repo_root = orchestrator.config.repo_root
    db_path = repo_root / ".issue-orchestrator" / "e2e.db"
    worktree_timeline = (
        get_e2e_worktree_path(repo_root)
        / ".issue-orchestrator"
        / "state"
        / "timeline.sqlite"
    )
    if not db_path.exists() or not worktree_timeline.exists():
        return []

    try:
        from ..infra.e2e_db import E2EDB

        db = E2EDB(db_path)
        run = db.get_run(run_id)
        if not run:
            return []
        return read_orchestrator_events_by_window(
            worktree_timeline,
            started_at=run.started_at,
            finished_at=run.finished_at,
        )
    except Exception:
        logger.debug("Could not load orchestrator events for E2E run %d", run_id, exc_info=True)
        return []


def _build_issue_story_context(  # noqa: C901, PLR0912 - story assembly pulls from multiple orchestrator-owned stores
    orchestrator: Any,
    issue_number: int,
) -> IssueStoryContext | None:
    """Assemble story context from orchestrator state for one issue."""
    if not orchestrator:
        return None

    state = orchestrator.state
    config = orchestrator.config

    active_runtime: int | None = None
    active_task_kind: str | None = None
    for session in state.active_sessions:
        if session.issue.number == issue_number:
            active_runtime = session.runtime_minutes
            active_task_kind = session.key.task.value
            break

    labels: tuple[str, ...] = ()
    for issue in state.cached_queue_issues:
        if issue.number == issue_number:
            labels = tuple(issue.labels)
            break
    if not labels:
        for session in state.active_sessions:
            if session.issue.number == issue_number:
                labels = tuple(session.issue.labels)
                break

    dependency_problem = state.dependency_problems.get(issue_number)
    dependency_summary = dependency_problem.summary if dependency_problem else None

    rework_cycle = 0
    for rework in state.pending_reworks:
        if rework.resolve_issue_number() == issue_number:
            rework_cycle = rework.rework_cycle
            break

    pr_url: str | None = None
    pr_number: int | None = None
    for review in state.pending_reviews:
        if review.issue_number == issue_number:
            pr_url = review.pr_url
            pr_number = review.pr_number
            break
    if not pr_url:
        for entry in state.session_history:
            if entry.issue_number == issue_number and entry.pr_url:
                pr_url = entry.pr_url
                break

    return IssueStoryContext(
        flow_stage=_determine_issue_flow_stage(
            issue_number,
            labels,
            active_task_kind,
            state,
            pr_url,
        ),
        active_runtime_minutes=active_runtime,
        active_task_kind=active_task_kind,
        labels=labels,
        dependency_summary=dependency_summary,
        current_rework_cycle=rework_cycle,
        max_rework_cycles=config.max_rework_cycles,
        pr_url=pr_url,
        pr_number=pr_number,
    )


def _determine_issue_flow_stage(
    issue_number: int,
    labels: tuple[str, ...],
    active_task_kind: str | None,
    state: Any,
    pr_url: str | None,
) -> str:
    """Determine the flow stage for an issue."""
    from ..domain.models import _base_of, _is_blocking_label

    if active_task_kind is not None:
        return "in_progress"
    if any(_is_blocking_label(label) for label in labels):
        return "blocked"
    if any(_base_of(label) == "pr-pending" for label in labels):
        return "awaiting_merge"

    for entry in state.session_history:
        if entry.issue_number == issue_number:
            if entry.status == "completed":
                return "done" if not pr_url else "awaiting_merge"
            if entry.status in ("blocked", "needs_human", "failed", "timed_out"):
                return "blocked"
    return "queued"


def _timeline_missing_diagnostic(
    orchestrator: Any,
    issue_number: int,
    events: list[dict[str, Any]],
    *,
    dropped_missing_semantics: int = 0,
) -> dict[str, Any] | None:
    """Return diagnostic details when timeline is unexpectedly empty."""
    if events or not orchestrator:
        return None

    state = orchestrator.state
    signals: list[str] = []
    if any(session.issue.number == issue_number for session in state.active_sessions):
        signals.append("active_session_present")
    if any(entry.issue_number == issue_number for entry in state.session_history):
        signals.append("session_history_present")
    if any(review.issue_number == issue_number for review in state.pending_reviews):
        signals.append("pending_review_present")
    if any(rework.resolve_issue_number() == issue_number for rework in state.pending_reworks):
        signals.append("pending_rework_present")
    if issue_number in state.completed_today:
        signals.append("completed_today_present")

    context = resolve_issue_session_context(orchestrator, issue_number)
    if context.run_dir is not None:
        signals.append("session_run_present")
    if dropped_missing_semantics > 0:
        signals.append("logical_semantics_missing")
    if not signals:
        return None

    logger.warning(
        "Timeline missing for issue #%s despite signals: %s",
        issue_number,
        ", ".join(signals),
    )
    from ..infra.repo_identity import state_dir

    timeline_db_path = state_dir(orchestrator.config.repo_root) / "timeline.sqlite"
    state_name = "logical_semantics_missing" if dropped_missing_semantics > 0 else "expected_history_missing"
    return {
        "state": state_name,
        "signals": signals,
        "expected_timeline_store": str(timeline_db_path),
        "expected_timeline_store_exists": timeline_db_path.exists(),
        "resolved_run_dir": str(context.run_dir) if context.run_dir else None,
        "dropped_missing_semantics": dropped_missing_semantics,
    }
