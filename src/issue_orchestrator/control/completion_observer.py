"""Completion observer - detects and reads session completions (fast, no I/O beyond file read).

This module is responsible for the OBSERVATION phase of completion handling:
1. Detect when a session has terminated
2. Read and parse the completion.json file
3. Return an ObservedCompletion fact

This module does NOT:
- Execute git operations (push, rebase, branch)
- Create PRs
- Post comments
- Mutate labels
- Run validation

Those actions are performed by the PublishJobExecutor in background threads.

Architecture:
    Observation (fast) -> Planning (fast) -> Execution (background)
    ^^^^^^^^^^^^^^^
    This module handles observation only.
"""

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from ..domain.completion_finalization import (
    CompletionFinalizationDecision,
    CompletionFinalizationPlan,
    CompletionRuntimeState,
)
from ..domain.models import (
    CompletionRecord,
    CompletionOutcome,
    ObservedCompletion,
    RequestedAction,
    Session,
    SessionIdentity,
    SessionStatus,
    WorktreeLocation,
    COMPLETION_RECORD_PATH,
)
from ..infra.logging_config import issue_log
from ..infra.provider_resilience import ProviderStatus, read_provider_status, now_iso
from ..ports.provider_resilience import ProviderErrorType
from ..observation.observation import SessionObservation, SessionObservationResult
from .completion_record_validation import (
    CompletionRecordLoadResult,
    load_completion_record_result,
)
from .session_run_resolution import resolve_session_run_dir

if TYPE_CHECKING:
    from ..ports.session_output import SessionOutput

logger = logging.getLogger(__name__)


@dataclass
class ObservationDecision:
    """Result of observing a session's completion state.

    This is a FACT about what was observed, not a decision about what to do.
    The planner will decide on actions based on these facts.
    """

    # The decided status based on observation
    status: SessionStatus

    # Observed completion data (if completion.json was found and valid)
    observed: ObservedCompletion | None = None

    # Whether this was a recovered timeout (timeout but completion.json existed)
    recovered_from_timeout: bool = False

    # Reason for the decision (for logging/debugging)
    reason: str = ""

    # Session log tail (for failed/timeout sessions without completion.json)
    session_log_tail: str = ""

    # Provider status (if available)
    provider_status: ProviderStatus | None = None

    # Typed load result when a completion record was checked. Invalid present
    # records must not collapse into the same fact as a missing record.
    completion_load_result: CompletionRecordLoadResult | None = None


class CompletionFinalizationOwner(Protocol):
    """Owns the bounded completion-finalization policy for a completed record.

    Implemented by ``CompletionProcessor``. The async observer routes every
    finalization decision through this single owner instead of re-implementing
    the matrix, so it stays in parity with the synchronous
    ``SessionController.decide_outcome`` path (issue #6009):

    - ``completion_finalization_plan`` returns the next safe finalization step.
      A terminated *or* timed-out completion whose background review exchange is
      still inside its own supervisor deadline yields ``DEFER_REVIEW_EXCHANGE``,
      which the observer maps to ``SessionStatus.RUNNING``.
    - ``cancel_deferred_review_exchange`` tears the hidden background job down
      when the plan returns ``TERMINAL_REVIEW_EXCHANGE_TIMEOUT``, so a timed-out
      exchange that has overshot its deadline is halted instead of being treated
      as a recovered completion.
    """

    def completion_finalization_plan(
        self,
        *,
        issue_number: int,
        session_name: str | None,
        outcome: CompletionOutcome,
        requested_actions: tuple[RequestedAction, ...],
        runtime_state: CompletionRuntimeState,
        validation_preflight_configured: bool,
    ) -> CompletionFinalizationPlan:
        ...

    def cancel_deferred_review_exchange(
        self,
        *,
        issue_number: int,
        session_name: str | None,
        reason: str,
    ) -> str | None:
        ...


class CompletionObserver:
    """Observes session completions without executing actions.

    This is the observation component that:
    1. Checks if a session has terminated
    2. Reads completion.json if present
    3. Returns facts about what was observed

    The observer has NO AUTHORITY to modify state - it only gathers facts.
    """

    def __init__(
        self,
        session_output: "SessionOutput",
        finalization_owner: CompletionFinalizationOwner,
    ):
        """Initialize the observer.

        Args:
            session_output: For reading session logs (diagnostics)
            finalization_owner: Owns the bounded completion-finalization policy
                (same owner the synchronous ``decide_outcome`` path uses), so a
                deferred completion keeps its session active and a terminal
                review-exchange timeout halts the hidden job instead of being
                recovered as a normal completion (#6009).
        """
        self.session_output = session_output
        self._finalization_owner = finalization_owner

    def observe_completion(
        self,
        session: Session,
        observation: SessionObservationResult,
    ) -> ObservationDecision:
        """Observe a session and determine its completion state.

        This is a PURE function - it only reads data and returns facts.
        It does NOT execute any actions.

        Args:
            session: The session to observe
            observation: The terminal observation result (running, terminated, timed_out)

        Returns:
            ObservationDecision with facts about the session's state
        """
        issue_number = session.issue.number

        # If still running, nothing to observe
        if observation.observation == SessionObservation.RUNNING:
            logger.debug(
                issue_log(issue_number, "Session still running: session=%s"),
                session.terminal_id,
            )
            return ObservationDecision(
                status=SessionStatus.RUNNING,
                reason="Session still running",
            )

        # Session has terminated - try to read completion record
        load_result = self._read_completion_record_result(
            session.worktree_path,
            session.completion_path,
            issue_number,
        )
        record = load_result.record

        if record is None:
            return self._handle_no_completion_record(
                session,
                observation,
                issue_number,
                completion_load_result=load_result,
            )

        # Route the record through the completion-finalization owner. A
        # DEFER_REVIEW_EXCHANGE keeps the session RUNNING; a terminal review
        # exchange timeout halts the hidden job and finalizes as a failure.
        # Mirrors the synchronous decide_outcome path instead of re-deciding.
        finalization = self._resolve_finalization(
            session, observation, record, load_result
        )
        if finalization is not None:
            return finalization

        # Found completion record - build observed completion
        recovered = observation.observation == SessionObservation.TIMED_OUT
        if recovered:
            logger.info(
                issue_log(
                    issue_number,
                    "Session timed out but has completion.json - recovering work: outcome=%s",
                ),
                record.outcome.value,
            )

        observed = self._build_observed_completion(session, record)
        status = self._map_outcome_to_status(record)

        return ObservationDecision(
            status=status,
            observed=observed,
            recovered_from_timeout=recovered,
            reason=f"Observed completion record with outcome: {record.outcome.value}",
            completion_load_result=load_result,
        )

    def _resolve_finalization(
        self,
        session: Session,
        observation: SessionObservationResult,
        record: CompletionRecord,
        load_result: CompletionRecordLoadResult,
    ) -> ObservationDecision | None:
        """Map the completion-finalization plan to an observation decision.

        Routes every ``CompletionFinalizationDecision`` through the same owner
        as the synchronous ``decide_outcome`` path instead of re-deciding here,
        and handles each branch intentionally so no policy cell stays implicit:

        - ``DEFER_REVIEW_EXCHANGE`` → keep the session ``RUNNING`` so the next
          tick re-observes it (a terminated-and-running exchange, or a timed-out
          session whose background exchange is still inside its supervisor
          deadline).
        - ``TERMINAL_REVIEW_EXCHANGE_TIMEOUT`` → the timed-out exchange has
          overshot its deadline; halt the hidden job and finalize as a failure
          (see ``_terminal_review_exchange_timeout``).
        - ``PROCESS`` / ``RUN_DIRTY_PREFLIGHT`` → ``None``; the caller builds the
          observed completion and the background publish phase owns dirty
          preflight and validation.
        - any other decision → ``AssertionError`` (fail fast, mirroring the
          synchronous controller) so future enum growth cannot silently fall
          through to a normal observed/publish completion.

        ``validation_preflight_configured`` is ``False`` here: dirty preflight
        and validation are owned by the publish phase, not observation, and that
        flag never affects the defer/terminal decision (it only selects between
        ``RUN_DIRTY_PREFLIGHT`` and ``PROCESS``, both of which finalize here).
        """
        runtime_state = (
            CompletionRuntimeState.TIMED_OUT
            if observation.observation is SessionObservation.TIMED_OUT
            else CompletionRuntimeState.TERMINATED
        )
        plan = self._finalization_owner.completion_finalization_plan(
            issue_number=session.issue.number,
            session_name=session.terminal_id,
            outcome=record.outcome,
            requested_actions=tuple(record.requested_actions),
            runtime_state=runtime_state,
            validation_preflight_configured=False,
        )
        if plan.decision is CompletionFinalizationDecision.DEFER_REVIEW_EXCHANGE:
            logger.info(
                issue_log(
                    session.issue.number,
                    "Completion deferred: review exchange running in background "
                    "(session=%s)",
                ),
                session.terminal_id,
            )
            return ObservationDecision(
                status=SessionStatus.RUNNING,
                reason="Review exchange running in background; awaiting completion",
                completion_load_result=load_result,
            )
        if (
            plan.decision
            is CompletionFinalizationDecision.TERMINAL_REVIEW_EXCHANGE_TIMEOUT
        ):
            return self._terminal_review_exchange_timeout(
                session, plan, load_result
            )
        if plan.decision in (
            CompletionFinalizationDecision.PROCESS,
            CompletionFinalizationDecision.RUN_DIRTY_PREFLIGHT,
        ):
            # Both finalize via the normal observed-completion path; the
            # background publish phase owns dirty preflight and validation.
            return None
        # Fail fast on any unhandled decision, mirroring the synchronous
        # controller's exhaustiveness, so future enum expansion or an owner
        # behavior change cannot silently become a normal observed/publish
        # completion in the async observer.
        raise AssertionError(
            f"Unhandled completion finalization decision: {plan.decision}"
        )

    def _terminal_review_exchange_timeout(
        self,
        session: Session,
        plan: CompletionFinalizationPlan,
        load_result: CompletionRecordLoadResult,
    ) -> ObservationDecision:
        """Finalize a timed-out session whose review exchange overshot its budget.

        Parity with ``SessionController._terminal_review_exchange_timeout_decision``:
        a visible timeout is an issue-lifetime boundary, so the hidden
        background review exchange is cancelled via the finalization owner
        before the active session is removed. The result is a terminal
        ``TIMED_OUT`` failure with **no** ``ObservedCompletion`` — the observer
        must not enqueue a publish job, because the publish worker would only
        return another ``review_exchange_deferred`` with no PR while the active
        session is already gone, leaving the issue stuck with no later
        observation tick to surface the timeout.
        """
        issue_number = session.issue.number
        cancel_error = self._finalization_owner.cancel_deferred_review_exchange(
            issue_number=issue_number,
            session_name=session.terminal_id,
            reason="session-timeout",
        )
        reason = plan.reason
        if cancel_error:
            reason = f"{reason}; {cancel_error}"
        logger.warning(
            issue_log(
                issue_number,
                "Review exchange terminal timeout; halting hidden job and "
                "finalizing session=%s reason=%s",
            ),
            session.terminal_id,
            reason,
        )
        return ObservationDecision(
            status=SessionStatus.TIMED_OUT,
            observed=None,
            recovered_from_timeout=True,
            reason=reason,
            completion_load_result=load_result,
        )

    def _read_completion_record(
        self,
        worktree: Path,
        completion_path: str | None,
        issue_number: int,
    ) -> CompletionRecord | None:
        """Read and validate a completion record from a worktree.

        Delegates to the typed completion-record loader so the per-file size
        gate and field bounds apply uniformly on both observation and publish
        paths, while preserving invalid-vs-missing classification.
        """
        return self._read_completion_record_result(
            worktree=worktree,
            completion_path=completion_path,
            issue_number=issue_number,
        ).record

    def _read_completion_record_result(
        self,
        worktree: Path,
        completion_path: str | None,
        issue_number: int,
    ) -> CompletionRecordLoadResult:
        """Read and classify a completion record from a worktree."""
        record_path = worktree / (completion_path or COMPLETION_RECORD_PATH)
        logger.info(
            issue_log(issue_number, "Checking completion: path=%s exists=%s"),
            record_path,
            record_path.exists(),
        )
        return load_completion_record_result(record_path)

    def _handle_no_completion_record(
        self,
        session: Session,
        observation: SessionObservationResult,
        issue_number: int,
        *,
        completion_load_result: CompletionRecordLoadResult | None = None,
    ) -> ObservationDecision:
        """Handle case where no completion record exists.

        Returns facts about the failure - does not take any actions.
        """
        session_log = self._get_session_log_tail(session)
        provider_status = self._read_provider_status(session)
        load_result = completion_load_result
        if load_result is not None and load_result.invalid:
            error = load_result.error or "Completion record rejected"
            failure = load_result.failure
            reason = f"Completion record rejected: {error}"
            logger.error(
                issue_log(
                    issue_number,
                    "Session completion record rejected: session=%s failure=%s error=%s",
                ),
                session.terminal_id,
                failure.value if failure else "unknown",
                error,
            )
            return ObservationDecision(
                status=SessionStatus.FAILED,
                reason=reason,
                session_log_tail=session_log,
                provider_status=provider_status,
                completion_load_result=load_result,
            )

        if (
            provider_status
            and provider_status.error_type == ProviderErrorType.TRANSIENT
            and not provider_status.succeeded
        ):
            record = CompletionRecord(
                session_id=session.terminal_id,
                timestamp=now_iso(),
                outcome=CompletionOutcome.BLOCKED,
                summary="Provider unavailable after retries",
                blocked_reason="provider_unavailable",
            )
            observed = self._build_observed_completion(session, record)
            return ObservationDecision(
                status=SessionStatus.BLOCKED,
                observed=observed,
                reason="Provider unavailable",
                provider_status=provider_status,
                completion_load_result=completion_load_result,
            )

        if observation.observation == SessionObservation.TIMED_OUT:
            logger.warning(
                issue_log(
                    issue_number,
                    "Session timed out without completion record: session=%s",
                ),
                session.terminal_id,
            )
            if session_log:
                logger.warning(
                    issue_log(issue_number, "LAST OUTPUT:\n%s"), session_log
                )
            return ObservationDecision(
                status=SessionStatus.TIMED_OUT,
                reason="Timed out without completion record",
                session_log_tail=session_log,
                provider_status=provider_status,
                completion_load_result=completion_load_result,
            )

        # Session terminated without completion record = failed
        logger.error(
            issue_log(
                issue_number,
                "Session terminated without completion record: session=%s",
            ),
            session.terminal_id,
        )
        if session_log:
            logger.error(issue_log(issue_number, "LAST OUTPUT:\n%s"), session_log)
        return ObservationDecision(
            status=SessionStatus.FAILED,
            reason="Terminated without completion record",
            session_log_tail=session_log,
            provider_status=provider_status,
            completion_load_result=completion_load_result,
        )

    def _read_provider_status(self, session: Session) -> ProviderStatus | None:
        run_dir = resolve_session_run_dir(self.session_output, session)
        return read_provider_status(run_dir)

    def _get_session_log_tail(self, session: Session) -> str:
        """Get last 50 lines of session log for diagnostics."""
        run_dir = resolve_session_run_dir(self.session_output, session)
        log_path = self.session_output.get_log_path_for_run_dir(run_dir)
        if not (log_path and log_path.exists()):
            return ""
        try:
            content = log_path.read_text()
            lines = content.strip().split("\n")
            return "\n".join(lines[-50:])
        except Exception as e:
            logger.debug("Could not read session log: %s", e)
            return ""

    def _build_observed_completion(
        self,
        session: Session,
        record: CompletionRecord,
    ) -> ObservedCompletion:
        """Build an ObservedCompletion fact from session and record.

        This packages all the data needed for:
        1. The planner to project labels
        2. The job executor to run publish actions
        """
        # Extract PR number from session name for review sessions
        pr_number = self._extract_pr_number_from_session_name(session.terminal_id)

        return ObservedCompletion(
            identity=SessionIdentity(
                issue_number=session.issue.number,
                issue_title=session.issue.title,
                session_key=session.key.stable_id(),
                terminal_id=session.terminal_id,
                issue_key=session.issue.key.stable_id(),
            ),
            worktree=WorktreeLocation(
                path=str(session.worktree_path),
                branch_name=session.branch_name,
                completion_path=session.completion_path,
            ),
            record=record,
            run_assets=session.run_assets,
            pr_number=pr_number,
            agent_label=session.agent_label,
            validation_retry_count=session.validation_retry_count,
            original_prompt=session.original_prompt,
        )

    def _extract_pr_number_from_session_name(self, session_name: str) -> int | None:
        """Extract PR number from review session name."""
        if not session_name.startswith("review-"):
            return None
        try:
            pr_number = int(session_name.replace("review-", ""))
            logger.debug("Review session detected, PR number: %d", pr_number)
            return pr_number
        except ValueError:
            logger.warning(
                "Could not parse PR number from session name: %s", session_name
            )
            return None

    def _map_outcome_to_status(self, record: CompletionRecord) -> SessionStatus:
        """Map completion outcome to session status."""
        outcome_to_status = {
            CompletionOutcome.COMPLETED: SessionStatus.COMPLETED,
            CompletionOutcome.BLOCKED: SessionStatus.BLOCKED,
            CompletionOutcome.NEEDS_HUMAN: SessionStatus.NEEDS_HUMAN,
            CompletionOutcome.REVIEW_APPROVED: SessionStatus.COMPLETED,
            CompletionOutcome.REVIEW_CHANGES_REQUESTED: SessionStatus.COMPLETED,
        }
        return outcome_to_status.get(record.outcome, SessionStatus.FAILED)
