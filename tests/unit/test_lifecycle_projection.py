"""Tests for projecting timeline payloads into semantic lifecycle models."""

from __future__ import annotations

import pytest

from issue_orchestrator.domain.logical_run_projection import group_events_by_logical_cycle
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
    ValidationPassed,
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


def _project_first_issue_cycle(
    events: tuple[dict[str, object], ...],
    *,
    review_required: bool = False,
):
    lifecycle = project_issue_lifecycle(
        issue_number=5723,
        title="Timeline regression",
        events=events,
        cycles=[{"cycle": 1, "events": list(events)}],
        review_required=review_required,
    )
    return lifecycle.cycles[0]


def _coding_validation_kind(cycle) -> str | None:
    coder = cycle.coder
    if isinstance(coder, CompletedCodingAttempt | PublishFailedCodingAttempt):
        return coder.validation.kind
    return None


def _missing_evidence_names(stage: object) -> tuple[str, ...]:
    missing = getattr(stage, "missing", ())
    return tuple(item.evidence for item in missing)


def _diagnostic_codes(stage: object) -> tuple[str, ...]:
    diagnostics = getattr(stage, "diagnostics", ())
    return tuple(diagnostic.code for diagnostic in diagnostics)


def _stage_command_kinds(stage: object) -> tuple[str, ...]:
    commands = getattr(stage, "commands", ())
    return command_kinds(commands)


def _cycle_summary(cycle) -> dict[str, object]:
    return {
        "cycle_number": cycle.cycle_number,
        "outcome": cycle.outcome,
        "coder_kind": cycle.coder.kind,
        "review_kind": cycle.review.kind,
        "validation_kind": _coding_validation_kind(cycle),
        "coder_commands": _stage_command_kinds(cycle.coder),
        "review_commands": _stage_command_kinds(cycle.review),
        "coder_started_at": getattr(cycle.coder, "started_at", None),
        "coder_completed_at": getattr(cycle.coder, "completed_at", None),
        "review_started_at": getattr(cycle.review, "started_at", None),
        "review_completed_at": getattr(cycle.review, "completed_at", None),
    }


def _issue_lifecycle_summary(lifecycle) -> dict[str, object]:
    return {
        "issue_number": lifecycle.issue_number,
        "cycle_count": len(lifecycle.cycles),
        "cycles": tuple(_cycle_summary(cycle) for cycle in lifecycle.cycles),
    }


def _approved_review_event() -> dict[str, object]:
    return _event(
        "review.approved",
        event_id="review-approved",
        timestamp="2026-04-21T12:18:00Z",
        reviewer_agent="agent:reviewer",
        run_dir="/tmp/review-1",
        actions=[{"type": "open_review_transcript"}],
    )


def _lifecycle_state_matrix_cases() -> tuple[dict[str, object], ...]:
    started, completed, validation, review_start, changes_requested = (
        _complete_issue_events()
    )
    validation_failed = _event(
        "validation.failed",
        event_id="validation-failed",
        timestamp="2026-04-21T12:12:00Z",
        agent="agent:validator",
        run_dir="/tmp/run-1",
        summary="pytest failed",
        artifacts=[_artifact("validation", "/tmp/run-1/validation.json")],
    )
    publish_failed = _event(
        "publish.failed",
        event_id="publish-failed",
        timestamp="2026-04-21T12:20:00Z",
        agent="agent:coder",
        run_dir="/tmp/run-1",
        summary="Push rejected",
    )
    blocked = _event(
        "agent.blocked",
        event_id="coding-blocked",
        timestamp="2026-04-21T12:10:00Z",
        agent="agent:coder",
        run_dir="/tmp/run-1",
        summary="Need product decision",
    )
    failed = _event(
        "session.failed",
        event_id="coding-failed",
        timestamp="2026-04-21T12:10:00Z",
        agent="agent:coder",
        run_dir="/tmp/run-1",
        summary="Session failed",
    )
    review_failed = _event(
        "review_exchange.failed",
        event_id="review-failed",
        timestamp="2026-04-21T12:18:00Z",
        reviewer_agent="agent:reviewer",
        run_dir="/tmp/review-1",
        summary="Reviewer crashed",
    )
    review_skipped = _event(
        "review.skipped",
        event_id="review-skipped",
        timestamp="2026-04-21T12:18:00Z",
        summary="Review disabled for fixture",
    )
    completion_without_record = _event(
        "agent.coding_completed",
        event_id="coding-done-no-record",
        timestamp="2026-04-21T12:10:00Z",
        agent="agent:coder",
        run_dir="/tmp/run-1",
        summary="Implemented the fix",
    )
    validation_without_record = _event(
        "validation.passed",
        event_id="validation-without-record",
        timestamp="2026-04-21T12:12:00Z",
        agent="agent:validator",
        run_dir="/tmp/run-1",
        summary="pytest passed",
    )
    return (
        {
            "name": "completed_review_approved",
            "events": (
                started,
                completed,
                validation,
                review_start,
                _approved_review_event(),
            ),
            "expected": {
                "coder_kind": "completed_coding_attempt",
                "review_kind": "review_approved",
                "validation_kind": "passed",
                "outcome": "approved",
                "coder_commands": (
                    "show_event_details",
                    "open_completion_record",
                    "open_session_recording",
                ),
                "review_commands": ("show_event_details",),
            },
            "expected_types": (CompletedCodingAttempt, ReviewApproved),
        },
        {
            "name": "running_coder",
            "events": (started,),
            "expected": {
                "coder_kind": "running_coding_attempt",
                "review_kind": "review_not_reached",
                "validation_kind": None,
                "outcome": "in_progress",
                "coder_commands": ("show_event_details", "open_session_recording"),
                "review_reason": "coding_in_progress",
            },
            "expected_types": (RunningCodingAttempt, ReviewNotReached),
        },
        {
            "name": "blocked_coder",
            "events": (started, blocked),
            "expected": {
                "coder_kind": "blocked_coding_attempt",
                "review_kind": "review_not_reached",
                "validation_kind": None,
                "outcome": "blocked",
                "coder_commands": ("show_event_details",),
                "review_reason": "coding_failed",
            },
            "expected_types": (BlockedCodingAttempt, ReviewNotReached),
        },
        {
            "name": "failed_coder",
            "events": (started, failed),
            "expected": {
                "coder_kind": "failed_coding_attempt",
                "review_kind": "review_not_reached",
                "validation_kind": None,
                "outcome": "failed",
                "coder_commands": ("show_event_details",),
                "review_reason": "coding_failed",
            },
            "expected_types": (FailedCodingAttempt, ReviewNotReached),
        },
        {
            "name": "publish_failed_after_completion",
            "events": (started, completed, validation, publish_failed),
            "expected": {
                "coder_kind": "publish_failed_coding_attempt",
                "review_kind": "review_not_reached",
                "validation_kind": "passed",
                "outcome": "publish_failed",
                "coder_diagnostics": ("publish.failed",),
                "review_reason": "publish_failed",
            },
            "expected_types": (PublishFailedCodingAttempt, ReviewNotReached),
        },
        {
            "name": "validation_failed",
            "events": (started, completed, validation_failed),
            "expected": {
                "coder_kind": "completed_coding_attempt",
                "review_kind": "review_not_reached",
                "validation_kind": "failed",
                "outcome": "completed",
                "review_reason": "validation_failed",
            },
            "expected_types": (CompletedCodingAttempt, ReviewNotReached),
        },
        {
            "name": "review_running",
            "events": (started, completed, validation, review_start),
            "expected": {
                "coder_kind": "completed_coding_attempt",
                "review_kind": "review_running",
                "validation_kind": "passed",
                "outcome": "review_in_progress",
                "review_commands": ("show_event_details",),
            },
            "expected_types": (CompletedCodingAttempt, ReviewRunning),
        },
        {
            "name": "review_changes_requested",
            "events": (started, completed, validation, review_start, changes_requested),
            "expected": {
                "coder_kind": "completed_coding_attempt",
                "review_kind": "review_changes_requested",
                "validation_kind": "passed",
                "outcome": "changes_requested",
                "review_commands": ("show_event_details", "open_review_feedback"),
            },
            "expected_types": (CompletedCodingAttempt, ReviewChangesRequested),
        },
        {
            "name": "review_failed",
            "events": (started, completed, validation, review_start, review_failed),
            "expected": {
                "coder_kind": "completed_coding_attempt",
                "review_kind": "review_failed",
                "validation_kind": "passed",
                "outcome": "review_failed",
                "review_diagnostics": ("review.failed",),
            },
            "expected_types": (CompletedCodingAttempt, ReviewFailed),
        },
        {
            "name": "review_skipped",
            "events": (started, completed, validation, review_skipped),
            "expected": {
                "coder_kind": "completed_coding_attempt",
                "review_kind": "review_skipped",
                "validation_kind": "passed",
                "outcome": "review_skipped",
            },
            "expected_types": (CompletedCodingAttempt, ReviewSkipped),
        },
        {
            "name": "missing_coding_completion_record",
            "events": (started, completion_without_record),
            "expected": {
                "coder_kind": "missing_coding_evidence",
                "review_kind": "review_not_reached",
                "validation_kind": None,
                "outcome": "missing_coding_evidence",
                "coder_missing": ("completion_record",),
                "coder_diagnostics": ("coding.completion_record.missing",),
                "review_reason": "coding_failed",
            },
            "expected_types": (MissingCodingEvidence, ReviewNotReached),
        },
        {
            "name": "missing_validation_record",
            "events": (started, completed, validation_without_record),
            "expected": {
                "coder_kind": "completed_coding_attempt",
                "review_kind": "review_not_reached",
                "validation_kind": "missing_evidence",
                "outcome": "completed",
                "review_reason": "not_required",
            },
            "expected_types": (CompletedCodingAttempt, ReviewNotReached),
        },
        {
            "name": "missing_required_review",
            "events": (started, completed, validation),
            "review_required": True,
            "expected": {
                "coder_kind": "completed_coding_attempt",
                "review_kind": "missing_review_evidence",
                "validation_kind": "passed",
                "outcome": "missing_review_evidence",
                "review_missing": ("review_stage",),
                "review_diagnostics": ("review.review_stage.missing",),
            },
            "expected_types": (CompletedCodingAttempt, MissingReviewEvidence),
        },
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


def test_coder_session_recording_command_omits_phase_context_without_round() -> None:
    lifecycle = _complete_issue_lifecycle()

    issue_cycle = lifecycle.cycles[0]
    assert isinstance(issue_cycle.coder, CompletedCodingAttempt)
    session_recording = issue_cycle.coder.session_recording
    assert session_recording.kind == "available"
    assert session_recording.command.round_index is None
    assert session_recording.command.session_role is None


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


@pytest.mark.parametrize(
    ("name", "events", "expected_state", "missing"),
    (
        (
            "running",
            (
                _event(
                    "agent.coding_started",
                    event_id="coding-start-no-agent",
                    timestamp="2026-04-21T12:00:00Z",
                ),
            ),
            "running",
            ("agent",),
        ),
        (
            "blocked",
            (
                _event(
                    "agent.blocked",
                    event_id="coding-blocked-no-agent",
                    timestamp="2026-04-21T12:10:00Z",
                    summary="No agent on blocked event",
                ),
            ),
            "blocked",
            ("agent",),
        ),
        (
            "failed",
            (
                _event(
                    "session.failed",
                    event_id="coding-failed-no-agent",
                    timestamp="2026-04-21T12:10:00Z",
                    summary="No agent on failed event",
                ),
            ),
            "failed",
            ("agent",),
        ),
    ),
    ids=("running", "blocked", "failed"),
)
def test_missing_coding_evidence_expected_state_variants(
    name: str,
    events: tuple[dict[str, object], ...],
    expected_state: str,
    missing: tuple[str, ...],
) -> None:
    lifecycle = project_issue_lifecycle(
        issue_number=5723,
        title=f"Missing {name} coding evidence",
        events=events,
        cycles=[{"cycle": 1, "events": list(events)}],
    )

    coder = lifecycle.cycles[0].coder
    assert isinstance(coder, MissingCodingEvidence)
    assert coder.expected_state == expected_state
    assert _missing_evidence_names(coder) == missing
    assert _stage_command_kinds(coder) == ("show_event_details",)


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


def test_failed_coding_attempt_ignores_stale_review_start() -> None:
    started, _, _, review_start, _ = _complete_issue_events()
    failed = _event(
        "session.failed",
        event_id="coding-failed",
        timestamp="2026-04-21T12:20:00Z",
        agent="agent:coder",
        run_dir="/tmp/run-1",
        summary="Exceeded timeout",
    )

    cycle = _project_first_issue_cycle((started, review_start, failed))

    assert isinstance(cycle.coder, FailedCodingAttempt)
    assert isinstance(cycle.review, ReviewNotReached)
    assert cycle.review.reason == "coding_failed"
    assert cycle.outcome == "failed"


def test_failed_coding_attempt_ignores_stale_review_approved() -> None:
    started, _, _, _, _ = _complete_issue_events()
    failed = _event(
        "session.failed",
        event_id="coding-failed",
        timestamp="2026-04-21T12:20:00Z",
        agent="agent:coder",
        run_dir="/tmp/run-1",
        summary="Exceeded timeout",
    )

    cycle = _project_first_issue_cycle((started, _approved_review_event(), failed))

    assert isinstance(cycle.coder, FailedCodingAttempt)
    assert isinstance(cycle.review, ReviewNotReached)
    assert cycle.review.reason == "coding_failed"
    assert cycle.outcome == "failed"


def test_blocked_coding_attempt_ignores_stale_review_start() -> None:
    started, _, _, review_start, _ = _complete_issue_events()
    blocked = _event(
        "agent.blocked",
        event_id="coding-blocked",
        timestamp="2026-04-21T12:20:00Z",
        agent="agent:coder",
        run_dir="/tmp/run-1",
        summary="Need product decision",
    )

    cycle = _project_first_issue_cycle((started, review_start, blocked))

    assert isinstance(cycle.coder, BlockedCodingAttempt)
    assert isinstance(cycle.review, ReviewNotReached)
    assert cycle.review.reason == "coding_failed"
    assert cycle.outcome == "blocked"


def test_publish_failed_coding_attempt_ignores_stale_review_start() -> None:
    started, completed, validation, review_start, _ = _complete_issue_events()
    publish_failed = _event(
        "publish.failed",
        event_id="publish-failed",
        timestamp="2026-04-21T12:20:00Z",
        agent="agent:coder",
        run_dir="/tmp/run-1",
        summary="Push rejected",
    )

    cycle = _project_first_issue_cycle(
        (started, completed, validation, review_start, publish_failed)
    )

    assert isinstance(cycle.coder, PublishFailedCodingAttempt)
    assert isinstance(cycle.review, ReviewNotReached)
    assert cycle.review.reason == "publish_failed"
    assert cycle.outcome == "publish_failed"


def test_validation_failed_attempt_ignores_stale_review_start() -> None:
    started, completed, _, review_start, _ = _complete_issue_events()
    validation_failed = _event(
        "validation.failed",
        event_id="validation-failed",
        timestamp="2026-04-21T12:12:00Z",
        agent="agent:validator",
        run_dir="/tmp/run-1",
        summary="pytest failed",
        artifacts=[_artifact("validation", "/tmp/run-1/validation.json")],
    )

    cycle = _project_first_issue_cycle(
        (started, completed, validation_failed, review_start),
        review_required=True,
    )

    assert isinstance(cycle.coder, CompletedCodingAttempt)
    assert isinstance(cycle.coder.validation, ValidationFailed)
    assert isinstance(cycle.review, ReviewNotReached)
    assert cycle.review.reason == "validation_failed"
    assert cycle.outcome == "completed"


def test_publish_failed_after_coding_completion_projects_publish_failed_attempt() -> (
    None
):
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
        cycles=[
            {"cycle": 1, "events": [started, completed, validation, publish_failed]}
        ],
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
        events=[
            started,
            completed,
            validation,
            review_start,
            review_observation,
            approved,
        ],
        cycles=[
            {
                "cycle": 1,
                "events": [
                    started,
                    completed,
                    validation,
                    review_start,
                    review_observation,
                    approved,
                ],
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
    all_events = [
        started,
        completed,
        validation,
        review_start,
        review_observation,
        approved,
    ]

    lifecycle = project_issue_lifecycle(
        issue_number=5723,
        title="Timeline regression",
        events=all_events,
        cycles=[
            {
                "cycle": 1,
                "status": "completed",
                "events": [review_start, review_observation],
            }
        ],
        review_required=True,
    )

    cycle = lifecycle.cycles[0]
    assert isinstance(cycle.coder, CompletedCodingAttempt)
    assert isinstance(cycle.review, ReviewApproved)


def test_missing_coding_terminal_ignores_stale_review_signals() -> None:
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
    assert isinstance(cycle.review, ReviewNotReached)
    assert cycle.review.reason == "coding_failed"
    assert cycle.outcome == "missing_coding_evidence"


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
        cycles=[
            {
                "cycle": 1,
                "events": [started, completed, validation, review_start, approved],
            }
        ],
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
        cycles=[
            {"cycle": 1, "events": [started, completed, validation, skipped_event]}
        ],
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
        cycles=[
            {
                "cycle": 1,
                "events": [started, completed, validation, review_start, failed_event],
            }
        ],
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
    assert (
        missing_record_cycle.coder.validation.diagnostics[0].code
        == "validation.record_missing"
    )

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
    assert (
        missing_run_dir_cycle.coder.validation.diagnostics[0].code
        == "validation.run_dir_missing"
    )


def test_validation_passed_exposes_details_command_for_modal_drilldown() -> None:
    # The per-cycle validation modal needs `run_dir` to fetch JUnit evidence
    # via the existing /api/dialog/validation-failure endpoint. Failed cycles
    # have always exposed it; green cycles must too so the modal works on
    # both outcomes.
    started, completed, *_ = _complete_issue_events()
    passed_event = _event(
        "validation.passed",
        event_id="validation-passed-with-run-dir",
        timestamp="2026-04-21T12:12:00Z",
        run_dir="/tmp/run-1",
        summary="pytest passed",
        artifacts=[_artifact("validation", "/tmp/run-1/validation.json")],
    )
    cycle = project_issue_lifecycle(
        issue_number=5723,
        title="Timeline regression",
        events=[started, completed, passed_event],
        cycles=[{"cycle": 1, "events": [started, completed, passed_event]}],
    ).cycles[0]
    assert isinstance(cycle.coder, CompletedCodingAttempt)
    assert isinstance(cycle.coder.validation, ValidationPassed)
    assert cycle.coder.validation.details_command is not None
    assert cycle.coder.validation.details_command.issue_number == 5723
    assert cycle.coder.validation.details_command.run_dir == "/tmp/run-1"


def test_validation_passed_without_run_dir_projects_missing_evidence() -> None:
    # A passed validation event with no run_dir has no way to surface
    # JUnit evidence in the per-cycle modal, so the projection treats it
    # the same as a failed event without run_dir: `ValidationEvidenceMissing`
    # with a `validation.run_dir_missing` diagnostic. No backcompat for
    # ValidationPassed with a null details_command — the field is required.
    started, completed, *_ = _complete_issue_events()
    passed_event = _event(
        "validation.passed",
        event_id="validation-passed-no-run-dir",
        timestamp="2026-04-21T12:12:00Z",
        summary="pytest passed",
        artifacts=[_artifact("validation", "/tmp/run-1/validation.json")],
    )
    cycle = project_issue_lifecycle(
        issue_number=5723,
        title="Timeline regression",
        events=[started, completed, passed_event],
        cycles=[{"cycle": 1, "events": [started, completed, passed_event]}],
    ).cycles[0]
    assert isinstance(cycle.coder.validation, ValidationEvidenceMissing)
    assert (
        cycle.coder.validation.diagnostics[0].code
        == "validation.run_dir_missing"
    )


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
    complete_events = tuple(
        dict(event, issue_number=5723) for event in _complete_issue_events()
    )
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
    assert lifecycles[0].cycles[0].outcome == "changes_requested"
    assert isinstance(lifecycles[1].cycles[0].coder, RunningCodingAttempt)
    assert lifecycles[1].cycles[0].outcome == "in_progress"


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
    assert isinstance(lifecycle.cycles[0].review, ReviewNotReached)
    assert lifecycle.cycles[0].review.reason == "coding_failed"
    assert isinstance(lifecycle.cycles[1].coder, FailedCodingAttempt)
    assert isinstance(lifecycle.cycles[1].review, ReviewNotReached)
    assert lifecycle.cycles[1].review.reason == "coding_failed"


def test_orphan_rework_completion_tail_merges_with_cached_review_cycle() -> None:
    orchestrator_boundary = _event(
        "issue.labels_changed",
        event_id="pr-pending-removed",
        timestamp="2026-04-30T23:06:10Z",
        logical_run=3,
        logical_cycle=1,
    )
    rework_start = _event(
        "rework.started",
        event_id="rework-start",
        timestamp="2026-04-30T23:06:27Z",
        run_dir="/tmp/rework-run",
        logical_run=4,
        logical_cycle=1,
        rework_cycle=1,
    )
    cached_review_started = _event(
        "review.started",
        event_id="cached-review-start",
        timestamp="2026-04-30T23:14:58Z",
        reviewer_agent="agent:reviewer",
        run_dir="/tmp/review-run",
        logical_run=4,
        logical_cycle=1,
    )
    cached_review_approved = _event(
        "review.approved",
        event_id="cached-review-approved",
        timestamp="2026-04-30T23:14:59Z",
        reviewer_agent="agent:reviewer",
        run_dir="/tmp/review-run",
        logical_run=4,
        logical_cycle=1,
    )
    orphan_completion = _event(
        "session.completed",
        event_id="orphan-rework-completed",
        timestamp="2026-04-30T23:15:10Z",
        agent="agent:coder",
        run_dir="/tmp/rework-run",
        logical_run=4,
        logical_cycle=2,
        rework_cycle=1,
        artifacts=[_artifact("completion_record", "/tmp/rework-run/completion.json")],
    )

    lifecycle = project_issue_lifecycle(
        issue_number=360,
        title="Cached rework review",
        events=[
            orchestrator_boundary,
            rework_start,
            cached_review_started,
            cached_review_approved,
            orphan_completion,
        ],
        cycles=[],
    )

    assert len(lifecycle.cycles) == 1
    assert isinstance(lifecycle.cycles[0].coder, CompletedCodingAttempt)
    assert isinstance(lifecycle.cycles[0].review, ReviewApproved)
    assert lifecycle.cycles[0].outcome == "approved"


def test_rework_completion_tail_with_iteration_start_does_not_merge() -> None:
    rework_start = _event(
        "rework.started",
        event_id="rework-start",
        timestamp="2026-04-30T23:06:27Z",
        logical_run=4,
        logical_cycle=1,
        rework_cycle=1,
    )
    cached_review_approved = _event(
        "review.approved",
        event_id="cached-review-approved",
        timestamp="2026-04-30T23:14:59Z",
        logical_run=4,
        logical_cycle=1,
    )
    next_iteration_start = _event(
        "session.started",
        event_id="next-iteration-start",
        timestamp="2026-04-30T23:15:00Z",
        logical_run=4,
        logical_cycle=2,
        task="rework",
        rework_cycle=1,
    )
    completion = _event(
        "session.completed",
        event_id="next-iteration-complete",
        timestamp="2026-04-30T23:16:00Z",
        logical_run=4,
        logical_cycle=2,
        task="rework",
        rework_cycle=1,
    )

    groups = group_events_by_logical_cycle([
        rework_start,
        cached_review_approved,
        next_iteration_start,
        completion,
    ])

    assert [(group.logical_run, group.logical_cycle) for group in groups] == [
        (4, 1),
        (4, 2),
    ]


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

    with pytest.raises(
        LifecycleProjectionError, match="must not mix logical cycle fields"
    ):
        project_issue_lifecycle(
            issue_number=5723,
            title="Timeline regression",
            events=[legacy_event, logical_event],
            cycles=[],
        )

    with pytest.raises(
        LifecycleProjectionError, match="must not mix logical cycle fields"
    ):
        project_issue_lifecycle(
            issue_number=5723,
            title="Timeline regression",
            events=[logical_event, legacy_event],
            cycles=[],
        )


@pytest.mark.parametrize(
    "case",
    _lifecycle_state_matrix_cases(),
    ids=lambda case: str(case["name"]),
)
def test_lifecycle_projection_state_matrix_preserves_distinct_public_states(
    case: dict[str, object],
) -> None:
    cycle = _project_first_issue_cycle(
        case["events"],
        review_required=bool(case.get("review_required", False)),
    )
    expected = case["expected"]
    expected_coder_type, expected_review_type = case["expected_types"]

    assert isinstance(cycle.coder, expected_coder_type)
    assert isinstance(cycle.review, expected_review_type)
    assert cycle.coder.kind == expected["coder_kind"]
    assert cycle.review.kind == expected["review_kind"]
    assert _coding_validation_kind(cycle) == expected["validation_kind"]
    assert cycle.outcome == expected["outcome"]

    if "coder_commands" in expected:
        assert _stage_command_kinds(cycle.coder) == expected["coder_commands"]
    if "review_commands" in expected:
        assert _stage_command_kinds(cycle.review) == expected["review_commands"]
    if "coder_missing" in expected:
        assert _missing_evidence_names(cycle.coder) == expected["coder_missing"]
    if "review_missing" in expected:
        assert _missing_evidence_names(cycle.review) == expected["review_missing"]
    if "coder_diagnostics" in expected:
        assert _diagnostic_codes(cycle.coder) == expected["coder_diagnostics"]
    if "review_diagnostics" in expected:
        assert _diagnostic_codes(cycle.review) == expected["review_diagnostics"]
    if "review_reason" in expected:
        assert isinstance(cycle.review, ReviewNotReached)
        assert cycle.review.reason == expected["review_reason"]


@pytest.mark.parametrize(
    "issue_events",
    (
        _complete_issue_events(),
        _complete_issue_events()[:3],
    ),
    ids=("completed_with_review", "completed_validated_missing_review"),
)
def test_dashboard_and_e2e_parent_models_project_congruent_issue_lifecycle(
    issue_events: tuple[dict[str, object], ...],
) -> None:
    issue_events = tuple(dict(event, issue_number=5723) for event in issue_events)
    e2e_events = (
        _event(
            "e2e.test_started",
            event_id="test-start",
            timestamp="2026-04-21T12:30:00Z",
            nodeid="tests/e2e/test_timeline.py::test_timeline",
        ),
        _event(
            "e2e.test_completed",
            event_id="test-done",
            timestamp="2026-04-21T12:30:05Z",
            nodeid="tests/e2e/test_timeline.py::test_timeline",
            outcome="passed",
            issue_affordances=[{"issue_number": 5723, "run_id": 88}],
        ),
    )

    dashboard = project_dashboard_lifecycle_container(
        subject_label="Dashboard",
        issue_number=5723,
        title="Timeline regression",
        events=issue_events,
        cycles=[],
        review_required=True,
    )
    e2e_suite = project_e2e_suite_lifecycle_container_for_run(
        run_id=88,
        events=e2e_events,
        agent_events=issue_events,
        subject_label="E2E",
        review_required=True,
    )

    dashboard_issue = dashboard.current.issue_lifecycles[0]
    e2e_issue = e2e_suite.runs[0].e2e_run.linked_issue_lifecycles[0]
    assert _issue_lifecycle_summary(dashboard_issue) == _issue_lifecycle_summary(
        e2e_issue
    )


def test_e2e_projection_builds_passed_and_failed_tests() -> None:
    events = [
        _event(
            "e2e.run_started", event_id="run-start", timestamp="2026-04-21T12:00:00Z"
        ),
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
        _event(
            "e2e.run_finished", event_id="run-finish", timestamp="2026-04-21T12:03:00Z"
        ),
    ]

    iteration = project_e2e_run_iteration(run_id=88, events=events)

    first, second = iteration.e2e_run.tests
    assert isinstance(first, PassedE2ETestExecution)
    assert first.duration_seconds == 5.0
    assert isinstance(second, FailedE2ETestExecution)
    assert second.failure.longrepr == "assert visible_time"


def test_failed_e2e_test_without_longrepr_is_explicit_missing_evidence() -> None:
    events = [
        _event(
            "e2e.run_started", event_id="run-start", timestamp="2026-04-21T12:00:00Z"
        ),
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
        _event(
            "e2e.run_started", event_id="run-start", timestamp="2026-04-21T12:00:00Z"
        ),
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
        _event(
            "e2e.run_started", event_id="run-start", timestamp="2026-04-21T12:00:00Z"
        ),
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
        _event(
            "e2e.run_started", event_id="run-start", timestamp="2026-04-21T12:00:00Z"
        ),
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

    with pytest.raises(
        LifecycleProjectionError, match="linked_issue_lifecycle_missing"
    ):
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
    agent_events = tuple(
        dict(event, issue_number=5723) for event in _complete_issue_events()
    )

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
