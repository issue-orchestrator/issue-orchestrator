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
    BlockedCodingAttempt,
    CompletedCodingAttempt,
    E2EFailureDetailsMissing,
    FailedCodingAttempt,
    FailedE2ETestExecution,
    MissingCodingEvidence,
    MissingE2ETestEvidence,
    MissingReviewEvidence,
    PassedE2ETestExecution,
    PublishFailedCodingAttempt,
    ReviewApproved,
    ReviewChangesRequested,
    ReviewFailed,
    ReviewNotReached,
    ReviewRunning,
    ReviewSkipped,
    RunningCodingAttempt,
    RunningE2ETestExecution,
    ValidationEvidenceMissing,
    ValidationFailed,
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


def test_blocked_coder_projects_as_terminal_blocked_state() -> None:
    started = _event(
        "agent.coding_started",
        event_id="coding-start",
        timestamp="2026-04-21T12:00:00Z",
        agent="agent:coder",
        run_dir="/tmp/run-1",
    )
    blocked = _event(
        "agent.blocked",
        event_id="coding-blocked",
        timestamp="2026-04-21T12:10:00Z",
        agent="agent:coder",
        run_dir="/tmp/run-1",
        summary="Need product decision",
    )

    lifecycle = project_issue_lifecycle(
        issue_number=5723,
        title="Timeline regression",
        events=[started, blocked],
        cycles=[{"cycle": 1, "events": [started, blocked]}],
        review_required=True,
    )

    cycle = lifecycle.cycles[0]
    assert isinstance(cycle.coder, BlockedCodingAttempt)
    assert cycle.coder.reason == "Need product decision"
    assert isinstance(cycle.review, ReviewNotReached)
    assert cycle.review.reason == "coding_failed"
    assert cycle.outcome == "blocked"


def test_publish_failed_after_coding_completion_projects_publish_failed_attempt() -> None:
    started, completed, validation, *_ = _complete_issue_events()
    publish_failed = _event(
        "publish.failed",
        event_id="publish-failed",
        timestamp="2026-04-21T12:20:00Z",
        agent="agent:coder",
        run_dir="/tmp/run-1",
        summary="Push rejected",
    )

    lifecycle = project_issue_lifecycle(
        issue_number=5723,
        title="Timeline regression",
        events=[started, completed, validation, publish_failed],
        cycles=[{"cycle": 1, "events": [started, completed, validation, publish_failed]}],
    )

    cycle = lifecycle.cycles[0]
    assert isinstance(cycle.coder, PublishFailedCodingAttempt)
    assert cycle.coder.reason == "Push rejected"
    assert cycle.coder.completed_at == "2026-04-21T12:10:00Z"
    assert cycle.coder.publish_failed_at == "2026-04-21T12:20:00Z"
    assert cycle.coder.diagnostics[0].code == "publish.failed"
    assert isinstance(cycle.review, ReviewNotReached)
    assert cycle.review.reason == "publish_failed"
    assert cycle.outcome == "publish_failed"


def test_review_completion_observation_does_not_replace_coding_completion() -> None:
    started, completed, validation, review_start, _changes = _complete_issue_events()
    review_observation = _event(
        "agent.coding_completed",
        event_id="review-observation",
        timestamp="2026-04-21T12:17:00Z",
        summary="review_approved",
        source_event="observation.completion_detected",
    )
    approved = _event(
        "review.approved",
        event_id="review-approved",
        timestamp="2026-04-21T12:18:00Z",
        reviewer_agent="agent:reviewer",
        run_dir="/tmp/review-1",
    )

    lifecycle = project_issue_lifecycle(
        issue_number=5723,
        title="Timeline regression",
        events=[started, completed, validation, review_start, review_observation, approved],
        cycles=[
            {
                "cycle": 1,
                "events": [started, completed, validation, review_start, review_observation, approved],
            }
        ],
        review_required=True,
    )

    cycle = lifecycle.cycles[0]
    assert isinstance(cycle.coder, CompletedCodingAttempt)
    assert isinstance(cycle.review, ReviewApproved)
    assert cycle.outcome == "approved"


def test_legacy_review_only_cycle_uses_full_event_window_for_semantics() -> None:
    started, completed, validation, review_start, _changes = _complete_issue_events()
    review_observation = _event(
        "agent.coding_completed",
        event_id="review-observation",
        timestamp="2026-04-21T12:17:00Z",
        summary="review_approved",
        source_event="observation.completion_detected",
    )
    approved = _event(
        "review.approved",
        event_id="review-approved",
        timestamp="2026-04-21T12:18:00Z",
        reviewer_agent="agent:reviewer",
        run_dir="/tmp/review-1",
    )
    all_events = [started, completed, validation, review_start, review_observation, approved]

    lifecycle = project_issue_lifecycle(
        issue_number=5723,
        title="Timeline regression",
        events=all_events,
        cycles=[{"cycle": 1, "status": "completed", "events": [review_start, review_observation]}],
        review_required=True,
    )

    cycle = lifecycle.cycles[0]
    assert isinstance(cycle.coder, CompletedCodingAttempt)
    assert isinstance(cycle.review, ReviewApproved)


def test_review_reached_without_coding_terminal_projects_missing_completion_evidence() -> None:
    started = _event(
        "agent.coding_started",
        event_id="coding-start",
        timestamp="2026-04-21T12:00:00Z",
        agent="agent:coder",
        run_dir="/tmp/run-1",
    )
    review_start = _event(
        "review.started",
        event_id="review-start",
        timestamp="2026-04-21T12:14:00Z",
        reviewer_agent="agent:reviewer",
        run_dir="/tmp/review-1",
    )
    approved = _event(
        "review.approved",
        event_id="review-approved",
        timestamp="2026-04-21T12:18:00Z",
        reviewer_agent="agent:reviewer",
        run_dir="/tmp/review-1",
    )

    lifecycle = project_issue_lifecycle(
        issue_number=5723,
        title="Timeline regression",
        events=[started, review_start, approved],
        cycles=[{"cycle": 1, "events": [started, review_start, approved]}],
        review_required=True,
    )

    cycle = lifecycle.cycles[0]
    assert isinstance(cycle.coder, MissingCodingEvidence)
    assert cycle.coder.expected_state == "completed"
    assert [item.evidence for item in cycle.coder.missing] == ["coding_terminal_event"]
    assert isinstance(cycle.review, ReviewApproved)
    assert cycle.outcome == "approved"


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


def test_review_approved_projects_transcript_evidence() -> None:
    started, completed, validation, review_start, _changes = _complete_issue_events()
    approved = _event(
        "review.approved",
        event_id="review-approved",
        timestamp="2026-04-21T12:18:00Z",
        reviewer_agent="agent:reviewer",
        run_dir="/tmp/review-1",
        actions=[{"type": "open_review_transcript"}],
    )

    lifecycle = project_issue_lifecycle(
        issue_number=5723,
        title="Timeline regression",
        events=[started, completed, validation, review_start, approved],
        cycles=[{"cycle": 1, "events": [started, completed, validation, review_start, approved]}],
        review_required=True,
    )

    review = lifecycle.cycles[0].review
    assert isinstance(review, ReviewApproved)
    assert review.transcript.kind == "available"
    assert lifecycle.cycles[0].outcome == "approved"


def test_review_running_skipped_and_failed_project_distinct_states() -> None:
    started, completed, validation, review_start, _changes = _complete_issue_events()

    running = project_issue_lifecycle(
        issue_number=5723,
        title="Timeline regression",
        events=[started, completed, validation, review_start],
        cycles=[{"cycle": 1, "events": [started, completed, validation, review_start]}],
        review_required=True,
    ).cycles[0]
    assert isinstance(running.review, ReviewRunning)
    assert running.outcome == "review_in_progress"

    skipped_event = _event(
        "review.skipped",
        event_id="review-skipped",
        timestamp="2026-04-21T12:18:00Z",
        summary="Review disabled for fixture",
    )
    skipped = project_issue_lifecycle(
        issue_number=5723,
        title="Timeline regression",
        events=[started, completed, validation, skipped_event],
        cycles=[{"cycle": 1, "events": [started, completed, validation, skipped_event]}],
    ).cycles[0]
    assert isinstance(skipped.review, ReviewSkipped)
    assert skipped.outcome == "review_skipped"

    failed_event = _event(
        "review_exchange.failed",
        event_id="review-failed",
        timestamp="2026-04-21T12:18:00Z",
        reviewer_agent="agent:reviewer",
        run_dir="/tmp/review-1",
        summary="Reviewer crashed",
    )
    failed = project_issue_lifecycle(
        issue_number=5723,
        title="Timeline regression",
        events=[started, completed, validation, review_start, failed_event],
        cycles=[{"cycle": 1, "events": [started, completed, validation, review_start, failed_event]}],
        review_required=True,
    ).cycles[0]
    assert isinstance(failed.review, ReviewFailed)
    assert failed.outcome == "review_failed"


def test_review_not_reached_reasons_are_semantically_distinct() -> None:
    coding_start = _event(
        "agent.coding_started",
        event_id="coding-start",
        timestamp="2026-04-21T12:00:00Z",
        agent="agent:coder",
        run_dir="/tmp/run-1",
    )
    running = project_issue_lifecycle(
        issue_number=5723,
        title="Timeline regression",
        events=[coding_start],
        cycles=[{"cycle": 1, "events": [coding_start]}],
    ).cycles[0]
    assert isinstance(running.review, ReviewNotReached)
    assert running.review.reason == "coding_in_progress"

    failed_event = _event(
        "session.failed",
        event_id="coding-failed",
        timestamp="2026-04-21T12:10:00Z",
        agent="agent:coder",
        run_dir="/tmp/run-1",
        summary="Session failed",
    )
    failed = project_issue_lifecycle(
        issue_number=5723,
        title="Timeline regression",
        events=[coding_start, failed_event],
        cycles=[{"cycle": 1, "events": [coding_start, failed_event]}],
    ).cycles[0]
    assert isinstance(failed.review, ReviewNotReached)
    assert failed.review.reason == "coding_failed"

    started, completed, _validation, *_ = _complete_issue_events()
    validation_failed = _event(
        "validation.failed",
        event_id="validation-failed",
        timestamp="2026-04-21T12:12:00Z",
        run_dir="/tmp/run-1",
        summary="pytest failed",
        artifacts=[_artifact("validation", "/tmp/run-1/validation.json")],
    )
    validation = project_issue_lifecycle(
        issue_number=5723,
        title="Timeline regression",
        events=[started, completed, validation_failed],
        cycles=[{"cycle": 1, "events": [started, completed, validation_failed]}],
    ).cycles[0]
    assert isinstance(validation.review, ReviewNotReached)
    assert validation.review.reason == "validation_failed"

    not_required = project_issue_lifecycle(
        issue_number=5723,
        title="Timeline regression",
        events=[started, completed],
        cycles=[{"cycle": 1, "events": [started, completed]}],
    ).cycles[0]
    assert isinstance(not_required.review, ReviewNotReached)
    assert not_required.review.reason == "not_required"


def test_validation_failed_and_missing_evidence_branches_project_distinctly() -> None:
    started, completed, *_ = _complete_issue_events()

    failed_validation_event = _event(
        "validation.failed",
        event_id="validation-failed",
        timestamp="2026-04-21T12:12:00Z",
        run_dir="/tmp/run-1",
        summary="pytest failed",
        artifacts=[_artifact("validation", "/tmp/run-1/validation.json")],
    )
    failed_cycle = project_issue_lifecycle(
        issue_number=5723,
        title="Timeline regression",
        events=[started, completed, failed_validation_event],
        cycles=[{"cycle": 1, "events": [started, completed, failed_validation_event]}],
    ).cycles[0]
    assert isinstance(failed_cycle.coder, CompletedCodingAttempt)
    assert isinstance(failed_cycle.coder.validation, ValidationFailed)

    missing_record_event = _event(
        "validation.passed",
        event_id="validation-missing-record",
        timestamp="2026-04-21T12:12:00Z",
        run_dir="/tmp/run-1",
        summary="pytest passed",
    )
    missing_record_cycle = project_issue_lifecycle(
        issue_number=5723,
        title="Timeline regression",
        events=[started, completed, missing_record_event],
        cycles=[{"cycle": 1, "events": [started, completed, missing_record_event]}],
    ).cycles[0]
    assert isinstance(missing_record_cycle.coder, CompletedCodingAttempt)
    assert isinstance(missing_record_cycle.coder.validation, ValidationEvidenceMissing)
    assert missing_record_cycle.coder.validation.diagnostics[0].code == "validation.record_missing"

    missing_run_dir_event = _event(
        "validation.failed",
        event_id="validation-missing-run-dir",
        timestamp="2026-04-21T12:12:00Z",
        summary="pytest failed",
        artifacts=[_artifact("validation", "/tmp/run-1/validation.json")],
    )
    missing_run_dir_cycle = project_issue_lifecycle(
        issue_number=5723,
        title="Timeline regression",
        events=[started, completed, missing_run_dir_event],
        cycles=[{"cycle": 1, "events": [started, completed, missing_run_dir_event]}],
    ).cycles[0]
    assert isinstance(missing_run_dir_cycle.coder, CompletedCodingAttempt)
    assert isinstance(missing_run_dir_cycle.coder.validation, ValidationEvidenceMissing)
    assert missing_run_dir_cycle.coder.validation.diagnostics[0].code == "validation.run_dir_missing"


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


def test_issue_lifecycle_without_presentation_cycles_groups_by_logical_cycle() -> None:
    cycle_one_start = _event(
        "agent.coding_started",
        event_id="coding-start-1",
        timestamp="2026-04-21T12:00:00Z",
        agent="agent:coder",
        run_dir="/tmp/run-1",
        logical_run=1,
        logical_cycle=1,
    )
    cycle_one_review = _event(
        "review.approved",
        event_id="review-approved-1",
        timestamp="2026-04-21T12:10:00Z",
        reviewer_agent="agent:reviewer",
        run_dir="/tmp/review-1",
        logical_run=1,
        logical_cycle=1,
    )
    cycle_two_start = _event(
        "agent.coding_started",
        event_id="coding-start-2",
        timestamp="2026-04-21T12:20:00Z",
        agent="agent:coder",
        run_dir="/tmp/run-2",
        logical_run=1,
        logical_cycle=2,
    )
    cycle_two_failed = _event(
        "session.failed",
        event_id="coding-failed-2",
        timestamp="2026-04-21T12:25:00Z",
        agent="agent:coder",
        run_dir="/tmp/run-2",
        logical_run=1,
        logical_cycle=2,
    )

    lifecycle = project_issue_lifecycle(
        issue_number=5723,
        title="Timeline regression",
        events=[cycle_one_start, cycle_one_review, cycle_two_start, cycle_two_failed],
        cycles=[],
        review_required=True,
    )

    assert [cycle.cycle_number for cycle in lifecycle.cycles] == [1, 2]
    assert isinstance(lifecycle.cycles[0].coder, MissingCodingEvidence)
    assert isinstance(lifecycle.cycles[0].review, ReviewApproved)
    assert isinstance(lifecycle.cycles[1].coder, FailedCodingAttempt)
    assert isinstance(lifecycle.cycles[1].review, ReviewNotReached)
    assert lifecycle.cycles[1].review.reason == "coding_failed"


def test_issue_lifecycle_rejects_mixed_logical_cycle_annotations() -> None:
    logical_event = _event(
        "agent.coding_started",
        event_id="coding-start-1",
        timestamp="2026-04-21T12:00:00Z",
        agent="agent:coder",
        run_dir="/tmp/run-1",
        logical_run=1,
        logical_cycle=1,
    )
    legacy_event = _event(
        "session.completed",
        event_id="coding-completed-legacy",
        timestamp="2026-04-21T12:10:00Z",
        agent="agent:coder",
        run_dir="/tmp/run-1",
    )

    with pytest.raises(LifecycleProjectionError, match="must not mix logical cycle fields"):
        project_issue_lifecycle(
            issue_number=5723,
            title="Timeline regression",
            events=[legacy_event, logical_event],
            cycles=[],
        )

    with pytest.raises(LifecycleProjectionError, match="must not mix logical cycle fields"):
        project_issue_lifecycle(
            issue_number=5723,
            title="Timeline regression",
            events=[logical_event, legacy_event],
            cycles=[],
        )


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


def test_started_e2e_test_without_completion_projects_running_execution() -> None:
    events = [
        _event("e2e.run_started", event_id="run-start", timestamp="2026-04-21T12:00:00Z"),
        _event(
            "e2e.test_started",
            event_id="test-start",
            timestamp="2026-04-21T12:01:00Z",
            nodeid="tests/e2e/test_time.py::test_visible",
        ),
    ]

    iteration = project_e2e_run_iteration(run_id=88, events=events)

    test = iteration.e2e_run.tests[0]
    assert isinstance(test, RunningE2ETestExecution)


def test_e2e_run_without_test_events_projects_missing_test_evidence() -> None:
    events = [
        _event("e2e.run_started", event_id="run-start", timestamp="2026-04-21T12:00:00Z"),
    ]

    iteration = project_e2e_run_iteration(run_id=88, events=events)

    test = iteration.e2e_run.tests[0]
    assert isinstance(test, MissingE2ETestEvidence)
    assert test.diagnostics[0].code == "e2e.tests_missing"


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
