"""Session completion flow coordination.

This module owns the post-launch session lifecycle: interpreting terminal
outcomes, applying completion policy actions, recording cleanup work, releasing
claims, and preserving failure diagnostics. Session launch setup stays in
``session_launcher``.
"""

import logging
import time
from typing import TYPE_CHECKING, Any, Callable, Optional

from ..domain.issue_key import GitHubIssueKey, IssueKey
from ..domain.models import Session, SessionStatus
from ..events import EventName
from ..infra.config import Config
from ..ports import EventSink
from ..ports.event_sink import make_trace_event
from ..ports.session_output import SessionOutput
from ..ports.worktree_manager import WorktreeManager
from .active_sessions import has_active_terminal
from .session_completion_diagnostics import run_session_analysis, surface_failure_context
from .transition_log import log_transition

if TYPE_CHECKING:
    from ..domain.models import OrchestratorState
    from ..observation.observer import SessionObserver
    from ..ports.claim_manager import ClaimManager
    from .action_applier import ActionApplier
    from .completion_handler import CompletionHandler
    from .session_controller import SessionController

logger = logging.getLogger(__name__)


def _validation_issue_key(session: Session, config: Config) -> IssueKey | None:
    repo = session.issue.repo or config.repo
    if repo:
        return GitHubIssueKey(repo=repo, external_id=str(session.issue.number))
    if config.is_validation_enabled():
        logger.info(
            "[COMPLETION] Validation attempt identity unavailable: repo is unset "
            "for issue %s",
            session.issue.number,
        )
    return None


def _terminate_timed_out_session(
    session: Session,
    kill_session_fn: Callable[[str], None],
) -> None:
    """Best-effort runtime terminalization for sessions already marked timed out."""
    try:
        kill_session_fn(session.terminal_id)
    except Exception as exc:
        logger.warning(
            "[COMPLETION] Failed to kill timed-out session %s: %s",
            session.terminal_id,
            exc,
        )


def handle_session_completion(  # noqa: C901, PLR0912 - handles validation, actions, observer cleanup, claims, and history
    session: Session,
    status: SessionStatus,
    state: "OrchestratorState",
    completion_handler: "CompletionHandler",
    action_applier: "ActionApplier",
    observer: "SessionObserver",
    worktree_manager: Optional[WorktreeManager],
    kill_session_fn: Callable[[str], None],
    config: Config,
    session_output: SessionOutput,
    pr_url_hint: Optional[str] = None,
    processing_errors: Optional[list[str]] = None,
    diagnostic_path: Optional[str] = None,
    validation_error: Optional[str] = None,
    validation_error_file: Optional[str] = None,
    review_exchange_completed: bool = False,
    review_exchange_halted: bool = False,
    blocked_label: Optional[str] = None,
    blocked_reason: Optional[str] = None,
    completion_detail: Optional[dict[str, Any]] = None,
    claim_manager: Optional["ClaimManager"] = None,
    events: Optional[EventSink] = None,
) -> None:
    """Handle session completion - moved from Orchestrator per method table.

    Complexity is inherent - this processes validation retries, completion,
    actions, observer cleanup, claim release, history, and failure tracking.
    These are sequential steps that share the session context.

    Args:
        session: The completed session
        status: The session status
        state: Orchestrator state (active_sessions, session_history, etc.)
        completion_handler: For processing completion
        action_applier: For applying actions
        observer: For cleanup
        worktree_manager: For worktree removal
        kill_session_fn: Function to kill terminal session
        session_output: For session artifact management
        config: Configuration
        pr_url_hint: Optional PR URL from completion processor (for dry-run mode)
        processing_errors: Errors from completion processor (push failed, PR creation failed, etc.)
        diagnostic_path: Path to detailed failure diagnostic file (in worktree)
        validation_error: Validation error message (for retry prompt)
        validation_error_file: Path to validation error file (for retry prompt)
        claim_manager: Optional ClaimManager for releasing claims on completion
        events: Optional EventSink for emitting claim events
    """
    from ..domain.models import DiscoveredReview, DiscoveredFailure, PendingValidationRetry

    name = session.terminal_id
    entity = "review" if name.startswith("review-") else ("rework" if name.startswith("rework-") else "issue")
    log_transition(entity, session.issue.number, "ACTIVE", status.value.upper(), f"runtime={session.runtime_minutes}min")

    # Remove by session name, NOT issue number - multiple sessions can share an issue number
    state.active_sessions = [s for s in state.active_sessions if s.terminal_id != session.terminal_id]

    # Handle validation retry - queue for re-launch instead of normal completion
    if status == SessionStatus.NEEDS_VALIDATION_RETRY:
        next_retry_count = session.validation_retry_count + 1
        logger.info(
            "[COMPLETION] Issue #%d needs validation retry (attempt %d), queueing for re-launch",
            session.issue.number,
            next_retry_count,
        )
        completion_handler.mark_session_retry(session, reason="validation_retry")
        pending_retry = PendingValidationRetry(
            issue_number=session.issue.number,
            issue_title=session.issue.title,
            agent_label=session.agent_label or "",
            worktree_path=str(session.worktree_path),
            branch_name=session.branch_name,
            original_prompt=session.original_prompt,
            validation_error=validation_error or "",
            validation_error_file=validation_error_file,
            retry_count=next_retry_count,
            validation_cmd=config.validation.cmd if config.validation else None,
        )
        state.pending_validation_retries = [
            retry for retry in state.pending_validation_retries
            if retry.issue_number != session.issue.number
        ]
        state.pending_validation_retries.append(pending_retry)
        # Kill the terminal session but don't cleanup worktree (agent will continue there)
        kill_session_fn(session.terminal_id)
        return  # Skip normal completion processing

    # Process completion through CompletionHandler (includes policy decisions)
    if status == SessionStatus.COMPLETED:
        state.completed_today.append(session.issue.number)
    try:
        result = completion_handler.process_completion(
            session, status, pr_url_hint=pr_url_hint,
            processing_errors=processing_errors,
            diagnostic_path=diagnostic_path,
            review_exchange_completed=review_exchange_completed,
            review_exchange_halted=review_exchange_halted,
            blocked_label=blocked_label,
            blocked_reason=blocked_reason,
            completion_detail=completion_detail,
        )
    finally:
        # Timeout is orchestrator-authoritative; the terminal may still be alive
        # even though the session is terminal and must not be rediscovered.
        if status == SessionStatus.TIMED_OUT:
            _terminate_timed_out_session(session, kill_session_fn)
    if session.worktree_path:
        run_dir = session_output.find_run_dir(session.worktree_path, session.terminal_id)
        if run_dir:
            session_output.attach_claude_log(run_dir)
            run_session_analysis(run_dir)
        else:
            logger.warning(
                "[%s] No session output dir found - Claude log won't be attached",
                session.terminal_id,
            )

    # Apply completion actions (from CompletionHandler policy)
    if result.actions:
        logger.info(
            "[COMPLETION] Applying %d actions for issue #%d status=%s: %s",
            len(result.actions),
            session.issue.number,
            status.value,
            [type(a).__name__ for a in result.actions],
        )
        action_applier.apply_all(list(result.actions))
    else:
        logger.warning(
            "[COMPLETION] No actions generated for issue #%d status=%s",
            session.issue.number,
            status.value,
        )

    # Observer handles session-level cleanup (kill sessions, close tabs)
    observer.handle_completion(session, status)

    # Release claim if session had one
    if claim_manager and session.lease_id:
        try:
            claim_manager.release_claim(session.issue.number, session.lease_id)
            logger.info(
                "[COMPLETION] Released claim for issue #%d: lease_id=%s",
                session.issue.number,
                session.lease_id,
            )
            if events:
                events.publish(make_trace_event(
                    EventName.CLAIM_RELEASED,
                    {
                        "issue_number": session.issue.number,
                        "lease_id": session.lease_id,
                        "status": status.value,
                    },
                ))
        except Exception as e:
            logger.warning(
                "[COMPLETION] Failed to release claim for issue #%d: %s",
                session.issue.number,
                e,
            )

    state.session_history.append(result.history_entry)
    if result.should_defer_cleanup and result.pending_cleanup:
        state.pending_cleanups.append(result.pending_cleanup)
    else:
        # Record immediate cleanup as a fact for the Planner to handle
        from ..domain.models import ImmediateCleanup
        state.immediate_cleanups.append(ImmediateCleanup(
            issue_number=session.issue.number,
            terminal_id=session.terminal_id,
            worktree_path=str(session.worktree_path),
            reason=status.value,
        ))

    if result.should_queue_review and result.pr_url and result.pr_number:
        state.discovered_reviews.append(DiscoveredReview(
            session.issue.number, result.pr_number, result.pr_url, session.branch_name,
            agent_label=session.agent_label,
            issue_key=session.issue.key.stable_id(),
        ))
    if status in (SessionStatus.FAILED, SessionStatus.TIMED_OUT):
        state.discovered_failures.append(DiscoveredFailure(session.issue.number, session.issue.title, status.value))
        # Track failed issues to prevent immediate retry (cleared on cache refresh)
        state.failed_this_cycle.add(session.issue.number)
        logger.info(
            "[COMPLETION] Issue #%d added to failed_this_cycle (prevents retry until cache refresh)",
            session.issue.number,
        )

        # Surface AI session logs for debugging
        surface_failure_context(session, status)


def process_active_sessions(
    state: "OrchestratorState",
    observer: "SessionObserver",
    session_controller: "SessionController",
    completion_handler: "CompletionHandler",
    action_applier: "ActionApplier",
    worktree_manager: Optional[WorktreeManager],
    kill_session_fn: Callable[[str], None],
    config: Config,
) -> None:
    """Process active sessions - moved from Orchestrator per method table.

    DEPRECATED: Use observe_active_sessions() for the new async completion flow.
    This function is kept for backwards compatibility during migration.

    Args:
        state: Orchestrator state (active_sessions)
        observer: Session observer for checking session status
        session_controller: For deciding outcome
        completion_handler: For processing completion
        action_applier: For applying actions
        worktree_manager: For worktree cleanup
        kill_session_fn: Function to kill terminal session
        config: Configuration
    """
    from ..observation.observation import SessionObservation

    for session in list(state.active_sessions):
        # Snapshot iteration is mutation-safe; the live check filters any
        # duplicate terminal already removed by an earlier snapshot entry.
        if not has_active_terminal(state.active_sessions, session.terminal_id):
            logger.debug(
                "[COMPLETION] Skipping stale active-session snapshot entry: %s",
                session.terminal_id,
            )
            continue
        session_start = time.monotonic()
        obs = observer.observe_session(session)
        if obs.observation == SessionObservation.RUNNING:
            continue
        decision = session_controller.decide_outcome(
            obs, session.worktree_path, session.issue.number,
            session.issue.title, session.terminal_id, session.completion_path,
            validation_retry_count=session.validation_retry_count,
            original_prompt=session.original_prompt,
            retry_prompt_template=(
                session.agent_config.retry_prompt_template
                or config.retry.retry_prompt_template
            ),
            repo_root=config.repo_root,
            issue_key=_validation_issue_key(session, config),
        )
        if decision.status == SessionStatus.RUNNING:
            logger.info(
                "[COMPLETION] Session remains active after completion decision: "
                "session=%s issue=%s reason=%s",
                session.terminal_id,
                session.issue.number,
                decision.reason,
            )
            continue
        # Extract pr_url, errors, and diagnostic_path from completion processor result
        pr_url_hint = None
        processing_errors = None
        diagnostic_path = None
        validation_error = decision.validation_error
        validation_error_file = decision.validation_error_file
        review_exchange_completed = False
        review_exchange_halted = False
        if decision.processing_result:
            if decision.processing_result.pr_url:
                pr_url_hint = decision.processing_result.pr_url
            if decision.processing_result.errors:
                processing_errors = decision.processing_result.errors
            if decision.processing_result.diagnostic_path:
                diagnostic_path = decision.processing_result.diagnostic_path
            review_exchange_completed = decision.processing_result.review_exchange_completed
            review_exchange_halted = decision.processing_result.review_exchange_halted
        handle_session_completion(
            session, decision.status, state, completion_handler, action_applier,
            observer, worktree_manager, kill_session_fn, config,
            session_output=session_controller.session_output,
            pr_url_hint=pr_url_hint, processing_errors=processing_errors,
            diagnostic_path=diagnostic_path,
            validation_error=validation_error,
            validation_error_file=str(validation_error_file) if validation_error_file else None,
            review_exchange_completed=review_exchange_completed,
            review_exchange_halted=review_exchange_halted,
            blocked_label=decision.blocked_label,
            blocked_reason=decision.blocked_reason,
            completion_detail=decision.completion_detail,
        )
        session_elapsed = time.monotonic() - session_start
        if session_elapsed > 5:
            logger.warning(
                "[LOOP] Session handling took %.1fs (session=%s issue=%s observation=%s)",
                session_elapsed,
                session.terminal_id,
                session.issue.number,
                obs.observation.value,
            )
