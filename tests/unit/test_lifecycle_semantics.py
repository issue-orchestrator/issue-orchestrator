"""Tests for typed lifecycle semantics used by timeline UI projections."""

from __future__ import annotations

from typing import Literal

import pytest
from pydantic import ValidationError

from issue_orchestrator.view_models.lifecycle_semantics import (
    AgentIdentity,
    CompletedCodingAttempt,
    CompletionRecordEvidence,
    DashboardIteration,
    DashboardTimelineContainer,
    E2EFailureDetailsAvailable,
    E2EFailureDetailsMissing,
    E2ERunIteration,
    E2ERunLifecycle,
    E2ESuiteTimelineContainer,
    FailedE2ETestExecution,
    IssueCycle,
    IssueLifecycle,
    LinkedIssueLifecycle,
    MissingCodingEvidence,
    MissingEvidence,
    OpenCompletionRecordCommand,
    OpenIssueTimelineCommand,
    OpenReviewFeedbackCommand,
    OpenSessionRecordingCommand,
    OpenValidationDetailsCommand,
    PassedE2ETestExecution,
    PublishFailedCodingAttempt,
    ReviewApproved,
    ReviewChangesRequested,
    ReviewFailed,
    ReviewNotReached,
    ReviewTranscriptAvailable,
    RunningCodingAttempt,
    SessionRecordingUnavailable,
    ShowEventDetailsCommand,
    TimelineDiagnostic,
    TimelineSubject,
    ValidationEvidenceMissing,
    ValidationFailed,
    ValidationPassed,
    command_kinds,
    validate_lifecycle_container,
)


def _coder() -> AgentIdentity:
    return AgentIdentity(name="codex", role="coder")


def _reviewer() -> AgentIdentity:
    return AgentIdentity(name="reviewer", role="reviewer")


def _details(event_ref: str = "event:coding") -> ShowEventDetailsCommand:
    return ShowEventDetailsCommand(event_ref=event_ref)


def _completion_cmd() -> OpenCompletionRecordCommand:
    return OpenCompletionRecordCommand(path="/runs/issue-1/completion-record.json")


def _session_unavailable(reason: str = "fixture has no recording") -> SessionRecordingUnavailable:
    return SessionRecordingUnavailable(reason=reason)


def _completion() -> CompletionRecordEvidence:
    return CompletionRecordEvidence(
        path="/runs/issue-1/completion-record.json",
        summary="Implemented feature",
    )


def _validated_coding_attempt(
    *,
    issue_number: int = 1,
) -> CompletedCodingAttempt:
    return CompletedCodingAttempt(
        issue_number=issue_number,
        agent=_coder(),
        started_at="2026-04-21T10:00:00Z",
        completed_at="2026-04-21T10:10:00Z",
        completion_record=_completion(),
        validation=ValidationPassed(
            command="pytest tests/unit -q",
            record_path="/runs/issue-1/validation.json",
            details_command=OpenValidationDetailsCommand(
                issue_number=1, run_dir="/runs/issue-1",
            ),
        ),
        session_recording=_session_unavailable(),
        commands=(_details(), _completion_cmd()),
    )


def _issue_lifecycle(issue_number: int = 1) -> IssueLifecycle:
    return IssueLifecycle(
        issue_number=issue_number,
        title=f"Issue {issue_number}",
        cycles=(
            IssueCycle(
                cycle_number=1,
                coder=_validated_coding_attempt(issue_number=issue_number),
                review=ReviewNotReached(reason="not_required"),
                outcome="Completed",
            ),
        ),
    )


def test_completed_coding_attempt_requires_completion_record() -> None:
    payload = {
        "kind": "completed_coding_attempt",
        "issue_number": 1,
        "agent": {"name": "codex", "role": "coder"},
        "started_at": "2026-04-21T10:00:00Z",
        "completed_at": "2026-04-21T10:10:00Z",
        "validation": {
            "kind": "passed",
            "command": "pytest",
            "record_path": "/runs/validation.json",
        },
        "session_recording": {"kind": "unavailable", "reason": "fixture"},
        "commands": [
            {"kind": "show_event_details", "event_ref": "event:coding"},
            {
                "kind": "open_completion_record",
                "path": "/runs/completion-record.json",
            },
        ],
    }

    with pytest.raises(ValidationError, match="completion_record"):
        CompletedCodingAttempt.model_validate(payload)


def test_completed_coding_attempt_requires_completion_command() -> None:
    with pytest.raises(ValidationError, match="open_completion_record"):
        CompletedCodingAttempt(
            issue_number=1,
            agent=_coder(),
            started_at="2026-04-21T10:00:00Z",
            completed_at="2026-04-21T10:10:00Z",
            completion_record=_completion(),
            validation=ValidationPassed(
                command="pytest",
                record_path="/runs/validation.json",
                details_command=OpenValidationDetailsCommand(
                    issue_number=1, run_dir="/runs",
                ),
            ),
            session_recording=_session_unavailable(),
            commands=(_details(),),
        )


def test_completed_coding_attempt_exposes_derived_capabilities() -> None:
    attempt = _validated_coding_attempt()

    assert attempt.has_validated_output() is True
    assert attempt.can_open_validation_details() is False
    assert attempt.can_open_session_recording() is False
    assert command_kinds(attempt.commands) == (
        "show_event_details",
        "open_completion_record",
    )


def test_validation_failed_requires_details_command() -> None:
    attempt = CompletedCodingAttempt(
        issue_number=1,
        agent=_coder(),
        started_at="2026-04-21T10:00:00Z",
        completed_at="2026-04-21T10:10:00Z",
        completion_record=_completion(),
        validation=ValidationFailed(
            command="pytest",
            record_path="/runs/validation.json",
            failure_summary="1 failed",
            details_command=OpenValidationDetailsCommand(
                issue_number=1,
                run_dir="/runs/issue-1",
            ),
        ),
        session_recording=_session_unavailable(),
        commands=(_details(), _completion_cmd()),
    )

    assert attempt.has_validated_output() is False
    assert attempt.can_open_validation_details() is True


def test_validation_missing_evidence_requires_diagnostic() -> None:
    with pytest.raises(ValidationError, match="requires a diagnostic"):
        ValidationEvidenceMissing(diagnostics=())


def test_missing_coding_evidence_is_explicit_state_not_completed_attempt() -> None:
    missing = MissingCodingEvidence(
        issue_number=1,
        expected_state="completed",
        observed_at="2026-04-21T10:10:00Z",
        missing=(
            MissingEvidence(
                evidence="completion_record",
                reason="completion event observed but record not found",
            ),
        ),
        diagnostics=(
            TimelineDiagnostic(
                code="coding.completion_record_missing",
                message="Completion record missing for completed coding attempt",
                severity="error",
            ),
        ),
        commands=(_details(),),
    )

    assert missing.expected_state == "completed"
    assert missing.missing[0].evidence == "completion_record"


def test_missing_coding_evidence_requires_diagnostics_and_missing_items() -> None:
    with pytest.raises(ValidationError, match="at least one missing item"):
        MissingCodingEvidence(
            issue_number=1,
            expected_state="completed",
            observed_at="2026-04-21T10:10:00Z",
            missing=(),
            diagnostics=(
                TimelineDiagnostic(code="x", message="missing", severity="error"),
            ),
            commands=(_details(),),
        )


def test_issue_cycle_requires_coder_and_review_stage() -> None:
    with pytest.raises(ValidationError, match="review"):
        IssueCycle.model_validate(
            {
                "cycle_number": 1,
                "coder": _validated_coding_attempt().model_dump(mode="json"),
                "outcome": "Completed",
            }
        )


def test_issue_cycle_rejects_incoherent_coder_review_combinations() -> None:
    running = RunningCodingAttempt(
        issue_number=1,
        agent=_coder(),
        started_at="2026-04-21T10:00:00Z",
        session_recording=_session_unavailable(),
        commands=(_details(),),
    )
    terminal_review = ReviewChangesRequested(
        reviewer=_reviewer(),
        started_at="2026-04-21T10:12:00Z",
        completed_at="2026-04-21T10:14:00Z",
        feedback_summary="Please add tests",
        session_recording=_session_unavailable(),
        commands=(
            _details("event:review"),
            OpenReviewFeedbackCommand(issue_number=1, event_ref="event:review"),
        ),
    )

    with pytest.raises(ValidationError, match="coding_in_progress"):
        IssueCycle(
            cycle_number=1,
            coder=running,
            review=terminal_review,
            outcome="changes_requested",
        )


def test_issue_cycle_requires_publish_failure_review_reason() -> None:
    publish_failed = PublishFailedCodingAttempt(
        issue_number=1,
        agent=_coder(),
        started_at="2026-04-21T10:00:00Z",
        completed_at="2026-04-21T10:10:00Z",
        publish_failed_at="2026-04-21T10:12:00Z",
        reason="Push rejected",
        completion_record=_completion(),
        validation=ValidationPassed(
            command="pytest",
            record_path="/runs/validation.json",
            details_command=OpenValidationDetailsCommand(
                issue_number=1, run_dir="/runs",
            ),
        ),
        session_recording=_session_unavailable(),
        diagnostics=(
            TimelineDiagnostic(
                code="publish.failed",
                message="Push rejected",
                severity="error",
            ),
        ),
        commands=(_details(), _completion_cmd()),
    )

    with pytest.raises(ValidationError, match="publish_failed"):
        IssueCycle(
            cycle_number=1,
            coder=publish_failed,
            review=ReviewNotReached(reason="coding_failed"),
            outcome="publish_failed",
        )

    cycle = IssueCycle(
        cycle_number=1,
        coder=publish_failed,
        review=ReviewNotReached(reason="publish_failed"),
        outcome="publish_failed",
    )

    assert cycle.coder.kind == "publish_failed_coding_attempt"


def test_issue_cycle_allows_missing_coding_evidence_with_review_evidence() -> None:
    missing_coder = MissingCodingEvidence(
        issue_number=1,
        expected_state="completed",
        observed_at="2026-04-21T10:10:00Z",
        missing=(
            MissingEvidence(
                evidence="completion_record",
                reason="completion event observed but record not found",
            ),
        ),
        diagnostics=(
            TimelineDiagnostic(
                code="coding.completion_record_missing",
                message="Completion record missing for completed coding attempt",
                severity="error",
            ),
        ),
        commands=(_details(),),
    )
    review = ReviewApproved(
        reviewer=_reviewer(),
        started_at="2026-04-21T10:12:00Z",
        completed_at="2026-04-21T10:14:00Z",
        session_recording=_session_unavailable(),
        transcript=ReviewTranscriptAvailable(),
        commands=(_details("event:review"),),
    )

    cycle = IssueCycle(
        cycle_number=1,
        coder=missing_coder,
        review=review,
        outcome="approved",
    )

    assert cycle.coder.kind == "missing_coding_evidence"
    assert cycle.review.kind == "review_approved"


def test_terminal_lifecycle_states_reject_inverted_chronology() -> None:
    with pytest.raises(ValidationError, match="started_at"):
        CompletedCodingAttempt(
            issue_number=1,
            agent=_coder(),
            started_at="2026-04-21T10:10:00Z",
            completed_at="2026-04-21T10:00:00Z",
            completion_record=_completion(),
            validation=ValidationPassed(
                command="pytest",
                record_path="/runs/validation.json",
                details_command=OpenValidationDetailsCommand(
                    issue_number=1, run_dir="/runs",
                ),
            ),
            session_recording=_session_unavailable(),
            commands=(_details(), _completion_cmd()),
        )

    with pytest.raises(ValidationError, match="started_at"):
        PassedE2ETestExecution(
            nodeid="tests/e2e/test_a.py::test_a",
            started_at="2026-04-21T11:01:00Z",
            completed_at="2026-04-21T11:00:00Z",
            commands=(_details("event:test"),),
        )


def test_review_changes_requested_requires_feedback_command() -> None:
    with pytest.raises(ValidationError, match="open_review_feedback"):
        ReviewChangesRequested(
            reviewer=_reviewer(),
            started_at="2026-04-21T10:12:00Z",
            completed_at="2026-04-21T10:14:00Z",
            feedback_summary="Please add tests",
            session_recording=_session_unavailable(),
            commands=(_details("event:review"),),
        )

    review = ReviewChangesRequested(
        reviewer=_reviewer(),
        started_at="2026-04-21T10:12:00Z",
        completed_at="2026-04-21T10:14:00Z",
        feedback_summary="Please add tests",
        session_recording=_session_unavailable(),
        commands=(
            _details("event:review"),
            OpenReviewFeedbackCommand(issue_number=1, event_ref="event:review"),
        ),
    )

    assert command_kinds(review.commands) == (
        "show_event_details",
        "open_review_feedback",
    )


def test_review_approved_uses_tagged_transcript_evidence() -> None:
    review = ReviewApproved(
        reviewer=_reviewer(),
        started_at="2026-04-21T10:12:00Z",
        completed_at="2026-04-21T10:14:00Z",
        session_recording=_session_unavailable(),
        transcript=ReviewTranscriptAvailable(),
        commands=(_details("event:review"),),
    )

    payload = review.model_dump(mode="json")

    assert payload["transcript"]["kind"] == "available"


def test_review_failed_is_distinct_terminal_state() -> None:
    review = ReviewFailed(
        reviewer=_reviewer(),
        started_at="2026-04-21T10:12:00Z",
        failed_at="2026-04-21T10:14:00Z",
        reason="review exchange crashed",
        session_recording=_session_unavailable(),
        commands=(_details("event:review-failed"),),
    )

    assert review.kind == "review_failed"
    assert review.reason == "review exchange crashed"


def test_dashboard_container_iterates_singleton_current_iteration() -> None:
    iteration = DashboardIteration(
        subject=TimelineSubject(
            kind="dashboard",
            id="current",
            label="Current Dashboard",
        ),
        issue_lifecycles=(_issue_lifecycle(),),
    )
    container = DashboardTimelineContainer(
        subject=TimelineSubject(kind="dashboard", id="dashboard", label="Dashboard"),
        current=iteration,
    )

    assert list(container.iter_iterations()) == [iteration]
    assert validate_lifecycle_container(container) == ()


def test_containers_reject_mismatched_subject_kinds() -> None:
    iteration = DashboardIteration(
        subject=TimelineSubject(kind="dashboard", id="current", label="Dashboard"),
        issue_lifecycles=(_issue_lifecycle(),),
    )
    with pytest.raises(ValidationError, match="dashboard container subject"):
        DashboardTimelineContainer(
            subject=TimelineSubject(kind="e2e_suite", id="suite", label="E2E Suite"),
            current=iteration,
        )

    with pytest.raises(ValidationError, match="E2E suite container subject"):
        E2ESuiteTimelineContainer(
            subject=TimelineSubject(kind="dashboard", id="current", label="Dashboard"),
            runs=(_e2e_iteration(run_id=88, nodeid="tests/e2e/test_a.py::test_a"),),
        )


def test_e2e_container_iterates_multiple_run_iterations() -> None:
    run_1 = _e2e_iteration(run_id=88, nodeid="tests/e2e/test_a.py::test_a")
    run_2 = _e2e_iteration(run_id=89, nodeid="tests/e2e/test_b.py::test_b")
    container = E2ESuiteTimelineContainer(
        subject=TimelineSubject(kind="e2e_suite", id="suite", label="E2E Suite"),
        runs=(run_1, run_2),
    )

    assert [iteration.e2e_run.run_id for iteration in container.iter_iterations()] == [88, 89]
    assert validate_lifecycle_container(container) == ()


def _e2e_iteration(run_id: int, nodeid: str) -> E2ERunIteration:
    linked_issue = _issue_lifecycle(issue_number=run_id)
    command = OpenIssueTimelineCommand(
        issue_number=run_id,
        scope_kind="e2e_run",
        e2e_run_id=run_id,
    )
    test = PassedE2ETestExecution(
        nodeid=nodeid,
        started_at="2026-04-21T11:00:00Z",
        completed_at="2026-04-21T11:01:00Z",
        linked_issues=(
            LinkedIssueLifecycle(
                issue_number=run_id,
                relationship="exercises",
                command=command,
            ),
        ),
        commands=(_details(f"event:test:{run_id}"),),
    )
    run = E2ERunLifecycle(
        run_id=run_id,
        started_at="2026-04-21T11:00:00Z",
        completed_at="2026-04-21T11:02:00Z",
        tests=(test,),
        linked_issue_lifecycles=(linked_issue,),
    )
    return E2ERunIteration(
        subject=TimelineSubject(kind="e2e_run", id=str(run_id), label=f"Run #{run_id}"),
        e2e_run=run,
    )


def test_e2e_issue_timeline_command_requires_scope_run_id() -> None:
    with pytest.raises(ValidationError, match="requires e2e_run_id"):
        OpenIssueTimelineCommand(issue_number=1, scope_kind="e2e_run")


def test_linked_issue_command_must_target_same_issue() -> None:
    with pytest.raises(ValidationError, match="target linked issue"):
        LinkedIssueLifecycle(
            issue_number=1,
            relationship="exercises",
            command=OpenIssueTimelineCommand(
                issue_number=2,
                scope_kind="dashboard",
            ),
        )


@pytest.mark.parametrize(
    "relationship",
    ["exercises", "discovered", "failed_with", "validates"],
)
def test_linked_issue_relationship_values_are_supported(
    relationship: Literal["exercises", "discovered", "failed_with", "validates"],
) -> None:
    linked = LinkedIssueLifecycle(
        issue_number=1,
        relationship=relationship,
        command=OpenIssueTimelineCommand(
            issue_number=1,
            scope_kind="dashboard",
        ),
    )

    assert linked.relationship == relationship


def test_aggregate_validator_flags_e2e_link_without_issue_lifecycle() -> None:
    test = PassedE2ETestExecution(
        nodeid="tests/e2e/test_a.py::test_a",
        started_at="2026-04-21T11:00:00Z",
        completed_at="2026-04-21T11:01:00Z",
        linked_issues=(
            LinkedIssueLifecycle(
                issue_number=123,
                relationship="exercises",
                command=OpenIssueTimelineCommand(
                    issue_number=123,
                    scope_kind="e2e_run",
                    e2e_run_id=88,
                ),
            ),
        ),
        commands=(_details("event:test"),),
    )
    run = E2ERunLifecycle(
        run_id=88,
        started_at="2026-04-21T11:00:00Z",
        completed_at="2026-04-21T11:02:00Z",
        tests=(test,),
        linked_issue_lifecycles=(),
    )
    container = E2ESuiteTimelineContainer(
        subject=TimelineSubject(kind="e2e_suite", id="suite", label="E2E Suite"),
        runs=(
            E2ERunIteration(
                subject=TimelineSubject(kind="e2e_run", id="88", label="Run #88"),
                e2e_run=run,
            ),
        ),
    )

    diagnostics = validate_lifecycle_container(container)

    assert len(diagnostics) == 1
    assert diagnostics[0].code == "e2e.linked_issue_lifecycle_missing"
    assert diagnostics[0].severity == "error"


def test_failed_e2e_test_requires_failure_evidence_or_diagnostic() -> None:
    with pytest.raises(ValidationError, match="missing failure details requires diagnostics"):
        FailedE2ETestExecution(
            nodeid="tests/e2e/test_a.py::test_a",
            started_at="2026-04-21T11:00:00Z",
            completed_at="2026-04-21T11:01:00Z",
            failure=E2EFailureDetailsMissing(diagnostics=()),
            commands=(_details("event:test"),),
        )

    failed = FailedE2ETestExecution(
        nodeid="tests/e2e/test_a.py::test_a",
        started_at="2026-04-21T11:00:00Z",
        completed_at="2026-04-21T11:01:00Z",
        failure=E2EFailureDetailsAvailable(longrepr="AssertionError: nope"),
        commands=(_details("event:test"),),
    )

    assert failed.failure.longrepr == "AssertionError: nope"


def test_serialized_model_preserves_discriminator_kinds() -> None:
    attempt = _validated_coding_attempt()

    payload = attempt.model_dump(mode="json")

    assert payload["kind"] == "completed_coding_attempt"
    assert payload["validation"]["kind"] == "passed"
    assert payload["session_recording"]["kind"] == "unavailable"
    assert [command["kind"] for command in payload["commands"]] == [
        "show_event_details",
        "open_completion_record",
    ]
