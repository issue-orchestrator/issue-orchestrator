"""Tests for projecting timeline payloads into semantic lifecycle models."""

from __future__ import annotations

import pytest

from issue_orchestrator.view_models.lifecycle_projection import (
    LifecycleProjectionError,
    project_dashboard_lifecycle_container,
    project_e2e_run_iteration,
    project_e2e_suite_lifecycle_container,
    project_e2e_suite_lifecycle_container_for_run,
    project_issue_lifecycles_from_events,
    project_issue_lifecycle,
)
from issue_orchestrator.view_models.lifecycle_semantics import (
    CompletedCodingAttempt,
    E2EFailureDetailsMissing,
    FailedE2ETestExecution,
    MissingCodingEvidence,
    MissingE2ETestEvidence,
    MissingReviewEvidence,
    PassedE2ETestExecution,
    ReviewChangesRequested,
    RunningCodingAttempt,
    command_kinds,
)


def _event(
    event: str,
    *,
    event_id: str,
    timestamp: str,
    agent: str | None = None,
    reviewer_agent: str | None = None,
    run_dir: str | None = None,
    summary: str | None = None,
    artifacts: list[dict[str, str]] | None = None,
    **extra: object,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "event": event,
        "event_id": event_id,
        "timestamp": timestamp,
        "summary": summary or event,
        "artifacts": artifacts or [],
    }
    if agent is not None:
        payload["agent"] = agent
    if reviewer_agent is not None:
        payload["reviewer_agent"] = reviewer_agent
    if run_dir is not None:
        payload["run_dir"] = run_dir
    payload.update(extra)
    return payload


def _artifact(kind: str, value: str) -> dict[str, str]:
    return {"type": kind, "label": kind, "value": value}


def _cycle(*events: dict[str, object]) -> dict[str, object]:
    return {"cycle": 1, "status": "completed", "events": list(events)}


def _complete_issue_events() -> tuple[dict[str, object], ...]:
    return (
        _event(
            "agent.coding_started",
            event_id="coding-start",
            timestamp="2026-04-21T12:00:00Z",
            agent="agent:coder",
            run_dir="/tmp/run-1",
        ),
        _event(
            "agent.coding_completed",
            event_id="coding-done",
            timestamp="2026-04-21T12:10:00Z",
            agent="agent:coder",
            run_dir="/tmp/run-1",
            summary="Implemented the fix",
            artifacts=[
                _artifact("completion_record", "/tmp/run-1/completion.json"),
                _artifact("worktree", "/tmp/wt"),
            ],
        ),
        _event(
            "validation.passed",
            event_id="validation-passed",
            timestamp="2026-04-21T12:12:00Z",
            agent="agent:validator",
            run_dir="/tmp/run-1",
            summary="pytest passed",
            artifacts=[_artifact("validation", "/tmp/run-1/validation.json")],
        ),
        _event(
            "review.started",
            event_id="review-start",
            timestamp="2026-04-21T12:14:00Z",
            reviewer_agent="agent:reviewer",
            run_dir="/tmp/review-1",
        ),
        _event(
            "review.changes_requested",
            event_id="review-changes",
            timestamp="2026-04-21T12:18:00Z",
            reviewer_agent="agent:reviewer",
            run_dir="/tmp/review-1",
            reviewer_response_text="Needs a regression test",
        ),
    )


def _complete_issue_lifecycle():
    events = _complete_issue_events()
    return project_issue_lifecycle(
        issue_number=5723,
        title="Timeline regression",
        events=events,
        cycles=[_cycle(*events)],
        review_required=True,
    )


def test_projection_builds_completed_coder_review_and_validation_children() -> None:
    lifecycle = _complete_issue_lifecycle()

    issue_cycle = lifecycle.cycles[0]
    assert isinstance(issue_cycle.coder, CompletedCodingAttempt)
    assert issue_cycle.coder.has_validated_output()
    assert issue_cycle.coder.can_open_session_recording()
    assert command_kinds(issue_cycle.coder.commands) == (
        "show_event_details",
        "open_completion_record",
        "open_session_recording",
    )

    assert isinstance(issue_cycle.review, ReviewChangesRequested)
    assert issue_cycle.review.feedback_summary == "Needs a regression test"
    assert command_kinds(issue_cycle.review.commands) == (
        "show_event_details",
        "open_review_feedback",
    )


def test_completed_coder_without_completion_record_becomes_missing_evidence() -> None:
    started = _event(
        "agent.coding_started",
        event_id="coding-start",
        timestamp="2026-04-21T12:00:00Z",
        agent="agent:coder",
        run_dir="/tmp/run-1",
    )
    completed = _event(
        "agent.coding_completed",
        event_id="coding-done",
        timestamp="2026-04-21T12:10:00Z",
        agent="agent:coder",
        run_dir="/tmp/run-1",
    )

    lifecycle = project_issue_lifecycle(
        issue_number=5723,
        title="Timeline regression",
        events=[started, completed],
        cycles=[_cycle(started, completed)],
    )

    coder = lifecycle.cycles[0].coder
    assert isinstance(coder, MissingCodingEvidence)
    assert [item.evidence for item in coder.missing] == ["completion_record"]
    assert coder.diagnostics[0].code == "coding.completion_record.missing"


def test_review_required_without_review_stage_becomes_missing_evidence() -> None:
    started, completed, validation, *_ = _complete_issue_events()

    lifecycle = project_issue_lifecycle(
        issue_number=5723,
        title="Timeline regression",
        events=[started, completed, validation],
        cycles=[_cycle(started, completed, validation)],
        review_required=True,
    )

    review = lifecycle.cycles[0].review
    assert isinstance(review, MissingReviewEvidence)
    assert [item.evidence for item in review.missing] == ["review_stage"]
    assert review.diagnostics[0].code == "review.review_stage.missing"


def test_dashboard_projection_container_iterates_singleton_issue_model() -> None:
    events = _complete_issue_events()

    container = project_dashboard_lifecycle_container(
        subject_label="Dashboard",
        issue_number=5723,
        title="Timeline regression",
        events=events,
        cycles=[_cycle(*events)],
        review_required=True,
    )

    iterations = tuple(container.iter_iterations())
    assert len(iterations) == 1
    assert iterations[0].issue_lifecycles[0].issue_number == 5723


def test_issue_lifecycle_without_events_is_explicit_missing_evidence_cycle() -> None:
    lifecycle = project_issue_lifecycle(
        issue_number=5723,
        title="Timeline regression",
        events=[],
        cycles=[],
    )

    assert len(lifecycle.cycles) == 1
    coder = lifecycle.cycles[0].coder
    assert isinstance(coder, MissingCodingEvidence)
    assert coder.expected_state == "running"
    assert coder.missing[0].evidence == "coding_start"


def test_issue_lifecycles_from_events_group_issues_and_synthesize_cycles() -> None:
    complete_events = tuple(dict(event, issue_number=5723) for event in _complete_issue_events())
    running_event = _event(
        "agent.coding_started",
        event_id="coding-start-2",
        timestamp="2026-04-21T12:20:00Z",
        agent="agent:coder-2",
        run_dir="/tmp/run-2",
        issue_number=5724,
    )

    lifecycles = project_issue_lifecycles_from_events(
        (*complete_events, running_event),
        title_prefix="E2E Issue",
        review_required=True,
    )

    assert [lifecycle.issue_number for lifecycle in lifecycles] == [5723, 5724]
    assert lifecycles[0].title == "E2E Issue #5723"
    assert isinstance(lifecycles[0].cycles[0].coder, CompletedCodingAttempt)
    assert isinstance(lifecycles[1].cycles[0].coder, RunningCodingAttempt)


def test_e2e_projection_builds_passed_and_failed_tests() -> None:
    events = [
        _event("e2e.run_started", event_id="run-start", timestamp="2026-04-21T12:00:00Z"),
        _event(
            "e2e.test_started",
            event_id="test-a-start",
            timestamp="2026-04-21T12:01:00Z",
            nodeid="tests/e2e/test_a.py::test_a",
        ),
        _event(
            "e2e.test_completed",
            event_id="test-a-done",
            timestamp="2026-04-21T12:01:05Z",
            nodeid="tests/e2e/test_a.py::test_a",
            outcome="passed",
            duration_seconds=5.0,
        ),
        _event(
            "e2e.test_started",
            event_id="test-b-start",
            timestamp="2026-04-21T12:02:00Z",
            nodeid="tests/e2e/test_b.py::test_b",
        ),
        _event(
            "e2e.test_completed",
            event_id="test-b-done",
            timestamp="2026-04-21T12:02:08Z",
            nodeid="tests/e2e/test_b.py::test_b",
            outcome="failed",
            longrepr="assert visible_time",
            duration_seconds=8.0,
        ),
        _event("e2e.run_finished", event_id="run-finish", timestamp="2026-04-21T12:03:00Z"),
    ]

    iteration = project_e2e_run_iteration(run_id=88, events=events)

    first, second = iteration.e2e_run.tests
    assert isinstance(first, PassedE2ETestExecution)
    assert first.duration_seconds == 5.0
    assert isinstance(second, FailedE2ETestExecution)
    assert second.failure.longrepr == "assert visible_time"


def test_failed_e2e_test_without_longrepr_is_explicit_missing_evidence() -> None:
    events = [
        _event("e2e.run_started", event_id="run-start", timestamp="2026-04-21T12:00:00Z"),
        _event(
            "e2e.test_started",
            event_id="test-start",
            timestamp="2026-04-21T12:01:00Z",
            nodeid="tests/e2e/test_time.py::test_visible",
        ),
        _event(
            "e2e.test_completed",
            event_id="test-done",
            timestamp="2026-04-21T12:01:05Z",
            nodeid="tests/e2e/test_time.py::test_visible",
            outcome="failed",
        ),
    ]

    iteration = project_e2e_run_iteration(run_id=88, events=events)

    test = iteration.e2e_run.tests[0]
    assert isinstance(test, FailedE2ETestExecution)
    assert isinstance(test.failure, E2EFailureDetailsMissing)
    assert test.failure.diagnostics[0].code == "e2e.failure_details_missing"


def test_completed_e2e_test_without_started_event_is_missing_evidence() -> None:
    events = [
        _event("e2e.run_started", event_id="run-start", timestamp="2026-04-21T12:00:00Z"),
        _event(
            "e2e.test_completed",
            event_id="test-done",
            timestamp="2026-04-21T12:01:05Z",
            nodeid="tests/e2e/test_time.py::test_visible",
            outcome="passed",
        ),
    ]

    iteration = project_e2e_run_iteration(run_id=88, events=events)

    test = iteration.e2e_run.tests[0]
    assert isinstance(test, MissingE2ETestEvidence)
    assert test.missing[0].evidence == "test_started_event"


def test_e2e_suite_container_requires_linked_issue_lifecycle() -> None:
    event_start = _event(
        "e2e.test_started",
        event_id="test-start",
        timestamp="2026-04-21T12:01:00Z",
        nodeid="tests/e2e/test_time.py::test_visible",
    )
    event_done = _event(
        "e2e.test_completed",
        event_id="test-done",
        timestamp="2026-04-21T12:01:05Z",
        nodeid="tests/e2e/test_time.py::test_visible",
        outcome="passed",
        issue_affordances=[{"issue_number": 5723, "run_id": 88}],
    )
    iteration = project_e2e_run_iteration(run_id=88, events=[event_start, event_done])

    with pytest.raises(LifecycleProjectionError, match="linked_issue_lifecycle_missing"):
        project_e2e_suite_lifecycle_container(
            subject_label="E2E",
            runs=[iteration],
        )


def test_e2e_suite_container_accepts_linked_issue_lifecycle() -> None:
    event_start = _event(
        "e2e.test_started",
        event_id="test-start",
        timestamp="2026-04-21T12:01:00Z",
        nodeid="tests/e2e/test_time.py::test_visible",
    )
    event_done = _event(
        "e2e.test_completed",
        event_id="test-done",
        timestamp="2026-04-21T12:01:05Z",
        nodeid="tests/e2e/test_time.py::test_visible",
        outcome="passed",
        issue_affordances=[{"issue_number": 5723, "run_id": 88}],
    )
    iteration = project_e2e_run_iteration(
        run_id=88,
        events=[event_start, event_done],
        linked_issue_lifecycles=[_complete_issue_lifecycle()],
    )

    container = project_e2e_suite_lifecycle_container(
        subject_label="E2E",
        runs=[iteration],
    )

    assert tuple(container.iter_iterations()) == (iteration,)


def test_e2e_suite_container_for_run_populates_linked_issue_lifecycle() -> None:
    test_start = _event(
        "e2e.test_started",
        event_id="test-start",
        timestamp="2026-04-21T12:01:00Z",
        nodeid="tests/e2e/test_time.py::test_visible",
    )
    test_done = _event(
        "e2e.test_completed",
        event_id="test-done",
        timestamp="2026-04-21T12:01:05Z",
        nodeid="tests/e2e/test_time.py::test_visible",
        outcome="passed",
        issue_affordances=[{"issue_number": 5723, "run_id": 88}],
    )
    agent_events = tuple(dict(event, issue_number=5723) for event in _complete_issue_events())

    container = project_e2e_suite_lifecycle_container_for_run(
        run_id=88,
        events=[test_start, test_done],
        agent_events=agent_events,
        subject_label="E2E",
    )

    iteration = tuple(container.iter_iterations())[0]
    assert iteration.e2e_run.linked_issue_lifecycles[0].issue_number == 5723
    test = iteration.e2e_run.tests[0]
    assert isinstance(test, PassedE2ETestExecution)
    assert [linked.issue_number for linked in test.linked_issues] == [5723]
