"""Semantically rich lifecycle model for timeline UI surfaces.

This module is intentionally stricter than the legacy dict-shaped timeline
payloads.  It models lifecycle protocol states and required evidence before a
browser renderer sees the data, so regressions fail in cheap model/contract
tests instead of late Playwright runs.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Severity = Literal["info", "warning", "error"]
Timestamp = str


class LifecycleBase(BaseModel):
    """Base for strict UI lifecycle contracts."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class TimelineDiagnostic(LifecycleBase):
    code: str
    message: str
    severity: Severity = "warning"
    evidence_ref: str | None = None

    @field_validator("code", "message")
    @classmethod
    def _non_empty_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("diagnostic fields must not be empty")
        return value


class MissingEvidence(LifecycleBase):
    kind: Literal["missing_evidence"] = "missing_evidence"
    evidence: str
    reason: str
    expected_ref: str | None = None

    @field_validator("evidence", "reason")
    @classmethod
    def _non_empty_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("missing evidence fields must not be empty")
        return value


class AgentIdentity(LifecycleBase):
    name: str
    role: Literal["coder", "reviewer", "rework", "validator", "e2e_runner", "orchestrator"]

    @field_validator("name")
    @classmethod
    def _non_empty_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("agent name must not be empty")
        return value


class TimelineSubject(LifecycleBase):
    kind: Literal["dashboard", "repo", "issue", "e2e_suite", "e2e_run", "e2e_test"]
    id: str
    label: str
    status: str | None = None
    outcome: str | None = None

    @field_validator("id", "label")
    @classmethod
    def _non_empty_identity(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("subject identity fields must not be empty")
        return value


class ShowEventDetailsCommand(LifecycleBase):
    kind: Literal["show_event_details"] = "show_event_details"
    label: str = "Event Details"
    event_ref: str


class OpenSessionRecordingCommand(LifecycleBase):
    kind: Literal["open_session_recording"] = "open_session_recording"
    label: str = "Session Recording"
    issue_number: int
    run_dir: str
    session_role: str | None = None
    round_index: int | None = None


class OpenValidationDetailsCommand(LifecycleBase):
    kind: Literal["open_validation_details"] = "open_validation_details"
    label: str = "Validation Details"
    issue_number: int
    run_dir: str


class OpenCompletionRecordCommand(LifecycleBase):
    kind: Literal["open_completion_record"] = "open_completion_record"
    label: str = "Completion Record"
    path: str


class OpenReviewFeedbackCommand(LifecycleBase):
    kind: Literal["open_review_feedback"] = "open_review_feedback"
    label: str = "Review Feedback"
    issue_number: int
    event_ref: str | None = None


class OpenIssueTimelineCommand(LifecycleBase):
    kind: Literal["open_issue_timeline"] = "open_issue_timeline"
    label: str = "Issue Timeline"
    issue_number: int
    scope_kind: Literal["dashboard", "e2e_run"]
    e2e_run_id: int | None = None

    @model_validator(mode="after")
    def _require_e2e_scope_id(self) -> "OpenIssueTimelineCommand":
        if self.scope_kind == "e2e_run" and self.e2e_run_id is None:
            raise ValueError("e2e issue timeline command requires e2e_run_id")
        return self


TimelineCommand = Annotated[
    ShowEventDetailsCommand
    | OpenSessionRecordingCommand
    | OpenValidationDetailsCommand
    | OpenCompletionRecordCommand
    | OpenReviewFeedbackCommand
    | OpenIssueTimelineCommand,
    Field(discriminator="kind"),
]


class CompletionRecordEvidence(LifecycleBase):
    kind: Literal["available"] = "available"
    path: str
    summary: str | None = None

    @field_validator("path")
    @classmethod
    def _non_empty_path(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("completion record path must not be empty")
        return value


class SessionRecordingAvailable(LifecycleBase):
    kind: Literal["available"] = "available"
    run_dir: str
    recording_path: str
    command: OpenSessionRecordingCommand


class SessionRecordingUnavailable(LifecycleBase):
    kind: Literal["unavailable"] = "unavailable"
    reason: str
    diagnostics: tuple[TimelineDiagnostic, ...] = ()


SessionRecordingEvidence = Annotated[
    SessionRecordingAvailable | SessionRecordingUnavailable,
    Field(discriminator="kind"),
]


class ValidationPassed(LifecycleBase):
    kind: Literal["passed"] = "passed"
    command: str
    record_path: str


class ValidationFailed(LifecycleBase):
    kind: Literal["failed"] = "failed"
    command: str
    record_path: str
    failure_summary: str
    details_command: OpenValidationDetailsCommand


class ValidationNotRun(LifecycleBase):
    kind: Literal["not_run"] = "not_run"
    reason: Literal["coding_in_progress", "validation_disabled", "not_required"]


class ValidationEvidenceMissing(LifecycleBase):
    kind: Literal["missing_evidence"] = "missing_evidence"
    expected_record_path: str | None = None
    diagnostics: tuple[TimelineDiagnostic, ...]

    @model_validator(mode="after")
    def _require_diagnostic(self) -> "ValidationEvidenceMissing":
        if not self.diagnostics:
            raise ValueError("missing validation evidence requires a diagnostic")
        return self


ValidationOutcome = Annotated[
    ValidationPassed | ValidationFailed | ValidationNotRun | ValidationEvidenceMissing,
    Field(discriminator="kind"),
]


class CodingOutputs(LifecycleBase):
    worktree_path: str | None = None
    pull_request_url: str | None = None


class RunningCodingAttempt(LifecycleBase):
    kind: Literal["running_coding_attempt"] = "running_coding_attempt"
    issue_number: int
    agent: AgentIdentity
    started_at: Timestamp
    session_recording: SessionRecordingEvidence
    commands: tuple[TimelineCommand, ...]

    @model_validator(mode="after")
    def _require_event_details(self) -> "RunningCodingAttempt":
        _ensure_event_details(self.commands)
        return self


class CompletedCodingAttempt(LifecycleBase):
    kind: Literal["completed_coding_attempt"] = "completed_coding_attempt"
    issue_number: int
    agent: AgentIdentity
    started_at: Timestamp
    completed_at: Timestamp
    completion_record: CompletionRecordEvidence
    validation: ValidationOutcome
    session_recording: SessionRecordingEvidence
    outputs: CodingOutputs = Field(default_factory=CodingOutputs)
    commands: tuple[TimelineCommand, ...]

    @model_validator(mode="after")
    def _require_required_commands(self) -> "CompletedCodingAttempt":
        _ensure_event_details(self.commands)
        _ensure_command_kind(self.commands, "open_completion_record")
        return self

    def has_validated_output(self) -> bool:
        return isinstance(self.validation, ValidationPassed)

    def can_open_session_recording(self) -> bool:
        return isinstance(self.session_recording, SessionRecordingAvailable)

    def can_open_validation_details(self) -> bool:
        return isinstance(self.validation, ValidationFailed)


class BlockedCodingAttempt(LifecycleBase):
    kind: Literal["blocked_coding_attempt"] = "blocked_coding_attempt"
    issue_number: int
    agent: AgentIdentity
    started_at: Timestamp | None = None
    blocked_at: Timestamp
    reason: str
    session_recording: SessionRecordingEvidence
    diagnostics: tuple[TimelineDiagnostic, ...] = ()
    commands: tuple[TimelineCommand, ...]

    @model_validator(mode="after")
    def _require_event_details(self) -> "BlockedCodingAttempt":
        _ensure_event_details(self.commands)
        return self


class FailedCodingAttempt(LifecycleBase):
    kind: Literal["failed_coding_attempt"] = "failed_coding_attempt"
    issue_number: int
    agent: AgentIdentity | None = None
    started_at: Timestamp | None = None
    failed_at: Timestamp
    reason: str
    session_recording: SessionRecordingEvidence
    diagnostics: tuple[TimelineDiagnostic, ...] = ()
    commands: tuple[TimelineCommand, ...]

    @model_validator(mode="after")
    def _require_event_details(self) -> "FailedCodingAttempt":
        _ensure_event_details(self.commands)
        return self


class MissingCodingEvidence(LifecycleBase):
    kind: Literal["missing_coding_evidence"] = "missing_coding_evidence"
    issue_number: int
    expected_state: Literal["completed", "running", "blocked", "failed"]
    observed_at: Timestamp
    missing: tuple[MissingEvidence, ...]
    diagnostics: tuple[TimelineDiagnostic, ...]
    commands: tuple[TimelineCommand, ...]

    @model_validator(mode="after")
    def _require_missing_evidence_and_details(self) -> "MissingCodingEvidence":
        if not self.missing:
            raise ValueError("missing coding evidence requires at least one missing item")
        if not self.diagnostics:
            raise ValueError("missing coding evidence requires diagnostics")
        _ensure_event_details(self.commands)
        return self


CodingAttempt = Annotated[
    RunningCodingAttempt
    | CompletedCodingAttempt
    | BlockedCodingAttempt
    | FailedCodingAttempt
    | MissingCodingEvidence,
    Field(discriminator="kind"),
]


class ReviewNotReached(LifecycleBase):
    kind: Literal["review_not_reached"] = "review_not_reached"
    reason: Literal["coding_in_progress", "coding_failed", "validation_failed", "not_required"]


class ReviewSkipped(LifecycleBase):
    kind: Literal["review_skipped"] = "review_skipped"
    reason: str


class ReviewRunning(LifecycleBase):
    kind: Literal["review_running"] = "review_running"
    reviewer: AgentIdentity
    started_at: Timestamp
    session_recording: SessionRecordingEvidence
    commands: tuple[TimelineCommand, ...]

    @model_validator(mode="after")
    def _require_event_details(self) -> "ReviewRunning":
        _ensure_event_details(self.commands)
        return self


class ReviewApproved(LifecycleBase):
    kind: Literal["review_approved"] = "review_approved"
    reviewer: AgentIdentity
    started_at: Timestamp
    completed_at: Timestamp
    session_recording: SessionRecordingEvidence
    transcript_available: bool
    commands: tuple[TimelineCommand, ...]

    @model_validator(mode="after")
    def _require_event_details(self) -> "ReviewApproved":
        _ensure_event_details(self.commands)
        return self


class ReviewChangesRequested(LifecycleBase):
    kind: Literal["review_changes_requested"] = "review_changes_requested"
    reviewer: AgentIdentity
    started_at: Timestamp
    completed_at: Timestamp
    feedback_summary: str
    session_recording: SessionRecordingEvidence
    commands: tuple[TimelineCommand, ...]

    @model_validator(mode="after")
    def _require_feedback_and_details(self) -> "ReviewChangesRequested":
        if not self.feedback_summary.strip():
            raise ValueError("changes-requested review requires feedback summary")
        _ensure_event_details(self.commands)
        _ensure_command_kind(self.commands, "open_review_feedback")
        return self


class MissingReviewEvidence(LifecycleBase):
    kind: Literal["missing_review_evidence"] = "missing_review_evidence"
    expected_state: Literal["approved", "changes_requested", "running"]
    observed_at: Timestamp
    missing: tuple[MissingEvidence, ...]
    diagnostics: tuple[TimelineDiagnostic, ...]
    commands: tuple[TimelineCommand, ...]

    @model_validator(mode="after")
    def _require_missing_evidence_and_details(self) -> "MissingReviewEvidence":
        if not self.missing:
            raise ValueError("missing review evidence requires at least one missing item")
        if not self.diagnostics:
            raise ValueError("missing review evidence requires diagnostics")
        _ensure_event_details(self.commands)
        return self


ReviewStage = Annotated[
    ReviewNotReached
    | ReviewSkipped
    | ReviewRunning
    | ReviewApproved
    | ReviewChangesRequested
    | MissingReviewEvidence,
    Field(discriminator="kind"),
]


class IssueCycle(LifecycleBase):
    cycle_number: int
    coder: CodingAttempt
    review: ReviewStage
    outcome: str
    diagnostics: tuple[TimelineDiagnostic, ...] = ()

    @field_validator("cycle_number")
    @classmethod
    def _positive_cycle_number(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("cycle_number must be positive")
        return value


class IssueLifecycle(LifecycleBase):
    issue_number: int
    title: str
    cycles: tuple[IssueCycle, ...]
    diagnostics: tuple[TimelineDiagnostic, ...] = ()

    @model_validator(mode="after")
    def _require_cycle(self) -> "IssueLifecycle":
        if not self.cycles:
            raise ValueError("issue lifecycle requires at least one cycle")
        return self


class E2EFailureDetailsAvailable(LifecycleBase):
    kind: Literal["available"] = "available"
    longrepr: str

    @field_validator("longrepr")
    @classmethod
    def _non_empty_longrepr(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("longrepr must not be empty")
        return value


class E2EFailureDetailsMissing(LifecycleBase):
    kind: Literal["missing_evidence"] = "missing_evidence"
    diagnostics: tuple[TimelineDiagnostic, ...]

    @model_validator(mode="after")
    def _require_diagnostic(self) -> "E2EFailureDetailsMissing":
        if not self.diagnostics:
            raise ValueError("missing failure details requires diagnostics")
        return self


E2EFailureEvidence = Annotated[
    E2EFailureDetailsAvailable | E2EFailureDetailsMissing,
    Field(discriminator="kind"),
]


class LinkedIssueLifecycle(LifecycleBase):
    issue_number: int
    relationship: Literal["exercises", "discovered", "failed_with", "validates"]
    command: OpenIssueTimelineCommand

    @model_validator(mode="after")
    def _command_targets_issue(self) -> "LinkedIssueLifecycle":
        if self.command.issue_number != self.issue_number:
            raise ValueError("linked issue command must target linked issue")
        return self


class PassedE2ETestExecution(LifecycleBase):
    kind: Literal["passed_e2e_test"] = "passed_e2e_test"
    nodeid: str
    started_at: Timestamp
    completed_at: Timestamp
    duration_seconds: float | None = None
    linked_issues: tuple[LinkedIssueLifecycle, ...] = ()
    commands: tuple[TimelineCommand, ...]

    @model_validator(mode="after")
    def _require_event_details(self) -> "PassedE2ETestExecution":
        _ensure_event_details(self.commands)
        return self


class FailedE2ETestExecution(LifecycleBase):
    kind: Literal["failed_e2e_test"] = "failed_e2e_test"
    nodeid: str
    started_at: Timestamp
    completed_at: Timestamp
    duration_seconds: float | None = None
    failure: E2EFailureEvidence
    linked_issues: tuple[LinkedIssueLifecycle, ...] = ()
    commands: tuple[TimelineCommand, ...]

    @model_validator(mode="after")
    def _require_event_details(self) -> "FailedE2ETestExecution":
        _ensure_event_details(self.commands)
        return self


class RunningE2ETestExecution(LifecycleBase):
    kind: Literal["running_e2e_test"] = "running_e2e_test"
    nodeid: str
    started_at: Timestamp
    linked_issues: tuple[LinkedIssueLifecycle, ...] = ()
    commands: tuple[TimelineCommand, ...]

    @model_validator(mode="after")
    def _require_event_details(self) -> "RunningE2ETestExecution":
        _ensure_event_details(self.commands)
        return self


class MissingE2ETestEvidence(LifecycleBase):
    kind: Literal["missing_e2e_test_evidence"] = "missing_e2e_test_evidence"
    nodeid: str
    observed_at: Timestamp
    missing: tuple[MissingEvidence, ...]
    diagnostics: tuple[TimelineDiagnostic, ...]
    commands: tuple[TimelineCommand, ...]

    @model_validator(mode="after")
    def _require_missing_evidence_and_details(self) -> "MissingE2ETestEvidence":
        if not self.missing:
            raise ValueError("missing E2E test evidence requires at least one missing item")
        if not self.diagnostics:
            raise ValueError("missing E2E test evidence requires diagnostics")
        _ensure_event_details(self.commands)
        return self


E2ETestExecution = Annotated[
    PassedE2ETestExecution
    | FailedE2ETestExecution
    | RunningE2ETestExecution
    | MissingE2ETestEvidence,
    Field(discriminator="kind"),
]


class E2ERunLifecycle(LifecycleBase):
    run_id: int
    started_at: Timestamp
    completed_at: Timestamp | None = None
    tests: tuple[E2ETestExecution, ...]
    linked_issue_lifecycles: tuple[IssueLifecycle, ...] = ()
    diagnostics: tuple[TimelineDiagnostic, ...] = ()

    @model_validator(mode="after")
    def _require_tests(self) -> "E2ERunLifecycle":
        if not self.tests:
            raise ValueError("E2E run lifecycle requires at least one test execution")
        return self


class DashboardIteration(LifecycleBase):
    kind: Literal["dashboard_current"] = "dashboard_current"
    subject: TimelineSubject
    issue_lifecycles: tuple[IssueLifecycle, ...]
    diagnostics: tuple[TimelineDiagnostic, ...] = ()


class E2ERunIteration(LifecycleBase):
    kind: Literal["e2e_run"] = "e2e_run"
    subject: TimelineSubject
    e2e_run: E2ERunLifecycle
    diagnostics: tuple[TimelineDiagnostic, ...] = ()

    @model_validator(mode="after")
    def _subject_matches_run(self) -> "E2ERunIteration":
        if self.subject.kind != "e2e_run" or self.subject.id != str(self.e2e_run.run_id):
            raise ValueError("E2E run iteration subject must match run lifecycle")
        return self


TimelineIteration = DashboardIteration | E2ERunIteration


class DashboardTimelineContainer(LifecycleBase):
    kind: Literal["dashboard"] = "dashboard"
    subject: TimelineSubject
    current: DashboardIteration

    def iter_iterations(self) -> Iterator[DashboardIteration]:
        yield self.current


class E2ESuiteTimelineContainer(LifecycleBase):
    kind: Literal["e2e_suite"] = "e2e_suite"
    subject: TimelineSubject
    runs: tuple[E2ERunIteration, ...]

    @model_validator(mode="after")
    def _require_runs(self) -> "E2ESuiteTimelineContainer":
        if not self.runs:
            raise ValueError("E2E suite container requires at least one run iteration")
        return self

    def iter_iterations(self) -> Iterator[E2ERunIteration]:
        yield from self.runs


TimelineContainer = DashboardTimelineContainer | E2ESuiteTimelineContainer


def validate_lifecycle_container(container: TimelineContainer) -> tuple[TimelineDiagnostic, ...]:
    """Run aggregate cross-object invariants not handled by constructors."""
    diagnostics: list[TimelineDiagnostic] = []
    for iteration in container.iter_iterations():
        if isinstance(iteration, DashboardIteration):
            diagnostics.extend(_validate_issue_lifecycles(iteration.issue_lifecycles))
        else:
            diagnostics.extend(_validate_e2e_run(iteration.e2e_run))
    return tuple(diagnostics)


def _validate_issue_lifecycles(
    lifecycles: tuple[IssueLifecycle, ...],
) -> list[TimelineDiagnostic]:
    diagnostics: list[TimelineDiagnostic] = []
    for lifecycle in lifecycles:
        for cycle in lifecycle.cycles:
            if (
                isinstance(cycle.coder, CompletedCodingAttempt)
                and cycle.coder.has_validated_output()
                and isinstance(cycle.review, ReviewNotReached)
                and cycle.review.reason == "not_required"
            ):
                continue
            if (
                isinstance(cycle.coder, CompletedCodingAttempt)
                and cycle.coder.has_validated_output()
                and isinstance(cycle.review, ReviewNotReached)
            ):
                diagnostics.append(
                    TimelineDiagnostic(
                        code="review.not_reached_after_validated_coding",
                        message=(
                            f"Issue #{lifecycle.issue_number} cycle {cycle.cycle_number} "
                            "has validated coding output but review has not been reached"
                        ),
                        severity="info",
                    )
                )
    return diagnostics


def _validate_e2e_run(run: E2ERunLifecycle) -> list[TimelineDiagnostic]:
    diagnostics: list[TimelineDiagnostic] = []
    lifecycle_issue_numbers = {
        lifecycle.issue_number for lifecycle in run.linked_issue_lifecycles
    }
    for test in run.tests:
        linked_issues = getattr(test, "linked_issues", ())
        for linked in linked_issues:
            if linked.issue_number not in lifecycle_issue_numbers:
                diagnostics.append(
                    TimelineDiagnostic(
                        code="e2e.linked_issue_lifecycle_missing",
                        message=(
                            f"E2E run {run.run_id} links test issue "
                            f"#{linked.issue_number} without an issue lifecycle"
                        ),
                        severity="error",
                    )
                )
    return diagnostics


def _ensure_event_details(commands: tuple[TimelineCommand, ...]) -> None:
    _ensure_command_kind(commands, "show_event_details")


def _ensure_command_kind(commands: tuple[TimelineCommand, ...], kind: str) -> None:
    if not any(getattr(command, "kind", None) == kind for command in commands):
        raise ValueError(f"commands must include {kind}")


def command_kinds(commands: tuple[TimelineCommand, ...]) -> tuple[str, ...]:
    """Return command kinds for high-signal tests and projections."""
    return tuple(str(command.kind) for command in commands)


def model_to_plain_dict(model: LifecycleBase) -> dict[str, Any]:
    """Serialize a lifecycle model using the JSON contract shape."""
    return model.model_dump(mode="json")


__all__ = [
    "AgentIdentity",
    "BlockedCodingAttempt",
    "CodingAttempt",
    "CodingOutputs",
    "CompletedCodingAttempt",
    "CompletionRecordEvidence",
    "DashboardIteration",
    "DashboardTimelineContainer",
    "E2EFailureDetailsAvailable",
    "E2EFailureDetailsMissing",
    "E2EFailureEvidence",
    "E2ERunIteration",
    "E2ERunLifecycle",
    "E2ESuiteTimelineContainer",
    "E2ETestExecution",
    "FailedCodingAttempt",
    "FailedE2ETestExecution",
    "IssueCycle",
    "IssueLifecycle",
    "LinkedIssueLifecycle",
    "MissingCodingEvidence",
    "MissingE2ETestEvidence",
    "MissingEvidence",
    "MissingReviewEvidence",
    "OpenCompletionRecordCommand",
    "OpenIssueTimelineCommand",
    "OpenReviewFeedbackCommand",
    "OpenSessionRecordingCommand",
    "OpenValidationDetailsCommand",
    "PassedE2ETestExecution",
    "ReviewApproved",
    "ReviewChangesRequested",
    "ReviewNotReached",
    "ReviewRunning",
    "ReviewSkipped",
    "ReviewStage",
    "RunningCodingAttempt",
    "RunningE2ETestExecution",
    "SessionRecordingAvailable",
    "SessionRecordingEvidence",
    "SessionRecordingUnavailable",
    "ShowEventDetailsCommand",
    "TimelineCommand",
    "TimelineContainer",
    "TimelineDiagnostic",
    "TimelineIteration",
    "TimelineSubject",
    "ValidationEvidenceMissing",
    "ValidationFailed",
    "ValidationNotRun",
    "ValidationOutcome",
    "ValidationPassed",
    "command_kinds",
    "model_to_plain_dict",
    "validate_lifecycle_container",
]
