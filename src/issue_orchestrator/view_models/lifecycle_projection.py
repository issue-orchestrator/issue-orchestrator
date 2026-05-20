"""Project legacy timeline dictionaries into semantic lifecycle models.

The browser still renders the older event/cycle payloads today.  This module
keeps a stricter representation beside that path so cheap unit and contract
tests can validate the lifecycle semantics before Playwright or live E2E
exercise the UI.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from typing import Any, Literal, cast

from ..domain.logical_run_projection import group_events_by_logical_cycle
from .lifecycle_event_sets import (
    BLOCKED_EVENT_NAMES,
    CODING_BLOCKED_EVENTS,
    CODING_COMPLETED_EVENTS,
    CODING_FAILED_EVENTS,
    CODING_PUBLISH_FAILED_EVENTS,
    CODING_TERMINAL_EVENTS,
    OUTCOME_EVENTS,
    VALIDATION_FAILED_EVENTS,
    VALIDATION_PASSED_EVENTS,
    classify_coding_terminal_event,
)
from .lifecycle_semantics import (
    AgentIdentity,
    BlockedCodingAttempt,
    CodingAttempt,
    CodingOutputs,
    CompletedCodingAttempt,
    CompletionRecordEvidence,
    DashboardIteration,
    DashboardTimelineContainer,
    E2EFailureDetailsAvailable,
    E2EFailureDetailsMissing,
    E2ERunIteration,
    E2ERunLifecycle,
    E2ESuiteTimelineContainer,
    E2ETestExecution,
    FailedCodingAttempt,
    FailedE2ETestExecution,
    IssueCycle,
    IssueLifecycle,
    LinkedIssueLifecycle,
    MissingCodingEvidence,
    MissingE2ETestEvidence,
    MissingEvidence,
    MissingReviewEvidence,
    OpenCompletionRecordCommand,
    OpenIssueTimelineCommand,
    OpenReviewArtifactCommand,
    OpenReviewFeedbackCommand,
    OpenSessionRecordingCommand,
    OpenValidationDetailsCommand,
    OutcomeBadge,
    PassedE2ETestExecution,
    PublishFailedCodingAttempt,
    ReviewApproved,
    ReviewChangesRequested,
    ReviewFailed,
    ReviewNotReached,
    ReviewRunning,
    ReviewSkipped,
    ReviewStage,
    ReviewTranscriptAvailable,
    ReviewTranscriptEvidence,
    ReviewTranscriptUnavailable,
    RunningCodingAttempt,
    RunningE2ETestExecution,
    SessionRecordingAvailable,
    SessionRecordingEvidence,
    SessionRecordingUnavailable,
    ShowEventDetailsCommand,
    TimelineCommand,
    TimelineContainer,
    TimelineDiagnostic,
    TimelineSubject,
    ValidationEvidenceMissing,
    ValidationFailed,
    ValidationNotRun,
    ValidationOutcome,
    ValidationPassed,
    validate_lifecycle_container,
)

EventDict = Mapping[str, Any]
CodingExpectedState = Literal["completed", "running", "blocked", "failed"]
ReviewExpectedState = Literal["approved", "changes_requested", "running"]
ReviewNotReachedReason = Literal[
    "coding_in_progress",
    "coding_failed",
    "publish_failed",
    "validation_failed",
    "not_required",
]

_CODING_START_EVENTS = frozenset(
    {
        "agent.coding_started",
        "agent.rework_started",
        "review.rework_started",
        "session.started",
        "rework.started",
    }
)
# Back-compat private aliases — same identity as the canonical component
# sets imported from ``lifecycle_event_sets`` (consumed by the AC-4
# drift-guard test).  The lifecycle owner consumes the public canonical
# sets directly; these aliases keep the internal call sites stable.
_CODING_COMPLETED_EVENTS = CODING_COMPLETED_EVENTS
_CODING_BLOCKED_EVENTS = CODING_BLOCKED_EVENTS
_CODING_FAILED_EVENTS = CODING_FAILED_EVENTS
_CODING_PUBLISH_FAILED_EVENTS = CODING_PUBLISH_FAILED_EVENTS
_VALIDATION_PASSED_EVENTS = VALIDATION_PASSED_EVENTS
_VALIDATION_FAILED_EVENTS = VALIDATION_FAILED_EVENTS

_REVIEW_START_EVENTS = frozenset(
    {
        "review.started",
        "review_exchange.started",
        "review_exchange.round_started",
    }
)
_REVIEW_APPROVED_EVENTS = frozenset({"review.approved"})
_REVIEW_CHANGES_REQUESTED_EVENTS = frozenset({"review.changes_requested"})
_REVIEW_SKIPPED_EVENTS = frozenset({"review.skipped"})
_REVIEW_FAILED_EVENTS = frozenset({"review_exchange.failed"})
_E2E_TEST_STARTED = "e2e.test_started"
_E2E_TEST_COMPLETED = "e2e.test_completed"


class LifecycleProjectionError(RuntimeError):
    """Raised when a semantic lifecycle projection violates hard invariants."""


def project_dashboard_lifecycle_container(
    *,
    subject_label: str,
    issue_number: int,
    title: str,
    events: Sequence[EventDict],
    cycles: Sequence[EventDict],
    review_required: bool = False,
) -> DashboardTimelineContainer:
    """Build a dashboard container that iterates like the E2E suite container."""
    issue_lifecycle = project_issue_lifecycle(
        issue_number=issue_number,
        title=title,
        events=events,
        cycles=cycles,
        review_required=review_required,
    )
    subject = TimelineSubject(
        kind="dashboard",
        id="current",
        label=subject_label,
    )
    container = DashboardTimelineContainer(
        subject=subject,
        current=DashboardIteration(
            subject=subject,
            issue_lifecycles=(issue_lifecycle,),
        ),
    )
    require_lifecycle_container_valid(container)
    return container


def project_issue_lifecycle(
    *,
    issue_number: int,
    title: str,
    events: Sequence[EventDict],
    cycles: Sequence[EventDict],
    review_required: bool = False,
) -> IssueLifecycle:
    """Project issue timeline cycles into explicit coder/reviewer child models."""
    projected_cycles: list[IssueCycle] = []
    for index, cycle in enumerate(_semantic_cycle_inputs(events, cycles), start=1):
        cycle_events = _cycle_events(cycle, events)
        cycle_number = _positive_int(cycle.get("cycle"), default=index)
        coder, review = project_cycle_stages(
            issue_number=issue_number,
            cycle_number=cycle_number,
            events=cycle_events,
            review_required=review_required,
        )
        projected_cycles.append(
            IssueCycle(
                cycle_number=cycle_number,
                coder=coder,
                review=review,
                outcome=_cycle_outcome(coder, review),
            )
        )
    return IssueLifecycle(
        issue_number=issue_number,
        title=title,
        cycles=tuple(projected_cycles),
    )


def project_cycle_stages(
    *,
    issue_number: int,
    cycle_number: int,
    events: Sequence[EventDict],
    review_required: bool = False,
) -> tuple[CodingAttempt, ReviewStage]:
    """Public cycle-stage projection: events → (typed coder, typed review).

    The lifecycle owner exports a behavior-level API for projecting a
    single cycle's typed coder/review pair so other view models (e.g. the
    drawer journey overlay in ``view_models.journey_projection``) can
    build typed cycles without reaching into private internals.  This is
    the canonical entry for "events of one cycle → typed states".
    """
    coder = _project_coder(
        issue_number=issue_number,
        cycle_number=cycle_number,
        events=events,
    )
    review = _project_review(
        issue_number=issue_number,
        cycle_number=cycle_number,
        events=events,
        coder=coder,
        review_required=review_required,
    )
    return coder, review


def project_issue_lifecycles_from_events(
    events: Sequence[EventDict],
    *,
    title_prefix: str = "Issue",
    review_required: bool = False,
) -> tuple[IssueLifecycle, ...]:
    """Project mixed issue events into one lifecycle per issue number."""
    events_by_issue: dict[int, list[EventDict]] = defaultdict(list)
    for event in events:
        issue_number = event.get("issue_number")
        if isinstance(issue_number, int) and issue_number > 0:
            events_by_issue[issue_number].append(event)
    return tuple(
        project_issue_lifecycle(
            issue_number=issue_number,
            title=f"{title_prefix} #{issue_number}",
            events=issue_events,
            cycles=(),
            review_required=review_required,
        )
        for issue_number, issue_events in sorted(events_by_issue.items())
    )


def project_e2e_suite_lifecycle_container(
    *,
    subject_label: str,
    runs: Sequence[E2ERunIteration],
) -> E2ESuiteTimelineContainer:
    """Build the E2E parent container around run iterations."""
    container = E2ESuiteTimelineContainer(
        subject=TimelineSubject(
            kind="e2e_suite",
            id="e2e",
            label=subject_label,
        ),
        runs=tuple(runs),
    )
    require_lifecycle_container_valid(container)
    return container


def project_e2e_suite_lifecycle_container_for_run(
    *,
    run_id: int,
    events: Sequence[EventDict],
    agent_events: Sequence[EventDict],
    subject_label: str = "E2E Suite",
    review_required: bool = False,
) -> E2ESuiteTimelineContainer:
    """Build the suite container for one E2E run and its linked issue lifecycles."""
    linked_issue_lifecycles = project_issue_lifecycles_from_events(
        agent_events,
        title_prefix="E2E Issue",
        review_required=review_required,
    )
    return project_e2e_suite_lifecycle_container(
        subject_label=subject_label,
        runs=(
            project_e2e_run_iteration(
                run_id=run_id,
                events=events,
                linked_issue_lifecycles=linked_issue_lifecycles,
            ),
        ),
    )


def project_e2e_run_iteration(
    *,
    run_id: int,
    events: Sequence[EventDict],
    linked_issue_lifecycles: Sequence[IssueLifecycle] = (),
) -> E2ERunIteration:
    """Project an E2E run timeline into test execution child models."""
    run_started = _first_event(events, {"e2e.run_started"})
    run_finished = _last_event(
        events, {"e2e.run_finished", "e2e.run_error", "e2e.run_canceled"}
    )
    started_at = (
        _event_timestamp(run_started) if run_started else _first_timestamp(events)
    )
    tests = _project_e2e_tests(run_id=run_id, events=events)
    subject = TimelineSubject(
        kind="e2e_run",
        id=str(run_id),
        label=f"Run #{run_id}",
        status=str(run_finished.get("status"))
        if run_finished and run_finished.get("status")
        else None,
    )
    return E2ERunIteration(
        subject=subject,
        e2e_run=E2ERunLifecycle(
            run_id=run_id,
            started_at=started_at,
            completed_at=_event_timestamp(run_finished) if run_finished else None,
            tests=tuple(tests),
            linked_issue_lifecycles=tuple(linked_issue_lifecycles),
        ),
    )


def require_lifecycle_container_valid(container: TimelineContainer) -> None:
    """Raise for aggregate lifecycle errors while allowing informational diagnostics."""
    diagnostics = validate_lifecycle_container(container)
    errors = [diag for diag in diagnostics if diag.severity == "error"]
    if not errors:
        return
    detail = "; ".join(f"{diag.code}: {diag.message}" for diag in errors)
    raise LifecycleProjectionError(detail)


def _cycle_events(
    cycle: EventDict, all_events: Sequence[EventDict]
) -> tuple[EventDict, ...]:
    raw = cycle.get("events")
    if isinstance(raw, list):
        events = tuple(event for event in raw if isinstance(event, Mapping))
        if not _has_coding_lifecycle_signal(events) and _has_coding_lifecycle_signal(
            all_events
        ):
            return tuple(event for event in all_events if isinstance(event, Mapping))
        return events
    cycle_number = cycle.get("cycle")
    if isinstance(cycle_number, int):
        return tuple(
            event
            for event in all_events
            if isinstance(event, Mapping) and event.get("logical_cycle") == cycle_number
        )
    return tuple(event for event in all_events if isinstance(event, Mapping))


def _has_coding_lifecycle_signal(events: Sequence[EventDict]) -> bool:
    signal_events = (
        _CODING_START_EVENTS
        | _CODING_COMPLETED_EVENTS
        | _CODING_BLOCKED_EVENTS
        | _CODING_FAILED_EVENTS
    )
    return any(
        _event_name(event) in signal_events
        and not _is_review_completion_observation(event)
        for event in events
    )


def _has_review_lifecycle_signal(events: Sequence[EventDict]) -> bool:
    signal_events = (
        _REVIEW_START_EVENTS
        | _REVIEW_APPROVED_EVENTS
        | _REVIEW_CHANGES_REQUESTED_EVENTS
        | _REVIEW_SKIPPED_EVENTS
        | _REVIEW_FAILED_EVENTS
    )
    return any(_event_name(event) in signal_events for event in events)


def _semantic_cycle_inputs(
    events: Sequence[EventDict],
    cycles: Sequence[EventDict],
) -> tuple[EventDict, ...]:
    real_cycles = tuple(cycle for cycle in cycles if isinstance(cycle, Mapping))
    if real_cycles:
        return real_cycles
    logical_cycles = _semantic_cycle_inputs_from_logical_fields(events)
    if logical_cycles:
        return logical_cycles
    if events:
        return ({"cycle": 1, "events": list(events)},)
    return ({"cycle": 1, "events": []},)


def _semantic_cycle_inputs_from_logical_fields(
    events: Sequence[EventDict],
) -> tuple[EventDict, ...]:
    grouped_events: list[dict[str, Any]] = []
    saw_logical_cycle_fields = False
    saw_legacy_event = False
    for event in events:
        if not isinstance(event, Mapping):
            continue
        logical_run = event.get("logical_run")
        logical_cycle = event.get("logical_cycle")
        if logical_run is None and logical_cycle is None:
            if saw_logical_cycle_fields:
                raise LifecycleProjectionError(
                    "timeline events must not mix logical cycle fields with legacy events"
                )
            saw_legacy_event = True
            continue
        if saw_legacy_event:
            raise LifecycleProjectionError(
                "timeline events must not mix logical cycle fields with legacy events"
            )
        saw_logical_cycle_fields = True
        if not isinstance(logical_run, int) or logical_run <= 0:
            raise LifecycleProjectionError(
                "timeline event logical_run must be a positive integer"
            )
        if not isinstance(logical_cycle, int) or logical_cycle <= 0:
            raise LifecycleProjectionError(
                "timeline event logical_cycle must be a positive integer"
            )
        grouped_events.append(dict(event))

    if not saw_logical_cycle_fields:
        return ()

    groups = group_events_by_logical_cycle(grouped_events)
    lifecycle_groups = tuple(
        group
        for group in groups
        if _has_coding_lifecycle_signal(group.events)
        or _has_review_lifecycle_signal(group.events)
    )
    if lifecycle_groups:
        groups = lifecycle_groups

    return tuple(
        {
            "cycle": index,
            "events": list(group.events),
        }
        for index, group in enumerate(groups, start=1)
    )


def _project_coder(
    *,
    issue_number: int,
    cycle_number: int,
    events: Sequence[EventDict],
) -> CodingAttempt:
    start = _first_event(events, _CODING_START_EVENTS)
    terminal = _last_coding_terminal_event(events)
    observed = _event_timestamp(terminal or start)

    # Single classifier owner for "which bucket does this terminal event
    # belong to" — drift between the four sub-categories is impossible
    # because the classification lives in ``lifecycle_event_sets``.
    terminal_kind = (
        classify_coding_terminal_event(_event_name(terminal))
        if terminal is not None
        else None
    )

    if terminal_kind == "completed":
        assert terminal is not None
        return _completed_coder_attempt(
            issue_number=issue_number,
            events=events,
            start=start,
            completed=terminal,
            observed_at=observed,
        )

    if terminal_kind == "blocked":
        assert terminal is not None
        return _blocked_coder_attempt(
            issue_number=issue_number,
            start=start,
            blocked=terminal,
            observed_at=observed,
        )

    if terminal_kind == "publish_failed":
        assert terminal is not None
        return _publish_failed_coder_attempt(
            issue_number=issue_number,
            events=events,
            start=start,
            publish_failed=terminal,
            observed_at=observed,
        )

    if terminal_kind == "failed":
        assert terminal is not None
        return _failed_coder_attempt(
            issue_number=issue_number,
            start=start,
            failed=terminal,
        )

    if start is not None and _has_review_lifecycle_signal(events):
        observed_event = _last_review_lifecycle_event(events) or start
        return _missing_coding(
            issue_number=issue_number,
            expected_state="completed",
            observed_at=_event_timestamp(observed_event),
            missing=(
                _missing(
                    "coding_terminal_event",
                    f"cycle {cycle_number} reached review without a completed coding event",
                ),
            ),
            event=observed_event,
        )

    if start is not None:
        return _running_coder_attempt(
            issue_number=issue_number,
            start=start,
            observed_at=observed,
        )

    return _missing_coding(
        issue_number=issue_number,
        expected_state="running",
        observed_at=observed,
        missing=(
            _missing("coding_start", f"cycle {cycle_number} has no coding start event"),
        ),
        event=None,
    )


def _last_coding_terminal_event(events: Sequence[EventDict]) -> EventDict | None:
    terminal_events = (
        _CODING_COMPLETED_EVENTS
        | _CODING_BLOCKED_EVENTS
        | _CODING_FAILED_EVENTS
        | _CODING_PUBLISH_FAILED_EVENTS
    )
    for event in reversed(events):
        if _event_name(event) not in terminal_events:
            continue
        if _is_review_completion_observation(event):
            continue
        return event
    return None


def _is_review_completion_observation(event: EventDict) -> bool:
    if _event_name(event) != "observation.completion_detected":
        return False
    summary = _optional_text(event.get("summary"))
    return summary is not None and summary.startswith("review_")


def _last_review_lifecycle_event(events: Sequence[EventDict]) -> EventDict | None:
    signal_events = (
        _REVIEW_START_EVENTS
        | _REVIEW_APPROVED_EVENTS
        | _REVIEW_CHANGES_REQUESTED_EVENTS
        | _REVIEW_SKIPPED_EVENTS
        | _REVIEW_FAILED_EVENTS
    )
    return _last_event(events, signal_events)


def _last_coding_completed_event(events: Sequence[EventDict]) -> EventDict | None:
    for event in reversed(events):
        if _event_name(event) not in _CODING_COMPLETED_EVENTS:
            continue
        if _is_review_completion_observation(event):
            continue
        return event
    return None


def _completed_coder_attempt(
    *,
    issue_number: int,
    events: Sequence[EventDict],
    start: EventDict | None,
    completed: EventDict,
    observed_at: str,
) -> CodingAttempt:
    agent = _agent_identity_from_events(start, completed, role="coder")
    completion_path = _artifact_value(completed, "completion_record")
    missing = _completed_coder_missing(agent=agent, completion_path=completion_path)
    if missing:
        return _missing_coding(
            issue_number=issue_number,
            expected_state="completed",
            observed_at=observed_at,
            missing=missing,
            event=completed,
        )
    assert agent is not None
    assert completion_path is not None
    session_recording = _session_recording(issue_number, start or completed)
    return CompletedCodingAttempt(
        issue_number=issue_number,
        agent=agent,
        started_at=_event_timestamp(start or completed),
        completed_at=_event_timestamp(completed),
        completion_record=CompletionRecordEvidence(
            path=completion_path,
            summary=_optional_text(completed.get("summary")),
        ),
        validation=_validation_outcome(issue_number, events),
        session_recording=session_recording,
        outputs=CodingOutputs(
            worktree_path=_artifact_value(completed, "worktree"),
            pull_request_url=_artifact_value(completed, "pull_request"),
        ),
        commands=_completed_coder_commands(
            completed, completion_path, session_recording
        ),
    )


def _completed_coder_missing(
    *,
    agent: AgentIdentity | None,
    completion_path: str | None,
) -> tuple[MissingEvidence, ...]:
    missing: list[MissingEvidence] = []
    if agent is None:
        missing.append(
            _missing("agent", "completed coding attempt did not name its coding agent")
        )
    if completion_path is None:
        missing.append(
            _missing(
                "completion_record",
                "completed coding attempt did not expose a completion record",
            )
        )
    return tuple(missing)


def _completed_coder_commands(
    completed: EventDict,
    completion_path: str,
    session_recording: SessionRecordingEvidence,
) -> tuple[TimelineCommand, ...]:
    commands: tuple[TimelineCommand, ...] = (
        _details_command(completed),
        OpenCompletionRecordCommand(path=completion_path),
    )
    if isinstance(session_recording, SessionRecordingAvailable):
        commands += (session_recording.command,)
    return commands


def _publish_failed_coder_attempt(
    *,
    issue_number: int,
    events: Sequence[EventDict],
    start: EventDict | None,
    publish_failed: EventDict,
    observed_at: str,
) -> CodingAttempt:
    completed = _last_coding_completed_event(events)
    if completed is None:
        return _missing_coding(
            issue_number=issue_number,
            expected_state="completed",
            observed_at=observed_at,
            missing=(
                _missing(
                    "coding_terminal_event",
                    "publish failure observed without a completed coding event",
                ),
            ),
            event=publish_failed,
        )

    agent = _agent_identity_from_events(start, completed, role="coder")
    completion_path = _artifact_value(completed, "completion_record")
    missing = _completed_coder_missing(agent=agent, completion_path=completion_path)
    if missing:
        return _missing_coding(
            issue_number=issue_number,
            expected_state="completed",
            observed_at=observed_at,
            missing=missing,
            event=completed,
        )

    assert agent is not None
    assert completion_path is not None
    session_recording = _session_recording(issue_number, start or completed)
    return PublishFailedCodingAttempt(
        issue_number=issue_number,
        agent=agent,
        started_at=_event_timestamp(start or completed),
        completed_at=_event_timestamp(completed),
        publish_failed_at=_event_timestamp(publish_failed),
        reason=_event_summary(publish_failed),
        completion_record=CompletionRecordEvidence(
            path=completion_path,
            summary=_optional_text(completed.get("summary")),
        ),
        validation=_validation_outcome(issue_number, events),
        session_recording=session_recording,
        outputs=CodingOutputs(
            worktree_path=_artifact_value(completed, "worktree"),
            pull_request_url=_artifact_value(completed, "pull_request"),
        ),
        diagnostics=(
            TimelineDiagnostic(
                code="publish.failed",
                message=_event_summary(publish_failed),
                severity="error",
                evidence_ref=_event_ref(publish_failed),
            ),
        ),
        commands=_completed_coder_commands(
            publish_failed, completion_path, session_recording
        ),
    )


def _blocked_coder_attempt(
    *,
    issue_number: int,
    start: EventDict | None,
    blocked: EventDict,
    observed_at: str,
) -> CodingAttempt:
    agent = _agent_identity_from_events(start, blocked, role="coder")
    if agent is None:
        return _missing_coding(
            issue_number=issue_number,
            expected_state="blocked",
            observed_at=observed_at,
            missing=(
                _missing(
                    "agent", "blocked coding attempt did not name its coding agent"
                ),
            ),
            event=blocked,
        )
    return BlockedCodingAttempt(
        issue_number=issue_number,
        agent=agent,
        started_at=_event_timestamp(start) if start else None,
        blocked_at=_event_timestamp(blocked),
        reason=_event_summary(blocked),
        session_recording=_session_recording(issue_number, start or blocked),
        commands=(_details_command(blocked),),
    )


def _failed_coder_attempt(
    *,
    issue_number: int,
    start: EventDict | None,
    failed: EventDict,
) -> CodingAttempt:
    agent = _agent_identity_from_events(start, failed, role="coder")
    if agent is None:
        return _missing_coding(
            issue_number=issue_number,
            expected_state="failed",
            observed_at=_event_timestamp(failed),
            missing=(
                _missing(
                    "agent", "failed coding attempt did not name its coding agent"
                ),
            ),
            event=failed,
        )
    return FailedCodingAttempt(
        issue_number=issue_number,
        agent=agent,
        started_at=_event_timestamp(start) if start else None,
        failed_at=_event_timestamp(failed),
        reason=_event_summary(failed),
        session_recording=_session_recording(issue_number, start or failed),
        commands=(_details_command(failed),),
    )


def _running_coder_attempt(
    *,
    issue_number: int,
    start: EventDict,
    observed_at: str,
) -> CodingAttempt:
    agent = _agent_identity_from_event(start, role="coder")
    if agent is None:
        return _missing_coding(
            issue_number=issue_number,
            expected_state="running",
            observed_at=observed_at,
            missing=(
                _missing(
                    "agent", "running coding attempt did not name its coding agent"
                ),
            ),
            event=start,
        )
    session_recording = _session_recording(issue_number, start)
    commands: tuple[TimelineCommand, ...] = (_details_command(start),)
    if isinstance(session_recording, SessionRecordingAvailable):
        commands += (session_recording.command,)
    return RunningCodingAttempt(
        issue_number=issue_number,
        agent=agent,
        started_at=_event_timestamp(start),
        session_recording=session_recording,
        commands=commands,
    )


def _project_review(
    *,
    issue_number: int,
    cycle_number: int,
    events: Sequence[EventDict],
    coder: CodingAttempt,
    review_required: bool,
) -> ReviewStage:
    blocked_by_coder = _review_not_reached_for_coder(coder)
    if blocked_by_coder is not None:
        return blocked_by_coder

    start = _first_event(events, _REVIEW_START_EVENTS)
    terminal = _last_event(
        events,
        _REVIEW_APPROVED_EVENTS
        | _REVIEW_CHANGES_REQUESTED_EVENTS
        | _REVIEW_SKIPPED_EVENTS
        | _REVIEW_FAILED_EVENTS,
    )

    if terminal is not None and _event_name(terminal) in _REVIEW_APPROVED_EVENTS:
        return _approved_review(
            issue_number=issue_number,
            start=start,
            approved=terminal,
        )

    if (
        terminal is not None
        and _event_name(terminal) in _REVIEW_CHANGES_REQUESTED_EVENTS
    ):
        return _changes_requested_review(
            issue_number=issue_number,
            start=start,
            changes_requested=terminal,
        )

    if terminal is not None and _event_name(terminal) in _REVIEW_FAILED_EVENTS:
        return _failed_review(
            issue_number=issue_number,
            start=start,
            failed=terminal,
        )

    if terminal is not None and _event_name(terminal) in _REVIEW_SKIPPED_EVENTS:
        return ReviewSkipped(reason=_event_summary(terminal))

    if start is not None:
        return _running_review(issue_number=issue_number, start=start)

    return _unreached_review(
        coder=coder,
        cycle_number=cycle_number,
        events=events,
        review_required=review_required,
    )


def _review_not_reached_for_coder(coder: CodingAttempt) -> ReviewNotReached | None:
    reason = _review_not_reached_reason_for_coder(coder)
    if reason is not None:
        return ReviewNotReached(reason=reason)
    return None


def _review_not_reached_reason_for_coder(
    coder: CodingAttempt,
) -> ReviewNotReachedReason | None:
    if isinstance(coder, RunningCodingAttempt):
        return "coding_in_progress"
    if isinstance(coder, PublishFailedCodingAttempt):
        return "publish_failed"
    if isinstance(coder, FailedCodingAttempt | BlockedCodingAttempt):
        return "coding_failed"
    if isinstance(coder, MissingCodingEvidence):
        return "coding_failed"
    if isinstance(coder, CompletedCodingAttempt) and isinstance(
        coder.validation, ValidationFailed
    ):
        return "validation_failed"
    return None


def _approved_review(
    *,
    issue_number: int,
    start: EventDict | None,
    approved: EventDict,
) -> ReviewStage:
    reviewer = _agent_identity_from_event(approved, role="reviewer")
    if reviewer is None:
        return _missing_review(
            expected_state="approved",
            observed_at=_event_timestamp(approved),
            missing=(_missing("reviewer", "approved review did not name a reviewer"),),
            event=approved,
        )
    return ReviewApproved(
        reviewer=reviewer,
        started_at=_event_timestamp(start or approved),
        completed_at=_event_timestamp(approved),
        session_recording=_session_recording(issue_number, approved),
        transcript=_review_transcript_evidence(approved),
        commands=(
            _details_command(approved),
            *_review_artifact_commands(issue_number, approved),
        ),
    )


def _review_transcript_evidence(event: EventDict) -> ReviewTranscriptEvidence:
    if _has_action(event, "open_review_transcript"):
        return ReviewTranscriptAvailable()
    return ReviewTranscriptUnavailable(
        reason="approved review event did not expose a transcript action",
        diagnostics=(
            TimelineDiagnostic(
                code="review.transcript_action_missing",
                message="approved review event did not expose a transcript action",
                severity="warning",
                evidence_ref=_event_ref(event),
            ),
        ),
    )


def _changes_requested_review(
    *,
    issue_number: int,
    start: EventDict | None,
    changes_requested: EventDict,
) -> ReviewStage:
    reviewer = _agent_identity_from_event(changes_requested, role="reviewer")
    feedback_summary = _review_feedback_summary(changes_requested)
    missing = _changes_requested_missing(
        reviewer=reviewer,
        feedback_summary=feedback_summary,
    )
    if missing:
        return _missing_review(
            expected_state="changes_requested",
            observed_at=_event_timestamp(changes_requested),
            missing=missing,
            event=changes_requested,
        )
    assert reviewer is not None
    assert feedback_summary is not None
    return ReviewChangesRequested(
        reviewer=reviewer,
        started_at=_event_timestamp(start or changes_requested),
        completed_at=_event_timestamp(changes_requested),
        feedback_summary=feedback_summary,
        session_recording=_session_recording(issue_number, changes_requested),
        commands=(
            _details_command(changes_requested),
            OpenReviewFeedbackCommand(
                issue_number=issue_number,
                event_ref=_event_ref(changes_requested),
            ),
            *_review_artifact_commands(issue_number, changes_requested),
        ),
    )


def _review_feedback_summary(event: EventDict) -> str | None:
    return _optional_text(
        event.get("reviewer_response_text")
        or event.get("summary")
        or event.get("narrative")
    )


def _changes_requested_missing(
    *,
    reviewer: AgentIdentity | None,
    feedback_summary: str | None,
) -> tuple[MissingEvidence, ...]:
    missing: list[MissingEvidence] = []
    if reviewer is None:
        missing.append(
            _missing("reviewer", "changes-requested review did not name a reviewer")
        )
    if feedback_summary is None:
        missing.append(
            _missing(
                "review_feedback", "changes-requested review did not expose feedback"
            )
        )
    return tuple(missing)


def _running_review(*, issue_number: int, start: EventDict) -> ReviewStage:
    reviewer = _agent_identity_from_event(start, role="reviewer")
    if reviewer is None:
        return _missing_review(
            expected_state="running",
            observed_at=_event_timestamp(start),
            missing=(_missing("reviewer", "running review did not name a reviewer"),),
            event=start,
        )
    return ReviewRunning(
        reviewer=reviewer,
        started_at=_event_timestamp(start),
        session_recording=_session_recording(issue_number, start),
        commands=(_details_command(start),),
    )


def _failed_review(
    *,
    issue_number: int,
    start: EventDict | None,
    failed: EventDict,
) -> ReviewFailed:
    return ReviewFailed(
        reviewer=_agent_identity_from_event(start or failed, role="reviewer"),
        started_at=_event_timestamp(start) if start else None,
        failed_at=_event_timestamp(failed),
        reason=_event_summary(failed),
        session_recording=_session_recording(issue_number, start or failed),
        diagnostics=(
            TimelineDiagnostic(
                code="review.failed",
                message=_event_summary(failed),
                severity="error",
                evidence_ref=_event_ref(failed),
            ),
        ),
        commands=(_details_command(failed),),
    )


def _unreached_review(
    *,
    coder: CodingAttempt,
    cycle_number: int,
    events: Sequence[EventDict],
    review_required: bool,
) -> ReviewStage:
    blocked_by_coder = _review_not_reached_for_coder(coder)
    if blocked_by_coder is not None:
        return blocked_by_coder
    if review_required:
        return _missing_review(
            expected_state="running",
            observed_at=_first_timestamp(events),
            missing=(
                _missing(
                    "review_stage",
                    f"cycle {cycle_number} completed coding without a review stage",
                ),
            ),
            event=None,
        )
    return ReviewNotReached(reason="not_required")


def _validation_outcome(
    issue_number: int, events: Sequence[EventDict]
) -> ValidationOutcome:
    passed = _last_event(events, _VALIDATION_PASSED_EVENTS)
    failed = _last_event(events, _VALIDATION_FAILED_EVENTS)
    terminal = failed or passed
    if terminal is None:
        return ValidationNotRun(reason="not_required")

    record_path = _artifact_value(terminal, "validation")
    command = _event_summary(terminal)
    if not record_path:
        return ValidationEvidenceMissing(
            expected_record_path=None,
            diagnostics=(
                TimelineDiagnostic(
                    code="validation.record_missing",
                    message="validation event did not expose a validation record artifact",
                    severity="error",
                    evidence_ref=_event_ref(terminal),
                ),
            ),
        )
    run_dir = _optional_text(terminal.get("run_dir"))
    if failed is not None:
        if not run_dir:
            return ValidationEvidenceMissing(
                expected_record_path=record_path,
                diagnostics=(
                    TimelineDiagnostic(
                        code="validation.run_dir_missing",
                        message="failed validation event did not expose run_dir for details",
                        severity="error",
                        evidence_ref=_event_ref(terminal),
                    ),
                ),
            )
        return ValidationFailed(
            command=command,
            record_path=record_path,
            failure_summary=_event_summary(terminal),
            details_command=OpenValidationDetailsCommand(
                issue_number=issue_number,
                run_dir=run_dir,
            ),
        )
    # Passed events also carry run_dir (session_controller emits both
    # SESSION_VALIDATION_PASSED and SESSION_VALIDATION_FAILED with run_dir);
    # we project the details_command on green cycles so the per-cycle
    # validation modal can load JUnit evidence without a new endpoint.
    # A passed event missing run_dir is treated the same way a failed
    # event without run_dir is — `ValidationEvidenceMissing` — because the
    # outcome we'd otherwise project would be unactionable from the UI.
    if not run_dir:
        return ValidationEvidenceMissing(
            expected_record_path=record_path,
            diagnostics=(
                TimelineDiagnostic(
                    code="validation.run_dir_missing",
                    message="passed validation event did not expose run_dir for details",
                    severity="error",
                    evidence_ref=_event_ref(terminal),
                ),
            ),
        )
    return ValidationPassed(
        command=command,
        record_path=record_path,
        details_command=OpenValidationDetailsCommand(
            issue_number=issue_number,
            run_dir=run_dir,
        ),
    )


def _project_e2e_tests(
    *,
    run_id: int,
    events: Sequence[EventDict],
) -> list[E2ETestExecution]:
    ordered_nodeids, started_by_nodeid, completed_by_nodeid = _index_e2e_test_events(
        events
    )
    if not ordered_nodeids:
        return [_missing_collected_e2e_tests(events)]
    return [
        _project_e2e_test(
            run_id=run_id,
            nodeid=nodeid,
            started=started_by_nodeid.get(nodeid),
            completed=completed_by_nodeid.get(nodeid),
        )
        for nodeid in ordered_nodeids
    ]


def _index_e2e_test_events(
    events: Sequence[EventDict],
) -> tuple[list[str], dict[str, EventDict], dict[str, EventDict]]:
    started_by_nodeid: dict[str, EventDict] = {}
    completed_by_nodeid: dict[str, EventDict] = {}
    ordered_nodeids: list[str] = []
    for event in events:
        if _event_name(event) not in {_E2E_TEST_STARTED, _E2E_TEST_COMPLETED}:
            continue
        nodeid = _optional_text(event.get("nodeid"))
        if nodeid is None:
            continue
        if nodeid not in ordered_nodeids:
            ordered_nodeids.append(nodeid)
        if _event_name(event) == _E2E_TEST_STARTED:
            started_by_nodeid.setdefault(nodeid, event)
        else:
            completed_by_nodeid[nodeid] = event
    return ordered_nodeids, started_by_nodeid, completed_by_nodeid


def _project_e2e_test(
    *,
    run_id: int,
    nodeid: str,
    started: EventDict | None,
    completed: EventDict | None,
) -> E2ETestExecution:
    if completed is None:
        return RunningE2ETestExecution(
            nodeid=nodeid,
            started_at=_event_timestamp(started),
            linked_issues=_linked_issues(run_id, started),
            commands=(_details_command(started),),
        )
    if started is None:
        return _missing_started_e2e_test(nodeid=nodeid, completed=completed)
    if _e2e_test_failed(completed):
        return FailedE2ETestExecution(
            nodeid=nodeid,
            started_at=_event_timestamp(started),
            completed_at=_event_timestamp(completed),
            duration_seconds=_float_or_none(completed.get("duration_seconds")),
            failure=_e2e_failure_evidence(nodeid=nodeid, completed=completed),
            linked_issues=_linked_issues(run_id, completed),
            commands=(_details_command(completed),),
        )
    return PassedE2ETestExecution(
        nodeid=nodeid,
        started_at=_event_timestamp(started),
        completed_at=_event_timestamp(completed),
        duration_seconds=_float_or_none(completed.get("duration_seconds")),
        linked_issues=_linked_issues(run_id, completed),
        commands=(_details_command(completed),),
    )


def _missing_started_e2e_test(
    *, nodeid: str, completed: EventDict
) -> MissingE2ETestEvidence:
    return MissingE2ETestEvidence(
        nodeid=nodeid,
        observed_at=_event_timestamp(completed),
        missing=(
            _missing(
                "test_started_event",
                "completed E2E test did not have a matching start event",
            ),
        ),
        diagnostics=(
            TimelineDiagnostic(
                code="e2e.test_started_missing",
                message=f"E2E test {nodeid} completed without a matching start event",
                severity="error",
                evidence_ref=_event_ref(completed),
            ),
        ),
        commands=(_details_command(completed),),
    )


def _missing_collected_e2e_tests(events: Sequence[EventDict]) -> MissingE2ETestEvidence:
    observed_at = _first_timestamp(events)
    return MissingE2ETestEvidence(
        nodeid="__e2e_tests__",
        observed_at=observed_at,
        missing=(
            _missing(
                "test_started_event",
                "E2E run timeline did not include any test_started events",
            ),
        ),
        diagnostics=(
            TimelineDiagnostic(
                code="e2e.tests_missing",
                message="E2E run lifecycle requires at least one test execution",
                severity="error",
            ),
        ),
        commands=(_details_command(events[0] if events else None),),
    )


def _e2e_test_failed(completed: EventDict) -> bool:
    outcome = str(completed.get("outcome") or completed.get("status") or "").lower()
    return outcome in {"failed", "error"}


def _e2e_failure_evidence(
    *,
    nodeid: str,
    completed: EventDict,
) -> E2EFailureDetailsAvailable | E2EFailureDetailsMissing:
    longrepr = _optional_text(completed.get("longrepr"))
    if longrepr is not None:
        return E2EFailureDetailsAvailable(longrepr=longrepr)
    return E2EFailureDetailsMissing(
        diagnostics=(
            TimelineDiagnostic(
                code="e2e.failure_details_missing",
                message=f"failed E2E test {nodeid} did not expose longrepr",
                severity="error",
                evidence_ref=_event_ref(completed),
            ),
        )
    )


def _linked_issues(
    run_id: int, event: EventDict | None
) -> tuple[LinkedIssueLifecycle, ...]:
    if event is None:
        return ()
    raw_affordances = event.get("issue_affordances")
    issue_numbers: list[int] = []
    if isinstance(raw_affordances, list):
        for affordance in raw_affordances:
            if isinstance(affordance, Mapping):
                issue_number = _positive_int(affordance.get("issue_number"), default=0)
                if issue_number > 0:
                    issue_numbers.append(issue_number)
    return tuple(
        LinkedIssueLifecycle(
            issue_number=issue_number,
            relationship="exercises",
            command=OpenIssueTimelineCommand(
                issue_number=issue_number,
                scope_kind="e2e_run",
                e2e_run_id=run_id,
            ),
        )
        for issue_number in dict.fromkeys(issue_numbers)
    )


def _missing_coding(
    *,
    issue_number: int,
    expected_state: CodingExpectedState,
    observed_at: str,
    missing: tuple[MissingEvidence, ...],
    event: EventDict | None,
) -> MissingCodingEvidence:
    return MissingCodingEvidence(
        issue_number=issue_number,
        expected_state=expected_state,
        observed_at=observed_at,
        missing=missing,
        diagnostics=tuple(
            TimelineDiagnostic(
                code=f"coding.{item.evidence}.missing",
                message=item.reason,
                severity="error",
                evidence_ref=_event_ref(event) if event is not None else None,
            )
            for item in missing
        ),
        commands=(_details_command(event),),
    )


def _missing_review(
    *,
    expected_state: ReviewExpectedState,
    observed_at: str,
    missing: tuple[MissingEvidence, ...],
    event: EventDict | None,
) -> MissingReviewEvidence:
    return MissingReviewEvidence(
        expected_state=expected_state,
        observed_at=observed_at,
        missing=missing,
        diagnostics=tuple(
            TimelineDiagnostic(
                code=f"review.{item.evidence}.missing",
                message=item.reason,
                severity="error",
                evidence_ref=_event_ref(event) if event is not None else None,
            )
            for item in missing
        ),
        commands=(_details_command(event),),
    )


def _missing(evidence: str, reason: str) -> MissingEvidence:
    return MissingEvidence(evidence=evidence, reason=reason)


def _session_recording(
    issue_number: int, event: EventDict | None
) -> SessionRecordingEvidence:
    run_dir = _optional_text(event.get("run_dir")) if event is not None else None
    if run_dir is None:
        return SessionRecordingUnavailable(
            reason="timeline event did not expose run_dir",
            diagnostics=(
                TimelineDiagnostic(
                    code="session_recording.run_dir_missing",
                    message="session recording requires run_dir evidence",
                    severity="warning",
                    evidence_ref=_event_ref(event) if event is not None else None,
                ),
            ),
        )
    assert event is not None
    round_index = (
        event.get("round_index") if isinstance(event.get("round_index"), int) else None
    )
    command = OpenSessionRecordingCommand(
        issue_number=issue_number,
        run_dir=run_dir,
        session_role=(
            _optional_text(event.get("task") or event.get("logical_phase"))
            if round_index is not None
            else None
        ),
        round_index=round_index,
    )
    return SessionRecordingAvailable(
        run_dir=run_dir,
        recording_path=f"{run_dir.rstrip('/')}/terminal-recording.jsonl",
        command=command,
    )


def _details_command(event: EventDict | None) -> ShowEventDetailsCommand:
    return ShowEventDetailsCommand(event_ref=_event_ref(event))


def _event_ref(event: EventDict | None) -> str:
    if event is None:
        return "missing-event"
    for key in ("event_id", "detail_id"):
        value = _optional_text(event.get(key))
        if value is not None:
            return value
    name = _event_name(event) or "event"
    timestamp = _optional_text(event.get("timestamp")) or "no-timestamp"
    return f"{name}:{timestamp}"


def _event_name(event: EventDict | None) -> str:
    if event is None:
        return ""
    return str(event.get("source_event") or event.get("event") or "")


def _first_event(
    events: Sequence[EventDict], names: set[str] | frozenset[str]
) -> EventDict | None:
    for event in events:
        if _event_name(event) in names:
            return event
    return None


def _last_event(
    events: Sequence[EventDict], names: set[str] | frozenset[str]
) -> EventDict | None:
    for event in reversed(events):
        if _event_name(event) in names:
            return event
    return None


def _first_timestamp(events: Sequence[EventDict]) -> str:
    if events:
        return _event_timestamp(events[0])
    return "unknown"


def _event_timestamp(event: EventDict | None) -> str:
    if event is None:
        return "unknown"
    timestamp = _optional_text(event.get("timestamp"))
    return timestamp or "unknown"


def _event_summary(event: EventDict) -> str:
    return _optional_text(
        event.get("summary")
        or event.get("narrative")
        or event.get("detail")
        or event.get("event")
    ) or _event_ref(event)


def _agent_identity_from_event(
    event: EventDict | None,
    *,
    role: str,
) -> AgentIdentity | None:
    if event is None:
        return None
    raw = event.get("reviewer_agent") if role == "reviewer" else None
    name = _optional_text(raw or event.get("agent"))
    if name is None:
        return None
    return AgentIdentity(
        name=name,
        role="reviewer" if role == "reviewer" else "coder",
    )


def _agent_identity_from_events(
    *events: EventDict | None,
    role: str,
) -> AgentIdentity | None:
    for event in events:
        agent = _agent_identity_from_event(event, role=role)
        if agent is not None:
            return agent
    return None


def _artifact_value(event: EventDict, artifact_type: str) -> str | None:
    raw = event.get("artifacts")
    if not isinstance(raw, list):
        return None
    for artifact in raw:
        if not isinstance(artifact, Mapping):
            continue
        if artifact.get("type") == artifact_type:
            return _optional_text(artifact.get("value"))
    return None


def _review_artifact_commands(
    issue_number: int,
    event: EventDict,
) -> tuple[OpenReviewArtifactCommand, ...]:
    run_dir = _optional_text(event.get("run_dir"))
    if run_dir is None:
        return ()
    commands: list[OpenReviewArtifactCommand] = []
    for artifact_type, label, render_mode in (
        ("review_report", "Review report", "markdown"),
        ("review_decision", "Decision JSON", "json"),
    ):
        path = _artifact_value(event, artifact_type)
        if path is None:
            continue
        commands.append(
            OpenReviewArtifactCommand(
                label=label,
                issue_number=issue_number,
                run_dir=run_dir,
                artifact_path=path,
                artifact_type=cast(
                    Literal["review_report", "review_decision"], artifact_type,
                ),
                render_mode=cast(Literal["markdown", "json"], render_mode),
            )
        )
    return tuple(commands)


def _has_action(event: EventDict, action_type: str) -> bool:
    raw = event.get("actions")
    if not isinstance(raw, list):
        return False
    return any(
        isinstance(action, Mapping) and action.get("type") == action_type
        for action in raw
    )


def _optional_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _positive_int(value: Any, *, default: int) -> int:
    if isinstance(value, int) and value > 0:
        return value
    if isinstance(value, str) and value.isdigit() and int(value) > 0:
        return int(value)
    return default


def _float_or_none(value: Any) -> float | None:
    if isinstance(value, int | float):
        return float(value)
    return None


def _cycle_outcome(coder: CodingAttempt, review: ReviewStage) -> OutcomeBadge:
    """Project a cycle outcome from typed coder/review state.

    Returns a typed ``OutcomeBadge`` whose ``label`` is the canonical
    lifecycle string (``approved``, ``changes_requested``,
    ``completed``, ``in_progress``, etc.) and whose ``tone`` reflects
    the visual semantic (passed/failed/error/in_progress/neutral).

    Tone classification is owned at the projection layer (PR #6333
    blocker) so the UI never has to string-match against these
    labels.
    """
    review_outcome = _review_outcome(review)
    if review_outcome is not None:
        return review_outcome
    return _coder_outcome(coder)


def _review_outcome(review: ReviewStage) -> OutcomeBadge | None:
    if isinstance(review, ReviewApproved):
        return OutcomeBadge(label="approved", tone="passed")
    if isinstance(review, ReviewChangesRequested):
        return OutcomeBadge(label="changes_requested", tone="failed")
    if isinstance(review, ReviewFailed):
        return OutcomeBadge(label="review_failed", tone="failed")
    if isinstance(review, ReviewRunning):
        return OutcomeBadge(label="review_in_progress", tone="in_progress")
    if isinstance(review, ReviewSkipped):
        return OutcomeBadge(label="review_skipped", tone="neutral")
    if isinstance(review, MissingReviewEvidence):
        return OutcomeBadge(label="missing_review_evidence", tone="error")
    return None


def _coder_outcome(coder: CodingAttempt) -> OutcomeBadge:
    if isinstance(coder, CompletedCodingAttempt):
        return OutcomeBadge(label="completed", tone="passed")
    if isinstance(coder, RunningCodingAttempt):
        return OutcomeBadge(label="in_progress", tone="in_progress")
    if isinstance(coder, BlockedCodingAttempt):
        return OutcomeBadge(label="blocked", tone="failed")
    if isinstance(coder, PublishFailedCodingAttempt):
        return OutcomeBadge(label="publish_failed", tone="failed")
    if isinstance(coder, FailedCodingAttempt):
        return OutcomeBadge(label="failed", tone="failed")
    if isinstance(coder, MissingCodingEvidence):
        return OutcomeBadge(label="missing_coding_evidence", tone="error")
    return OutcomeBadge(label="unknown", tone="neutral")


__all__ = [
    "BLOCKED_EVENT_NAMES",
    "CODING_TERMINAL_EVENTS",
    "LifecycleProjectionError",
    "OUTCOME_EVENTS",
    "VALIDATION_FAILED_EVENTS",
    "VALIDATION_PASSED_EVENTS",
    "project_dashboard_lifecycle_container",
    "project_e2e_run_iteration",
    "project_e2e_suite_lifecycle_container_for_run",
    "project_e2e_suite_lifecycle_container",
    "project_issue_lifecycles_from_events",
    "project_issue_lifecycle",
    "require_lifecycle_container_valid",
]
