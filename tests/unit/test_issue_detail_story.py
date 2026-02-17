"""Semantics-first tests for issue detail story synthesis."""

from __future__ import annotations

from issue_orchestrator.view_models.issue_detail import (
    IssueStoryContext,
    _build_journey_cycles,
    _build_journey_steps,
    _build_status_explanation,
    _collect_cycle_artifacts,
    build_issue_detail_view_model,
    filter_last_run_cycles,
)


def _ctx(**overrides: object) -> IssueStoryContext:
    base: dict[str, object] = {
        "flow_stage": "queued",
        "active_runtime_minutes": None,
        "active_task_kind": None,
        "labels": (),
        "dependency_summary": None,
        "current_rework_cycle": 0,
        "max_rework_cycles": 3,
        "pr_url": None,
        "pr_number": None,
    }
    base.update(overrides)
    return IssueStoryContext(**base)  # type: ignore[arg-type]


def _intent_for(event: str, task: str | None = None) -> str:
    if task == "review" or event.startswith("review.") or event.startswith("review_exchange."):
        return "review"
    if event.startswith("rework.") or task == "rework":
        return "rework"
    if event.startswith("session.") and not event.startswith("session.validation"):
        return "coding"
    return "orchestrator"


def _phase_for(intent: str, logical_cycle: int) -> str:
    if intent == "review":
        return "review"
    if intent == "rework":
        return "rework"
    if intent == "orchestrator":
        return "orchestrator"
    return "rework" if logical_cycle > 1 else "coding"


def _evt(
    event: str,
    *,
    timestamp: str,
    logical_run: int,
    logical_cycle: int,
    task: str | None = None,
    status: str = "started",
    summary: str | None = None,
    agent: str | None = None,
    artifacts: list[dict[str, str]] | None = None,
) -> dict[str, object]:
    intent = _intent_for(event, task)
    return {
        "event": event,
        "timestamp": timestamp,
        "status": status,
        "step": event.split(".")[-1],
        "phase": "in_progress",
        "timeline_schema_version": 3,
        "event_intent": intent,
        "review_oriented": intent == "review",
        "logical_run": logical_run,
        "logical_cycle": logical_cycle,
        "logical_phase": _phase_for(intent, logical_cycle),
        "task": task,
        "summary": summary,
        "agent": agent,
        "artifacts": artifacts or [],
    }


def test_status_explanation_running_review() -> None:
    ctx = _ctx(flow_stage="in_progress", active_runtime_minutes=7, active_task_kind="review")
    assert _build_status_explanation(ctx, []) == "Code review in progress (7 min)"


def test_status_explanation_awaiting_merge() -> None:
    ctx = _ctx(flow_stage="awaiting_merge", pr_number=4124)
    assert _build_status_explanation(ctx, []) == "PR #4124 approved — ready to merge"


def test_journey_cycles_require_logical_semantics() -> None:
    events = [{"event": "session.started", "timestamp": "2026-02-16T10:00:00Z"}]
    assert _build_journey_cycles(events, "2026-02-16") == []


def test_journey_cycles_group_by_logical_run_and_cycle() -> None:
    events = [
        _evt("session.started", timestamp="2026-02-16T10:00:00Z", logical_run=1, logical_cycle=1, agent="agent:backend"),
        _evt("session.completed", timestamp="2026-02-16T10:10:00Z", logical_run=1, logical_cycle=1, status="completed"),
        _evt("review.started", timestamp="2026-02-16T10:11:00Z", logical_run=1, logical_cycle=1, task="review"),
        _evt("review.changes_requested", timestamp="2026-02-16T10:12:00Z", logical_run=1, logical_cycle=1, task="review", status="failed"),
        _evt("rework.started", timestamp="2026-02-16T10:20:00Z", logical_run=1, logical_cycle=2, task="rework", agent="agent:backend"),
        _evt("session.completed", timestamp="2026-02-16T10:30:00Z", logical_run=1, logical_cycle=2, status="completed"),
        _evt("session.started", timestamp="2026-02-16T11:00:00Z", logical_run=2, logical_cycle=1, agent="agent:backend"),
    ]

    cycles = _build_journey_cycles(events, "2026-02-16")
    assert len(cycles) == 3
    assert [c["lifecycle"] for c in cycles] == [1, 1, 2]
    assert [c["iteration"] for c in cycles] == [1, 2, 1]


def test_last_run_filter_uses_logical_run() -> None:
    cycles = [
        {"lifecycle": 1, "cycle": 1, "iteration": 1, "run_id": "a"},
        {"lifecycle": 1, "cycle": 2, "iteration": 2, "run_id": "b"},
        {"lifecycle": 2, "cycle": 3, "iteration": 1, "run_id": "c"},
    ]
    filtered = filter_last_run_cycles(cycles)
    assert len(filtered) == 1
    assert filtered[0]["lifecycle"] == 2


def test_rework_cycle_outcome_prefixed_for_non_review_cycle() -> None:
    events = [
        _evt("rework.started", timestamp="2026-02-16T10:20:00Z", logical_run=1, logical_cycle=2, task="rework"),
        _evt("session.failed", timestamp="2026-02-16T10:30:00Z", logical_run=1, logical_cycle=2, status="failed", summary="compile error"),
    ]
    cycles = _build_journey_cycles(events, "2026-02-16")
    assert cycles[0]["outcome"].startswith("Rework →")


def test_phase_groups_follow_logical_phase_not_event_name_guessing() -> None:
    events = [
        _evt("session.started", timestamp="2026-02-16T10:00:00Z", logical_run=1, logical_cycle=1, agent="agent:backend"),
        _evt("validation.completed", timestamp="2026-02-16T10:01:00Z", logical_run=1, logical_cycle=1),
        _evt("review.started", timestamp="2026-02-16T10:02:00Z", logical_run=1, logical_cycle=1, task="review"),
        _evt("review.approved", timestamp="2026-02-16T10:03:00Z", logical_run=1, logical_cycle=1, task="review"),
    ]
    cycles = _build_journey_cycles(events, "2026-02-16")
    labels = [group["label"] for group in cycles[0]["phase_groups"]]
    assert labels == ["Coding", "Orchestrator", "Review"]


def test_collect_cycle_artifacts_extracts_pr_and_review_feedback() -> None:
    events = [
        _evt(
            "issue.pr_created",
            timestamp="2026-02-16T10:00:00Z",
            logical_run=1,
            logical_cycle=1,
            artifacts=[{"type": "pull_request", "label": "PR", "value": "https://github.com/org/repo/pull/4124"}],
        ),
        _evt("review.approved", timestamp="2026-02-16T10:01:00Z", logical_run=1, logical_cycle=1, task="review"),
    ]
    artifacts = _collect_cycle_artifacts(events)
    assert artifacts["pr_number"] == 4124
    assert artifacts["has_review_feedback"] is True


def test_timeline_steps_preserve_day_field() -> None:
    events = [
        _evt("session.started", timestamp="2026-02-15T10:00:00Z", logical_run=1, logical_cycle=1),
        _evt("session.completed", timestamp="2026-02-16T10:00:00Z", logical_run=1, logical_cycle=1, status="completed"),
    ]
    steps = _build_journey_steps(events, "2026-02-16")
    assert [step["day"] for step in steps] == ["2026-02-15", "2026-02-16"]


def test_build_issue_detail_view_model_returns_runs() -> None:
    events = [
        _evt("session.started", timestamp="2026-02-16T10:00:00Z", logical_run=1, logical_cycle=1, agent="agent:backend"),
        _evt("session.completed", timestamp="2026-02-16T10:10:00Z", logical_run=1, logical_cycle=1, status="completed"),
        _evt("session.started", timestamp="2026-02-16T11:00:00Z", logical_run=2, logical_cycle=1, agent="agent:backend"),
    ]
    payload = build_issue_detail_view_model(
        issue_number=4057,
        title="Timeline",
        issue_url="https://github.com/org/repo/issues/4057",
        events=events,
        phase_toc=[],
        cycles=[],
        context=_ctx(flow_stage="in_progress", active_runtime_minutes=1, active_task_kind="code"),
    )
    assert payload["run_count"] == 2
    assert payload["runs"][1]["expanded"] is True
