"""Issue-scoped runtime lifecycle helpers.

This module is the behavior owner for issue terminal boundaries that must
tear down hidden review-exchange work and, when requested, visible issue/rework
terminal sessions. Call sites should use these helpers instead of directly
reaching into pair registries, background job supervisors, or session managers.
"""

from __future__ import annotations

from pathlib import Path

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Iterable, Protocol

from ..ports.issue_run_evidence import IssueRunEvidenceSource
from ..ports.validated_work_preservation import ValidatedWorkPreservation
from ..domain.validated_work_commands import AutomaticCaptureScope, AutomaticCaptureCommand, ValidatedWorkDispositionBatch
from enum import StrEnum
from ..events import EventName
from ..ports.event_sink import EventSink, make_trace_event
from ..domain.validated_work_observation import disposition_observation
from ..ports.session_runner import SessionRunner
from .background_job_supervisor import drain_background_jobs
from ..domain.session_key import TaskKind
from ..domain.session_run import SessionRunAssets
from ..domain.issue_run_evidence import IssueRunEvidence
from ..domain.tech_lead_session import TechLeadSessionGeneration
from .completion_review_exchange import is_review_exchange_job_for_issue

if TYPE_CHECKING:
    from ..domain.models import Session
    from ..ports.persistent_exchange_pair_registry import (
        PersistentExchangePairRegistry,
    )
    from .background_job_supervisor import BackgroundJobSupervisor
    from .session_manager import SessionManager, SessionRef


class PublishRetryAbandoner(Protocol):
    """Issue-scoped publish-retry teardown seam for the runtime terminator.

    ``PublishRecoveryService`` implements this structurally. Publish-retry work
    runs on its own owner/runner outside the review-exchange supervisor, so the
    shared issue-runtime boundary must abandon it explicitly or a late republish
    could repopulate an already-terminated issue.
    """

    def abandon_issue(self, issue_number: int) -> None: ...


class IssuePublishRetryRuntime(PublishRetryAbandoner, Protocol):
    """The publish-retry owner as seen by the shared issue-runtime boundary.

    Extends the teardown seam (:class:`PublishRetryAbandoner`) with the
    non-mutating activity query the reset-freshness predicate needs, so the
    activity check and the abandon it guards read/mutate the exact same owner
    through one contract and cannot drift.
    """

    def has_active_retry(self, issue_number: int) -> bool: ...


from .session_manager import SessionType

logger = logging.getLogger(__name__)


ISSUE_RUNTIME_SESSION_TYPES = (SessionType.ISSUE, SessionType.REWORK)


@dataclass(frozen=True)
class ReviewExchangeCancellation:
    """Result of cancelling review-exchange work for one issue."""

    issue_number: int
    cancelled_job_ids: tuple[str, ...]
    validated_work: ValidatedWorkDispositionBatch


@dataclass(frozen=True)
class IssueRuntimeTermination:
    """Result of applying an issue-scoped runtime lifecycle boundary."""

    issue_number: int
    review_exchange: ReviewExchangeCancellation
    stopped_session_ids: tuple[str, ...]
    cleared_active_session_ids: tuple[str, ...]
    validated_work: ValidatedWorkDispositionBatch

    @property
    def cancelled_job_ids(self) -> tuple[str, ...]:
        return self.review_exchange.cancelled_job_ids


@dataclass(frozen=True)
class GenerationBoundTermination:
    """Exact-generation kill result: one mutation or a fail-closed stale reason."""

    termination: IssueRuntimeTermination | None = None
    stale_reason: str | None = None

    def __post_init__(self) -> None:
        if (self.termination is None) == (self.stale_reason is None):
            raise ValueError(
                "generation-bound termination requires exactly one outcome"
            )


class GenerationTerminationPartialFailure(RuntimeError):
    """The exact terminal stopped, but its lifecycle acknowledgement failed."""

    def __init__(self, target: TechLeadSessionGeneration, cause: Exception, validated_work: ValidatedWorkDispositionBatch) -> None:
        self.validated_work = validated_work
        self.target = target
        self.cause = cause
        super().__init__(
            f"{target.task_kind.value} terminal {target.terminal_id} "
            f"(run {target.run_id}) stopped, but its stop owner raised after "
            f"commit: {cause}"
        )


def _cancel_issue_review_exchange(
    *,
    validated_work: ValidatedWorkDispositionBatch,
    issue_number: int,
    reason: str,
    pair_registry: "PersistentExchangePairRegistry | None",
    job_supervisor: "BackgroundJobSupervisor | None",
) -> ReviewExchangeCancellation:
    """Terminate persistent pair resources and stop supervised polling.

    Review exchange work has two owners: the pair registry owns the persistent
    coder/reviewer processes, and the background supervisor owns the async job
    status observed by the main tick. Operator cancellation must touch both or
    the visible issue session can stop while a hidden exchange continues to
    report "still running".
    """
    errors: list[Exception] = []
    if pair_registry is not None:
        try:
            pair_registry.release(issue_number, reason=reason)
        except Exception as exc:
            errors.append(exc)

    cancelled: tuple[str, ...] = ()
    if job_supervisor is not None:
        try:
            cancelled = tuple(
                job_supervisor.cancel_matching(
                    lambda job_id: is_review_exchange_job_for_issue(
                        job_id, issue_number
                    ),
                    reason=reason,
                )
            )
        except Exception as exc:
            errors.append(exc)
    if errors:
        _raise_lifecycle_errors(
            f"failed to cancel all review-exchange owners for issue #{issue_number}",
            errors,
        )
    if pair_registry is not None or cancelled:
        logger.info(
            "[REVIEW_EXCHANGE] cancelled issue=%d reason=%s jobs=%s",
            issue_number,
            reason,
            ",".join(cancelled) if cancelled else "none",
        )
    return ReviewExchangeCancellation(
        issue_number=issue_number,
        cancelled_job_ids=cancelled,
        validated_work=validated_work,
    )


def _release_issue_runtime(
    *,
    issue_number: int,
    validated_work: ValidatedWorkDispositionBatch,
    reason: str,
    pair_registry: "PersistentExchangePairRegistry | None",
    job_supervisor: "BackgroundJobSupervisor | None",
    session_manager: "SessionManager | None" = None,
    active_sessions: list["Session"] | None = None,
    publish_recovery: "PublishRetryAbandoner | None" = None,
    session_types: Iterable[SessionType] = ISSUE_RUNTIME_SESSION_TYPES,
) -> IssueRuntimeTermination:
    """Apply an issue terminal boundary to every issue-scoped runtime owner.

    The review exchange pair/job is always checked. Visible ``issue-N`` and
    ``rework-N`` sessions are stopped when a ``SessionManager`` is supplied.
    If an active-session registry is supplied, stale records for already-gone
    issue/rework sessions are cleared in the same boundary so queue eligibility
    does not remain blocked by a dead terminal. When a ``PublishRetryAbandoner``
    is supplied, any in-flight/stored publish retry for the issue is abandoned in
    the same boundary so a late republish cannot repopulate a terminated issue.
    """
    refs = tuple(_issue_runtime_session_refs(issue_number, session_types))
    active_names = _active_session_names(active_sessions)
    matching_active = active_names.intersection(ref.name for ref in refs)
    if session_manager is None and matching_active:
        raise RuntimeError(
            "cannot terminate active issue runtime sessions without a "
            f"SessionManager: issue={issue_number} sessions={sorted(matching_active)}"
        )

    review_exchange = _cancel_issue_review_exchange(
        validated_work=validated_work,        issue_number=issue_number,
        reason=reason,
        pair_registry=pair_registry,
        job_supervisor=job_supervisor,
    )

    if publish_recovery is not None:
        # Publish-retry work has its own owner/runner outside the review-exchange
        # supervisor, so terminate it on the same boundary. Idempotent: a no-op
        # when the issue has no stored/in-flight retry.
        publish_recovery.abandon_issue(issue_number)

    stopped: list[str] = []
    stale: list[str] = []
    if session_manager is not None:
        for ref in refs:
            if session_manager.exists(ref):
                session_manager.stop(ref)
                stopped.append(ref.name)
            elif ref.name in matching_active:
                stale.append(ref.name)

    terminal_ids_to_clear = _active_session_ids_to_clear(
        active_sessions,
        set(stopped).union(stale),
    )
    _drop_active_session_records(active_sessions, terminal_ids_to_clear)
    if stopped or terminal_ids_to_clear:
        logger.info(
            "[ISSUE_RUNTIME] terminated issue=%d reason=%s stopped=%s cleared=%s",
            issue_number,
            reason,
            ",".join(stopped) if stopped else "none",
            ",".join(terminal_ids_to_clear) if terminal_ids_to_clear else "none",
        )
    return IssueRuntimeTermination(
        issue_number=issue_number,
        review_exchange=review_exchange,
        stopped_session_ids=tuple(stopped),
        cleared_active_session_ids=terminal_ids_to_clear,
        validated_work=validated_work,
    )


def _terminate_issue_session_generation(
    *,
    target: TechLeadSessionGeneration,
    reason: str,
    preserve: Callable[[int, str], ValidatedWorkDispositionBatch],
    active_sessions: list["Session"],
    session_exists: Callable[[str], bool],
    kill_session: Callable[[str], None],
    pair_registry: "PersistentExchangePairRegistry | None",
    job_supervisor: "BackgroundJobSupervisor | None",
    publish_recovery: "PublishRetryAbandoner | None" = None,
) -> GenerationBoundTermination:
    """Conditionally stop the exact launch-observed worker generation.

    Capture, live eligibility, generation comparison, and mutation meet at
    this single behavior boundary. Only CODE/REWORK sessions participate. A
    missing, replacement, review-only, or ambiguous runtime returns a stale
    outcome without touching any terminal or hidden owner.
    """
    candidates = [
        session
        for session in active_sessions
        if session.issue.number == target.issue_number
        and session.key.task in {TaskKind.CODE, TaskKind.REWORK}
    ]
    if not candidates:
        return GenerationBoundTermination(
            stale_reason=(
                f"issue #{target.issue_number} has no active killable session; "
                "the observed generation is already gone"
            )
        )
    if len(candidates) != 1:
        return GenerationBoundTermination(
            stale_reason=(
                f"issue #{target.issue_number} has {len(candidates)} active "
                "killable sessions; refusing an ambiguous termination"
            )
        )
    current = candidates[0]
    if (
        current.key.task is not target.task_kind
        or current.terminal_id != target.terminal_id
        or current.run_assets.run_id != target.run_id
    ):
        return GenerationBoundTermination(
            stale_reason=(
                f"issue #{target.issue_number}'s live generation "
                f"({current.key.task.value} terminal {current.terminal_id}, "
                f"run {current.run_assets.run_id}) is not the observed generation "
                f"({target.task_kind.value} terminal {target.terminal_id}, "
                f"run {target.run_id}); refusing to kill a replacement"
            )
        )
    if not session_exists(target.terminal_id):
        return GenerationBoundTermination(
            stale_reason=(
                f"issue #{target.issue_number}'s observed terminal "
                f"{target.terminal_id} is no longer running"
            )
        )

    validated_work = preserve(target.issue_number, reason)

    # Prepare every hidden owner before committing the visible terminal stop.
    # Each teardown is idempotent, and every owner is attempted even when a
    # sibling fails. A preparation failure deliberately leaves the terminal and
    # exact active row intact so the action remains retryable. Once the terminal
    # stop commits, no fallible external cleanup remains before reconciliation.
    cleanup_errors: list[Exception] = []
    review_exchange: ReviewExchangeCancellation | None = None
    try:
        review_exchange = _cancel_issue_review_exchange(
        validated_work=validated_work,            issue_number=target.issue_number,
            reason=reason,
            pair_registry=pair_registry,
            job_supervisor=job_supervisor,
        )
    except Exception as exc:
        cleanup_errors.append(exc)
    if publish_recovery is not None:
        try:
            publish_recovery.abandon_issue(target.issue_number)
        except Exception as exc:
            cleanup_errors.append(exc)
    if cleanup_errors:
        _raise_lifecycle_errors(
            f"failed to prepare all runtime owners for issue #{target.issue_number}",
            cleanup_errors,
        )
    assert review_exchange is not None

    # The orchestrator serializes lifecycle mutations; this opaque terminal id
    # is exactly the one retained on the generation-matched Session record.
    _stop_exact_generation(
        target=target,
        active_sessions=active_sessions,
        session_exists=session_exists,
        kill_session=kill_session,
        validated_work=validated_work,
    )
    termination = IssueRuntimeTermination(
        issue_number=target.issue_number,
        review_exchange=review_exchange,
        stopped_session_ids=(target.terminal_id,),
        cleared_active_session_ids=(target.terminal_id,),
        validated_work=validated_work,
    )
    return GenerationBoundTermination(termination=termination)


def _raise_lifecycle_errors(message: str, errors: list[Exception]) -> None:
    """Raise one lifecycle failure directly, or preserve all sibling failures."""
    if len(errors) == 1:
        raise errors[0]
    raise ExceptionGroup(message, errors)


def _drop_exact_generation(
    active_sessions: list["Session"], target: TechLeadSessionGeneration
) -> None:
    """Reconcile only the active row proven to represent the stopped generation."""
    active_sessions[:] = [
        session
        for session in active_sessions
        if not (
            session.issue.number == target.issue_number
            and session.key.task is target.task_kind
            and session.terminal_id == target.terminal_id
            and session.run_assets.run_id == target.run_id
        )
    ]


def _stop_exact_generation(
    *,
    validated_work: ValidatedWorkDispositionBatch,
    target: TechLeadSessionGeneration,
    active_sessions: list["Session"],
    session_exists: Callable[[str], bool],
    kill_session: Callable[[str], None],
) -> None:
    """Stop one exact terminal and reconcile whether a raised stop committed."""
    try:
        kill_session(target.terminal_id)
    except Exception as stop_error:
        try:
            terminal_still_running = session_exists(target.terminal_id)
        except Exception as probe_error:
            raise ExceptionGroup(
                "terminal stop failed and its commit state is unverifiable",
                (stop_error, probe_error),
            ) from stop_error
        if terminal_still_running:
            raise
        _drop_exact_generation(active_sessions, target)
        raise GenerationTerminationPartialFailure(target, stop_error, validated_work) from stop_error

    _drop_exact_generation(active_sessions, target)


def _issue_runtime_session_active(
    issue_number: int,
    session_manager: "SessionManager | None",
    active_sessions: list["Session"] | None,
    session_types: Iterable[SessionType],
) -> bool:
    """True while a visible issue/rework terminal for the issue is live.

    Reads ``active_sessions`` (the registry ``terminate_issue_runtime`` clears)
    and, when supplied, the ``SessionManager`` it stops, so the visible-session
    activity signal matches the terminals the reset would tear down.
    """
    registry_active = any(
        session.issue.number == issue_number for session in (active_sessions or ())
    )
    refs = _issue_runtime_session_refs(issue_number, session_types)
    manager_active = session_manager is not None and any(
        session_manager.exists(ref) for ref in refs
    )
    return registry_active or manager_active


def _issue_runtime_session_refs(
    issue_number: int,
    session_types: Iterable[SessionType],
) -> list["SessionRef"]:
    from .session_manager import SessionRef

    return [
        SessionRef(session_type=session_type, number=issue_number)
        for session_type in session_types
        if session_type in ISSUE_RUNTIME_SESSION_TYPES
    ]


def _active_session_names(active_sessions: list["Session"] | None) -> set[str]:
    if active_sessions is None:
        return set()
    return {session.terminal_id for session in active_sessions}


def _active_session_ids_to_clear(
    active_sessions: list["Session"] | None,
    terminal_ids: set[str],
) -> tuple[str, ...]:
    if active_sessions is None or not terminal_ids:
        return ()
    return tuple(
        session.terminal_id
        for session in active_sessions
        if session.terminal_id in terminal_ids
    )


def _drop_active_session_records(
    active_sessions: list["Session"] | None,
    terminal_ids: tuple[str, ...],
) -> None:
    if active_sessions is None or not terminal_ids:
        return
    terminal_id_set = set(terminal_ids)
    active_sessions[:] = [
        session
        for session in active_sessions
        if session.terminal_id not in terminal_id_set
    ]


def _shutdown_agent_runtime(
    pair_registry: PersistentExchangePairRegistry | None,
    runner: SessionRunner,
    supervisor: BackgroundJobSupervisor | None,
) -> None:
    """Stop subprocess owners before waiting for supervised worker threads."""
    logger.info("[SHUTDOWN] Terminating agent runtime owners")
    if pair_registry is not None:
        pair_registry.shutdown_all(reason="orchestrator-shutdown")
    runner.on_orchestrator_shutdown()
    drain_background_jobs(supervisor, 60.0)
    logger.info("[SHUTDOWN] Agent runtime owners terminated")


class IssueRuntimeOwnerKind(StrEnum):
    SESSIONS = "sessions"
    EXCHANGE_PAIR = "exchange_pair"
    EXCHANGE_JOBS = "exchange_jobs"
    PUBLISH_RETRY = "publish_retry"
    VALIDATED_WORK = "validated_work"


@dataclass(frozen=True, slots=True)
class IssueRuntimeActivity:
    active: frozenset[IssueRuntimeOwnerKind]
    unverifiable: frozenset[IssueRuntimeOwnerKind]

    @property
    def busy(self) -> bool:
        return bool(self.active or self.unverifiable)


def _probe_owners(probes: dict[IssueRuntimeOwnerKind, Callable[[], bool]]) -> IssueRuntimeActivity:
    active: set[IssueRuntimeOwnerKind] = set()
    unverifiable: set[IssueRuntimeOwnerKind] = set()
    for kind, probe in probes.items():
        try:
            if probe():
                active.add(kind)
        except Exception:
            logger.warning("Runtime owner %s is unverifiable", kind, exc_info=True)
            unverifiable.add(kind)
    return IssueRuntimeActivity(frozenset(active), frozenset(unverifiable))


@dataclass(frozen=True, slots=True)
class CoreIssueRuntimeOwners:
    """The one composition of the four runtime owners, shared by every boundary."""

    session_manager: SessionManager
    active_sessions: list[Session]
    pair_registry: PersistentExchangePairRegistry | None
    job_supervisor: BackgroundJobSupervisor | None
    publish_recovery: IssuePublishRetryRuntime

    def probe(self, issue_number: int) -> IssueRuntimeActivity:
        return _probe_owners({
            IssueRuntimeOwnerKind.SESSIONS: lambda: _issue_runtime_session_active(
                issue_number, self.session_manager, self.active_sessions, ISSUE_RUNTIME_SESSION_TYPES),
            IssueRuntimeOwnerKind.EXCHANGE_PAIR: lambda: self.pair_registry is not None and self.pair_registry.has_active_pair(issue_number),
            IssueRuntimeOwnerKind.EXCHANGE_JOBS: lambda: self.job_supervisor is not None and self.job_supervisor.has_matching(
                lambda job_id: is_review_exchange_job_for_issue(job_id, issue_number)),
            IssueRuntimeOwnerKind.PUBLISH_RETRY: lambda: self.publish_recovery.has_active_retry(issue_number),
        })

    def cancel_preserved_exchange(self, issue_number: int, reason: str, validated_work: ValidatedWorkDispositionBatch) -> ReviewExchangeCancellation:
        return _cancel_issue_review_exchange(issue_number=issue_number, reason=reason, validated_work=validated_work,
            pair_registry=self.pair_registry, job_supervisor=self.job_supervisor)

    def release_preserved(self, issue_number: int, reason: str, batch: ValidatedWorkDispositionBatch) -> IssueRuntimeTermination:
        return _release_issue_runtime(issue_number=issue_number, reason=reason, validated_work=batch,
            pair_registry=self.pair_registry, job_supervisor=self.job_supervisor,
            session_manager=self.session_manager, active_sessions=self.active_sessions,
            publish_recovery=self.publish_recovery)


@dataclass(frozen=True, slots=True)
class OtherRuntimeActivity:
    """Structural four-owner view; no caller-supplied exclusion list."""

    core: CoreIssueRuntimeOwners

    def probe(self, issue_number: int) -> IssueRuntimeActivity:
        return self.core.probe(issue_number)


@dataclass(frozen=True, slots=True)
class IssueRuntimeResetSnapshot:
    activity: IssueRuntimeActivity
    validated_work: ValidatedWorkDispositionBatch | None


class UnresolvedValidatedWork(RuntimeError):
    def __init__(self, batch: ValidatedWorkDispositionBatch) -> None:
        self.batch = batch
        details = "; ".join(f"{d.state.value}:{d.evidence_id}:{d.failure}" for d in batch.unresolved_dispositions)
        super().__init__(f"issue #{batch.issue_number} retains unresolved validated work: {details}")


@dataclass(frozen=True, slots=True)
class IssueRuntimeLifecycleOwners:
    core: CoreIssueRuntimeOwners
    validated_work: ValidatedWorkPreservation
    run_evidence: IssueRunEvidenceSource
    events: EventSink

    def _capture(self, issue_number: int, reason: str) -> ValidatedWorkDispositionBatch:
        evidence = self.run_evidence.evidence_for_issue(issue_number)
        return self.validated_work.dispose_at_termination(AutomaticCaptureCommand(issue_number, reason, evidence))

    def _observe(self, batch: ValidatedWorkDispositionBatch) -> None:
        self.events.publish(make_trace_event(EventName.VALIDATED_WORK_DISPOSITION_OBSERVED, disposition_observation(batch)))

    def preserve(self, issue_number: int, reason: str) -> ValidatedWorkDispositionBatch:
        batch = self._capture(issue_number, reason)
        self._observe(batch)
        return batch

    def preserve_named_terminal(self, terminal_id: str, reason: str) -> tuple[ValidatedWorkDispositionBatch, ...]:
        issues = set(self.run_evidence.terminal_issues(terminal_id)).union(
            session.issue.number for session in self.core.active_sessions if session.terminal_id == terminal_id)
        return tuple(self.preserve_terminal(issue, terminal_id, reason) for issue in sorted(issues))

    def preserve_terminal(self, issue_number: int, terminal_id: str, reason: str, *, run: SessionRunAssets | None = None) -> ValidatedWorkDispositionBatch:
        evidence = self.run_evidence.terminal_evidence(issue_number, terminal_id, run)
        return self._preserve_selected(evidence, reason)

    def _preserve_selected(self, evidence: IssueRunEvidence, reason: str) -> ValidatedWorkDispositionBatch:
        batch = self.validated_work.dispose_at_termination(AutomaticCaptureCommand(
            evidence.issue_number, reason, evidence, AutomaticCaptureScope.SELECTED_RUNS))
        self._observe(batch)
        return batch

    def preserve_cleanup(self, issue_number: int, terminal_id: str, worktree_path: Path | None, reason: str) -> ValidatedWorkDispositionBatch:
        if worktree_path is None:
            return self.preserve_terminal(issue_number, terminal_id, reason)
        for issue in set(self.run_evidence.issues_for_worktree(worktree_path)) | {issue_number}:
            self._preserve_selected(self.run_evidence.worktree_evidence(issue, worktree_path), reason)
        return self.validated_work.for_issue(issue_number)

    def terminate(self, issue_number: int, reason: str) -> IssueRuntimeTermination:
        batch = self._capture(issue_number, reason)
        result = self.core.release_preserved(issue_number, reason, batch)
        self._observe(batch)
        return result

    def cancel_exchange(self, issue_number: int, reason: str) -> ReviewExchangeCancellation:
        batch = self._capture(issue_number, reason)
        result = self.core.cancel_preserved_exchange(issue_number, reason, batch)
        self._observe(batch)
        return result

    def require_reset(self, issue_number: int, reason: str) -> ValidatedWorkDispositionBatch:
        batch = self.preserve(issue_number, reason)
        if batch.unresolved:
            raise UnresolvedValidatedWork(batch)
        return batch

    def probe(self, issue_number: int) -> IssueRuntimeActivity:
        core = self.core.probe(issue_number)
        work = _probe_owners({IssueRuntimeOwnerKind.VALIDATED_WORK: lambda: self.validated_work.has_unresolved_work(issue_number)})
        return IssueRuntimeActivity(core.active | work.active, core.unverifiable | work.unverifiable)

    def reset_snapshot(self, issue_number: int) -> IssueRuntimeResetSnapshot:
        core = self.core.probe(issue_number)
        try:
            batch = self.validated_work.for_issue(issue_number)
        except Exception:
            return IssueRuntimeResetSnapshot(IssueRuntimeActivity(core.active,
                core.unverifiable | {IssueRuntimeOwnerKind.VALIDATED_WORK}), None)
        active = core.active | ({IssueRuntimeOwnerKind.VALIDATED_WORK} if batch.unresolved else set())
        return IssueRuntimeResetSnapshot(IssueRuntimeActivity(frozenset(active), core.unverifiable), batch)

    def preserve_worktree(self, path: Path, reason: str) -> tuple[ValidatedWorkDispositionBatch, ...]:
        issues = self.run_evidence.issues_for_worktree(path)
        if not issues:
            raise RuntimeError(f"No trusted run ownership for worktree {path}")
        return tuple(self._preserve_selected(self.run_evidence.worktree_evidence(issue, path), reason) for issue in issues)

    def has_active_issue_runtime(self, issue_number: int) -> bool:
        return self.probe(issue_number).busy

    def terminate_generation(self, target: TechLeadSessionGeneration, reason: str, *, session_exists: Callable[[str], bool], kill_session: Callable[[str], None]) -> GenerationBoundTermination:
        return _terminate_issue_session_generation(target=target, reason=reason, preserve=self.preserve,
            active_sessions=self.core.active_sessions, session_exists=session_exists, kill_session=kill_session,
            pair_registry=self.core.pair_registry, job_supervisor=self.core.job_supervisor,
            publish_recovery=self.core.publish_recovery)

    def shutdown(self, runner: SessionRunner) -> None:
        # Freeze every retained issue before the first global subprocess teardown.
        for issue_number in set(self.run_evidence.issue_numbers()).union(session.issue.number for session in self.core.active_sessions):
            self.preserve(issue_number, "orchestrator-shutdown")
        _shutdown_agent_runtime(self.core.pair_registry, runner, self.core.job_supervisor)
