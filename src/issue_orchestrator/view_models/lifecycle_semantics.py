"""Semantically rich lifecycle model for timeline UI surfaces.

This module is intentionally stricter than the legacy dict-shaped timeline
payloads.  It models lifecycle protocol states and required evidence before a
browser renderer sees the data, so regressions fail in cheap model/contract
tests instead of late Playwright runs.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
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
    kind: Literal["dashboard", "issue", "e2e_suite", "e2e_run"]
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


class OpenE2ERunCommand(LifecycleBase):
    """Open an E2E run's detail view (issue #6322, PR #6329 review blocker).

    Issued by the dashboard's E2E chip, the issue-row "View" button,
    and any other affordance that navigates the user to a specific
    E2E run.  The frontend dispatcher (``runE2ELifecycleCommand``)
    routes this kind to ``showUnifiedRunView(run_id, options)``.

    Adding this kind to the typed ``TimelineCommand`` union means
    every user-facing "open E2E run" affordance serializes through
    the same Pydantic-validated payload — no more hand-built JSON
    strings in templates or call sites.
    """

    kind: Literal["open_e2e_run"] = "open_e2e_run"
    label: str = "Open E2E Run"
    # Strict-int: reject string/boolean coercion (PR #6329 round-5).
    # JSON Schema's ``{"type": "integer", "minimum": 1}`` does not
    # accept ``"88"`` or ``True``; the canonical Pydantic model
    # must match — without ``strict=True`` Pydantic would coerce
    # both into ``88`` / ``1`` and silently normalize bad payloads.
    run_id: int = Field(..., ge=1, strict=True)
    expand_run_details: bool = False


TimelineCommand = Annotated[
    ShowEventDetailsCommand
    | OpenSessionRecordingCommand
    | OpenValidationDetailsCommand
    | OpenCompletionRecordCommand
    | OpenReviewFeedbackCommand
    | OpenIssueTimelineCommand
    | OpenE2ERunCommand,
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
    # Same shape as ValidationFailed so the per-cycle validation modal can
    # fetch full evidence (JUnit cases, stdout/stderr) for green cycles too.
    # Required: a ValidationPassed without somewhere to find the evidence
    # is unactionable, and we don't keep a backcompat path for older
    # projections — writers always emit run_dir on passed events.
    details_command: OpenValidationDetailsCommand


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
        _ensure_timestamp_order(
            self.started_at,
            self.completed_at,
            "completed coding attempt",
        )
        _ensure_event_details(self.commands)
        _ensure_command_kind(self.commands, "open_completion_record")
        return self

    def has_validated_output(self) -> bool:
        return isinstance(self.validation, ValidationPassed)

    def can_open_session_recording(self) -> bool:
        return isinstance(self.session_recording, SessionRecordingAvailable)

    def can_open_validation_details(self) -> bool:
        return isinstance(self.validation, ValidationFailed)


class PublishFailedCodingAttempt(LifecycleBase):
    kind: Literal["publish_failed_coding_attempt"] = "publish_failed_coding_attempt"
    issue_number: int
    agent: AgentIdentity
    started_at: Timestamp
    completed_at: Timestamp
    publish_failed_at: Timestamp
    reason: str
    completion_record: CompletionRecordEvidence
    validation: ValidationOutcome
    session_recording: SessionRecordingEvidence
    outputs: CodingOutputs = Field(default_factory=CodingOutputs)
    diagnostics: tuple[TimelineDiagnostic, ...]
    commands: tuple[TimelineCommand, ...]

    @model_validator(mode="after")
    def _require_publish_failure_evidence(self) -> "PublishFailedCodingAttempt":
        if not self.reason.strip():
            raise ValueError("publish-failed coding attempt requires a reason")
        if not self.diagnostics:
            raise ValueError("publish-failed coding attempt requires diagnostics")
        _ensure_timestamp_not_after(
            self.started_at,
            self.completed_at,
            "publish-failed coding attempt started_at",
            "completed_at",
        )
        _ensure_timestamp_not_after(
            self.completed_at,
            self.publish_failed_at,
            "publish-failed coding attempt completed_at",
            "publish_failed_at",
        )
        _ensure_event_details(self.commands)
        _ensure_command_kind(self.commands, "open_completion_record")
        return self

    def has_validated_output(self) -> bool:
        return isinstance(self.validation, ValidationPassed)


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
        _ensure_optional_timestamp_order(
            self.started_at,
            self.blocked_at,
            "blocked coding attempt",
        )
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
        _ensure_optional_timestamp_order(
            self.started_at,
            self.failed_at,
            "failed coding attempt",
        )
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
    | PublishFailedCodingAttempt
    | BlockedCodingAttempt
    | FailedCodingAttempt
    | MissingCodingEvidence,
    Field(discriminator="kind"),
]


class ReviewNotReached(LifecycleBase):
    kind: Literal["review_not_reached"] = "review_not_reached"
    reason: Literal[
        "coding_in_progress",
        "coding_failed",
        "publish_failed",
        "validation_failed",
        "not_required",
    ]


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


class ReviewTranscriptAvailable(LifecycleBase):
    kind: Literal["available"] = "available"


class ReviewTranscriptUnavailable(LifecycleBase):
    kind: Literal["unavailable"] = "unavailable"
    reason: str
    diagnostics: tuple[TimelineDiagnostic, ...] = ()

    @field_validator("reason")
    @classmethod
    def _non_empty_reason(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("unavailable review transcript requires a reason")
        return value


ReviewTranscriptEvidence = Annotated[
    ReviewTranscriptAvailable | ReviewTranscriptUnavailable,
    Field(discriminator="kind"),
]


class ReviewApproved(LifecycleBase):
    kind: Literal["review_approved"] = "review_approved"
    reviewer: AgentIdentity
    started_at: Timestamp
    completed_at: Timestamp
    session_recording: SessionRecordingEvidence
    transcript: ReviewTranscriptEvidence
    commands: tuple[TimelineCommand, ...]

    @model_validator(mode="after")
    def _require_event_details(self) -> "ReviewApproved":
        _ensure_timestamp_order(
            self.started_at,
            self.completed_at,
            "approved review",
        )
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
        _ensure_timestamp_order(
            self.started_at,
            self.completed_at,
            "changes-requested review",
        )
        _ensure_event_details(self.commands)
        _ensure_command_kind(self.commands, "open_review_feedback")
        return self


class ReviewFailed(LifecycleBase):
    kind: Literal["review_failed"] = "review_failed"
    reviewer: AgentIdentity | None = None
    started_at: Timestamp | None = None
    failed_at: Timestamp
    reason: str
    session_recording: SessionRecordingEvidence
    diagnostics: tuple[TimelineDiagnostic, ...] = ()
    commands: tuple[TimelineCommand, ...]

    @model_validator(mode="after")
    def _require_event_details(self) -> "ReviewFailed":
        if not self.reason.strip():
            raise ValueError("failed review requires a reason")
        _ensure_optional_timestamp_order(
            self.started_at,
            self.failed_at,
            "failed review",
        )
        _ensure_event_details(self.commands)
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
    | ReviewFailed
    | MissingReviewEvidence,
    Field(discriminator="kind"),
]


class CycleArtifacts(LifecycleBase):
    """Artifact references collected for one cycle (logs, PR, review flag)."""

    log_url: str | None = None
    pr_url: str | None = None
    pr_number: int | None = None
    has_review_feedback: bool = False


class JourneyStep(LifecycleBase):
    """One narrative entry in a journey cycle's step list.

    ``actions`` is an opaque tuple of action descriptors (e.g.
    ``{"type": "open_agent_log", "value": "/path"}``) that the drawer reads
    by ``type``.  The shapes vary across event sources and are not part of
    the typed contract yet — they are pass-through from the event stream.
    PR 2 introduces typed Command dispatch for the per-cycle validation
    badge; broader step-action typing is a later follow-up.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    timestamp: str
    time_label: str
    day: str
    narrative: str
    status: str
    event: str
    detail: str | None = None
    actions: tuple[dict[str, Any], ...] = ()


JourneyPhaseKey = Literal["coding", "review", "rework", "orchestrator"]


class JourneyPhaseGroup(LifecycleBase):
    """A bucket of journey steps under a single user-facing phase label."""

    key: JourneyPhaseKey
    label: str
    steps: tuple[JourneyStep, ...] = ()


class IssueProjectionContext(LifecycleBase):
    """Primitive policy inputs the projection needs for outcome labeling.

    Owned by the projection layer; constructed from the entry-point
    ``IssueStoryContext`` so ``lifecycle_projection`` never imports the
    entry-point dataclass (see issue #6310 AC-3).
    """

    flow_stage: str = ""
    labels: tuple[str, ...] = ()
    current_rework_cycle: int = 0
    max_rework_cycles: int = 5


class CycleValidationBadge(LifecycleBase):
    """Per-cycle validation badge state (issue #6310 AC-2).

    One typed model owns both the badge state and the action it dispatches.
    A ``passed``/``failed`` badge requires an ``OpenValidationDetailsCommand``
    (so the dialog can fetch evidence); ``pending``/``not_validated`` must
    not carry a command (there is no dialog to open).
    """

    state: Literal["pending", "not_validated", "passed", "failed"]
    command: OpenValidationDetailsCommand | None = None

    @model_validator(mode="after")
    def _command_matches_state(self) -> "CycleValidationBadge":
        if self.state in ("passed", "failed") and self.command is None:
            raise ValueError(f"command required for state={self.state!r}")
        if self.state in ("pending", "not_validated") and self.command is not None:
            raise ValueError(f"command must be absent for state={self.state!r}")
        return self


class IssueCycle(LifecycleBase):
    cycle_number: int
    coder: CodingAttempt
    review: ReviewStage
    outcome: str
    diagnostics: tuple[TimelineDiagnostic, ...] = ()

    # Journey/drawer fields — populated when the projection is built with
    # ``IssueProjectionContext`` (e.g. ``/api/issue-detail``).  E2E
    # ``linked_issue_lifecycles[].cycles[]`` leave these at ``None`` since
    # the typed lifecycle does not have journey context.  Per #6310 AC's
    # review feedback: required placeholder facts (lifecycle=0,
    # iteration=0, cycle_label="") would mislead non-journey consumers,
    # so absent journey context is represented as ``None`` rather than
    # sentinel values.
    lifecycle: int | None = None
    iteration: int | None = None
    run_id: str | None = None
    timestamp: str | None = None
    session_run_ids: tuple[str, ...] = ()
    agent: str | None = None
    reviewer_agent: str | None = None
    retry_count: int | None = None
    reset_from_scratch: bool | None = None
    cycle_label: str | None = None
    time_label: str | None = None
    expanded: bool | None = None
    cycle_in_run: int | None = None
    artifacts: CycleArtifacts | None = None
    steps: tuple[JourneyStep, ...] = ()
    phase_groups: tuple[JourneyPhaseGroup, ...] = ()
    # Typed per-cycle validation badge.  ``None`` when no journey context
    # is threaded (e.g. E2E linked-issue lifecycle projection).
    validation: CycleValidationBadge | None = None

    @field_validator("cycle_number")
    @classmethod
    def _positive_cycle_number(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("cycle_number must be positive")
        return value

    @model_validator(mode="after")
    def _review_matches_coding_state(self) -> "IssueCycle":
        if isinstance(self.coder, RunningCodingAttempt):
            _ensure_review_not_reached_reason(self.review, "coding_in_progress")
        elif isinstance(self.coder, PublishFailedCodingAttempt):
            _ensure_review_not_reached_reason(self.review, "publish_failed")
        elif isinstance(self.coder, BlockedCodingAttempt | FailedCodingAttempt):
            _ensure_review_not_reached_reason(self.review, "coding_failed")
        elif isinstance(self.coder, CompletedCodingAttempt) and isinstance(self.coder.validation, ValidationFailed):
            _ensure_review_not_reached_reason(self.review, "validation_failed")
        return self


class JourneyRun(LifecycleBase):
    """A logical-run grouping of issue cycles for the drawer view."""

    run_number: int
    run_label: str
    outcome: str
    run_key: str = ""
    run_id: str | None = None
    session_run_ids: tuple[str, ...] = ()
    timestamp: str = ""
    time_label: str = ""
    expanded: bool = False
    reset_from_scratch: bool = False
    cycles: tuple[IssueCycle, ...] = ()


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
        _ensure_timestamp_order(
            self.started_at,
            self.completed_at,
            "passed E2E test",
        )
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
        _ensure_timestamp_order(
            self.started_at,
            self.completed_at,
            "failed E2E test",
        )
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
        _ensure_optional_timestamp_order(
            self.started_at,
            self.completed_at,
            "E2E run lifecycle",
        )
        return self


class DashboardIteration(LifecycleBase):
    kind: Literal["dashboard_current"] = "dashboard_current"
    subject: TimelineSubject
    issue_lifecycles: tuple[IssueLifecycle, ...]
    diagnostics: tuple[TimelineDiagnostic, ...] = ()

    @model_validator(mode="after")
    def _subject_matches_dashboard_iteration(self) -> "DashboardIteration":
        if self.subject.kind != "dashboard":
            raise ValueError("dashboard iteration subject must be a dashboard subject")
        return self


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

    @model_validator(mode="after")
    def _subject_matches_container(self) -> "DashboardTimelineContainer":
        if self.subject.kind != "dashboard":
            raise ValueError("dashboard container subject must be a dashboard subject")
        if self.current.subject.kind != "dashboard":
            raise ValueError("dashboard current iteration subject must be a dashboard subject")
        return self

    def iter_iterations(self) -> Iterator[DashboardIteration]:
        yield self.current


class E2ESuiteTimelineContainer(LifecycleBase):
    kind: Literal["e2e_suite"] = "e2e_suite"
    subject: TimelineSubject
    runs: tuple[E2ERunIteration, ...]

    @model_validator(mode="after")
    def _require_runs(self) -> "E2ESuiteTimelineContainer":
        if self.subject.kind != "e2e_suite":
            raise ValueError("E2E suite container subject must be an e2e_suite subject")
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
                and cycle.review.reason != "not_required"
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
        for linked in _linked_issues_for_test(test):
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
    if not any(command.kind == kind for command in commands):
        raise ValueError(f"commands must include {kind}")


def _linked_issues_for_test(test: E2ETestExecution) -> tuple[LinkedIssueLifecycle, ...]:
    if isinstance(test, MissingE2ETestEvidence):
        return ()
    return test.linked_issues


def _ensure_review_not_reached_reason(review: ReviewStage, reason: str) -> None:
    if not isinstance(review, ReviewNotReached) or review.reason != reason:
        raise ValueError(f"{reason} coder state requires review_not_reached:{reason}")


def _ensure_optional_timestamp_order(
    started_at: Timestamp | None,
    completed_at: Timestamp | None,
    context: str,
) -> None:
    if started_at is None or completed_at is None:
        return
    _ensure_timestamp_order(started_at, completed_at, context)


def _ensure_timestamp_order(started_at: Timestamp, completed_at: Timestamp, context: str) -> None:
    _ensure_timestamp_not_after(
        started_at,
        completed_at,
        f"{context} started_at",
        f"{context} completed_at",
    )


def _ensure_timestamp_not_after(
    earlier_at: Timestamp,
    later_at: Timestamp,
    earlier_label: str,
    later_label: str,
) -> None:
    earlier = _parse_required_timestamp(earlier_at, earlier_label)
    later = _parse_required_timestamp(later_at, later_label)
    if earlier > later:
        raise ValueError(f"{earlier_label} must not be after {later_label}")


def _parse_required_timestamp(value: Timestamp, label: str) -> datetime:
    text = value.strip()
    if not text or text == "unknown":
        raise ValueError(f"{label} must be a concrete timestamp")
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{label} must be ISO-8601 parseable") from exc


def command_kinds(commands: tuple[TimelineCommand, ...]) -> tuple[str, ...]:
    """Return command kinds for high-signal tests and projections."""
    return tuple(str(command.kind) for command in commands)


__all__ = [
    "AgentIdentity",
    "BlockedCodingAttempt",
    "CodingAttempt",
    "CodingOutputs",
    "CompletedCodingAttempt",
    "CompletionRecordEvidence",
    "CycleArtifacts",
    "CycleValidationBadge",
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
    "IssueProjectionContext",
    "JourneyPhaseGroup",
    "JourneyPhaseKey",
    "JourneyRun",
    "JourneyStep",
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
    "PublishFailedCodingAttempt",
    "ReviewApproved",
    "ReviewChangesRequested",
    "ReviewFailed",
    "ReviewNotReached",
    "ReviewRunning",
    "ReviewSkipped",
    "ReviewStage",
    "ReviewTranscriptAvailable",
    "ReviewTranscriptEvidence",
    "ReviewTranscriptUnavailable",
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
    "validate_lifecycle_container",
]
