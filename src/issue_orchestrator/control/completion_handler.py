"""CompletionHandler - handles session completion state machine updates and events.

This module extracts completion logic from the orchestrator:
1. State machine transitions (issue, session, review)
2. Event emission for trace events
3. History entry creation
4. Cleanup decision logic

The orchestrator calls this to handle the complex state updates when a session completes.
"""

import logging
import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Optional, Callable

if TYPE_CHECKING:
    from ..domain.state_machines.issue_machine import IssueStateMachine
    from ..domain.state_machines.session_machine import SessionStateMachine
    from ..domain.state_machines.review_machine import ReviewStateMachine
    from ..domain.models import PendingReview, PendingRework, PendingTriageReview
    from .state_machine_manager import StateMachineManager

from ..infra.config import Config
from ..events import EventName
from ..infra.logging_config import log_context, get_repo_log_path
from ..domain.models import Session, SessionStatus, SessionHistoryEntry, PendingCleanup
from ..ports import EventSink, TraceEvent, RepositoryHost, Issue
from ..ports.session_output import SessionOutput
from .actions import Action, AddLabelAction, RemoveLabelAction, AddCommentAction
from .completion_processor import ERROR_PREFIX_PUSH, ERROR_PREFIX_CREATE_PR
from .reconciliation import build_expected_for_mutation
from ..infra import labels

logger = logging.getLogger(__name__)


def _has_critical_errors(processing_errors: Optional[list[str]]) -> bool:
    """Check if processing_errors contains critical push/PR failures."""
    if not processing_errors:
        return False
    return any(
        error.startswith(ERROR_PREFIX_PUSH) or error.startswith(ERROR_PREFIX_CREATE_PR)
        for error in processing_errors
    )


@dataclass
class CompletionResult:
    """Result of processing a session completion."""

    history_entry: SessionHistoryEntry
    pr_url: Optional[str] = None
    pr_number: Optional[int] = None
    should_defer_cleanup: bool = False
    should_queue_review: bool = False
    pending_cleanup: Optional[PendingCleanup] = None
    actions: tuple[Action, ...] = ()


class CompletionHandler:
    """Handles session completion state machine updates and event emission.

    Dependencies:
    - config: Configuration with cleanup and review settings
    - events: EventSink for trace event emission
    - repository_host: For fetching PR info
    - issue_machines: Dict of issue state machines
    - session_machines: Dict of session state machines
    - review_machines: Dict of review state machines
    """

    def __init__(
        self,
        config: Config,
        events: EventSink,
        repository_host: RepositoryHost,
        get_issue_machine_fn: Callable[[Issue], Optional["IssueStateMachine"]],
        get_session_machine_fn: Callable[[str], Optional["SessionStateMachine"]],
        get_review_machine_fn: Callable[[int], Optional["ReviewStateMachine"]],
        session_output: SessionOutput,
    ):
        self.config = config
        self.events = events
        self.repository_host = repository_host
        self._get_issue_machine = get_issue_machine_fn
        self._get_session_machine = get_session_machine_fn
        self._get_review_machine = get_review_machine_fn
        self._session_output = session_output

    def process_completion(
        self,
        session: Session,
        status: SessionStatus,
        pr_url_hint: Optional[str] = None,
        processing_errors: Optional[list[str]] = None,
        diagnostic_path: Optional[str] = None,
    ) -> CompletionResult:
        """Process a session completion and update all state machines.

        Args:
            session: The completed session
            status: The completion status
            pr_url_hint: Optional PR URL from completion processor (for dry-run mode)
            processing_errors: Errors from completion processor (push failed, etc.)
            diagnostic_path: Path to detailed failure diagnostic file (in worktree)

        Returns:
            CompletionResult with history entry and cleanup decision
        """
        start_time = time.monotonic()
        issue_key = session.key.issue.stable_id()
        logger.info(
            "Processing completion: issue=%s session=%s status=%s branch=%s worktree=%s",
            session.issue.number,
            session.terminal_id,
            status.value,
            session.branch_name,
            session.worktree_path,
            extra=log_context(issue_key=issue_key, session_id=session.terminal_id),
        )

        # Fetch PR info if completed (or use hint from completion processor)
        pr_url, pr_number, pr_infos = self._fetch_pr_info(session, status, pr_url_hint=pr_url_hint)
        if pr_infos:
            self._emit_pr_view_changed(
                pr_infos[0],
                issue_key=session.key.issue.stable_id(),
                issue_number=session.issue.number,
            )
        elif pr_url and pr_number is not None:
            self._emit_pr_view_hint(
                pr_number,
                pr_url,
                issue_key=session.key.issue.stable_id(),
                issue_number=session.issue.number,
            )

        # Determine history status: if agent said COMPLETED but push/PR failed,
        # use FAILED for history to show red dot in UI
        history_status = status
        history_status_reason: Optional[str] = None
        if status == SessionStatus.COMPLETED and _has_critical_errors(processing_errors):
            logger.info(
                "[COMPLETION] Agent reported completed but push/PR failed - using FAILED for history: issue=%d",
                session.issue.number,
            )
            history_status = SessionStatus.FAILED
            history_status_reason = "Push or PR creation failed"

        # Create history entry
        history_entry = self._create_history_entry(
            session, history_status, pr_url, status_reason_override=history_status_reason
        )

        # Emit trace events
        self._emit_trace_events(session, status, pr_url)

        # Update state machines
        self._update_state_machines(session, status, pr_url)

        # Determine cleanup strategy
        should_defer, pending_cleanup = self._determine_cleanup_strategy(
            session, status, pr_url, pr_number
        )

        # Determine if we should queue code review
        should_queue_review = self._should_queue_review(session, status, pr_url, pr_number)

        # Generate actions for label/comment changes (policy logic)
        completion_actions = self.generate_completion_actions(
            session, status, processing_errors=processing_errors,
            diagnostic_path=diagnostic_path
        )

        if status in (
            SessionStatus.FAILED,
            SessionStatus.TIMED_OUT,
            SessionStatus.BLOCKED,
            SessionStatus.NEEDS_HUMAN,
        ) or processing_errors:
            log_path = get_repo_log_path(self.config.repo_root)
            run_dir = self._session_output.find_run_dir(
                session.worktree_path, session.terminal_id
            )
            if run_dir:
                self._session_output.write_orchestrator_tail(
                    run_dir=run_dir,
                    log_path=log_path,
                    issue_number=session.issue.number,
                    session_name=session.terminal_id,
                )
            else:
                logger.warning(
                    "[%s] No session output dir found for failed session - "
                    "session may have crashed before setup completed",
                    session.terminal_id,
                )

        result = CompletionResult(
            history_entry=history_entry,
            pr_url=pr_url,
            pr_number=pr_number,
            should_defer_cleanup=should_defer,
            should_queue_review=should_queue_review,
            pending_cleanup=pending_cleanup,
            actions=completion_actions,
        )
        total_duration = time.monotonic() - start_time
        logger.info(
            "Completion processed: issue=%s session=%s status=%s pr_number=%s queue_review=%s defer_cleanup=%s elapsed=%.2fs",
            session.issue.number,
            session.terminal_id,
            status.value,
            pr_number,
            should_queue_review,
            should_defer,
            total_duration,
            extra=log_context(issue_key=issue_key, session_id=session.terminal_id),
        )
        return result

    def _fetch_pr_info(
        self,
        session: Session,
        status: SessionStatus,
        pr_url_hint: Optional[str] = None,
    ) -> tuple[Optional[str], Optional[int], Optional[list[Any]]]:
        """Fetch PR info for a completed session.

        Args:
            session: The completed session
            status: The completion status
            pr_url_hint: Optional PR URL from completion processor (for dry-run mode)

        Returns:
            Tuple of (pr_url, pr_number, prs_list)
        """
        import re

        pr_url = None
        pr_number = None
        prs = None

        if status == SessionStatus.COMPLETED:
            # If we have a pr_url_hint from completion processor, use it (dry-run mode)
            if pr_url_hint:
                pr_url = pr_url_hint
                # Extract PR number from URL
                match = re.search(r"/pull/(\d+)", pr_url)
                if match:
                    pr_number = int(match.group(1))
                    try:
                        pr_info = self.repository_host.get_pr(pr_number)
                        if pr_info:
                            prs = [pr_info]
                    except Exception as e:
                        logger.warning("Failed to fetch PR %s for PR hint: %s", pr_number, e)
                logger.info(
                    "[PR_HINT] Using PR from completion processor: %s (number=%s)",
                    pr_url,
                    pr_number,
                    extra=log_context(issue_key=session.key.issue.stable_id(), session_id=session.terminal_id),
                )
            else:
                logger.debug("[ADAPTER] Using GitHubAdapter for get_prs_for_branch")
                start = time.monotonic()
                pr_infos = self.repository_host.get_prs_for_branch(session.branch_name)
                duration = time.monotonic() - start
                logger.info(
                    "Fetched PRs for branch in %.2fs: branch=%s count=%d",
                    duration,
                    session.branch_name,
                    len(pr_infos),
                    extra=log_context(issue_key=session.key.issue.stable_id(), session_id=session.terminal_id),
                )
                if pr_infos:
                    pr_url = pr_infos[0].url
                    pr_number = pr_infos[0].number
                    prs = list(pr_infos)

        return pr_url, pr_number, prs

    def _create_history_entry(
        self,
        session: Session,
        status: SessionStatus,
        pr_url: Optional[str],
        status_reason_override: Optional[str] = None,
    ) -> SessionHistoryEntry:
        """Create a session history entry.

        Args:
            session: The session that completed
            status: The status to record in history
            pr_url: URL of the PR if one was created
            status_reason_override: Optional override for the status reason
                (used when agent said completed but push/PR failed)
        """
        # Generate human-readable status reason
        status_reasons = {
            SessionStatus.COMPLETED: "PR created successfully",
            SessionStatus.BLOCKED: "Agent marked issue as blocked",
            SessionStatus.NEEDS_HUMAN: "Agent requested human input",
            SessionStatus.TIMED_OUT: f"Exceeded {session.agent_config.timeout_minutes} min timeout",
            SessionStatus.FAILED: "Session ended without PR or status update",
        }
        status_reason = status_reason_override or status_reasons.get(status, "Unknown")

        return SessionHistoryEntry(
            issue_number=session.issue.number,
            title=session.issue.title,
            agent_type=session.issue.agent_type or "unknown",
            status=status.value,
            runtime_minutes=session.runtime_minutes,
            pr_url=pr_url,
            status_reason=status_reason,
            worktree_path=session.worktree_path,
        )

    def _emit_trace_events(
        self,
        session: Session,
        status: SessionStatus,
        pr_url: Optional[str],
    ) -> None:
        """Emit trace events for session completion."""
        status_reasons = {
            SessionStatus.COMPLETED: "PR created successfully",
            SessionStatus.BLOCKED: "Agent marked issue as blocked",
            SessionStatus.NEEDS_HUMAN: "Agent requested human input",
            SessionStatus.TIMED_OUT: f"Exceeded {session.agent_config.timeout_minutes} min timeout",
            SessionStatus.FAILED: "Session ended without PR or status update",
        }
        status_reason = status_reasons.get(status, "Unknown")

        if status == SessionStatus.COMPLETED:
            self.events.publish(TraceEvent(EventName.SESSION_COMPLETED, {
                "issue_number": session.issue.number,
                "session_id": session.terminal_id,
                "pr_url": pr_url,
                "runtime_minutes": session.runtime_minutes,
            }))
        elif status == SessionStatus.FAILED or status == SessionStatus.TIMED_OUT:
            self.events.publish(TraceEvent(EventName.SESSION_FAILED, {
                "issue_number": session.issue.number,
                "session_id": session.terminal_id,
                "error": status_reason,
                "runtime_minutes": session.runtime_minutes,
            }))
        elif status == SessionStatus.BLOCKED:
            self.events.publish(TraceEvent(EventName.ISSUE_BLOCKED, {
                "issue_number": session.issue.number,
                "reason": status_reason,
            }))
        elif status == SessionStatus.NEEDS_HUMAN:
            self.events.publish(TraceEvent(EventName.ISSUE_NEEDS_HUMAN, {
                "issue_number": session.issue.number,
                "reason": status_reason,
            }))

    def _update_state_machines(
        self,
        session: Session,
        status: SessionStatus,
        pr_url: Optional[str],
    ) -> None:
        """Update all relevant state machines for the session completion."""
        status_reasons = {
            SessionStatus.COMPLETED: "PR created successfully",
            SessionStatus.BLOCKED: "Agent marked issue as blocked",
            SessionStatus.NEEDS_HUMAN: "Agent requested human input",
            SessionStatus.TIMED_OUT: f"Exceeded {session.agent_config.timeout_minutes} min timeout",
            SessionStatus.FAILED: "Session ended without PR or status update",
        }
        status_reason = status_reasons.get(status, "Unknown")

        logger.debug(f"[STATE_MACHINE] Triggering transitions for session {session.terminal_id}")

        # 1. Update session state machine
        self._update_session_machine(session, status, status_reason)

        # 2. Update issue state machine
        self._update_issue_machine(session, status, pr_url)

        # 3. Update review state machine (if this is a review session)
        is_review = session.terminal_id.startswith("review-")
        if is_review and status == SessionStatus.COMPLETED:
            self._update_review_machine(session)

    def _update_session_machine(
        self,
        session: Session,
        status: SessionStatus,
        status_reason: str,
    ) -> None:
        """Update the session state machine."""
        session_machine = self._get_session_machine(session.terminal_id)
        if session_machine:
            logger.debug(f"[STATE_MACHINE] Found session machine for {session.terminal_id}")
            if status == SessionStatus.COMPLETED:
                logger.info(f"[STATE_MACHINE] Session {session.terminal_id}: RUNNING -> COMPLETED")
                session_machine.complete()  # type: ignore[attr-defined]
            elif status == SessionStatus.FAILED:
                logger.info(f"[STATE_MACHINE] Session {session.terminal_id}: RUNNING -> FAILED (reason: {status_reason})")
                session_machine.fail(data={'reason': status_reason})  # type: ignore[attr-defined]
            elif status == SessionStatus.TIMED_OUT:
                logger.info(f"[STATE_MACHINE] Session {session.terminal_id}: RUNNING -> TIMED_OUT")
                session_machine.timeout()  # type: ignore[attr-defined]
            elif status == SessionStatus.BLOCKED:
                logger.info(f"[STATE_MACHINE] Session {session.terminal_id}: RUNNING -> BLOCKED")
                session_machine.block()  # type: ignore[attr-defined]
            elif status == SessionStatus.NEEDS_HUMAN:
                logger.info(f"[STATE_MACHINE] Session {session.terminal_id}: RUNNING -> NEEDS_HUMAN")
                session_machine.needs_human()  # type: ignore[attr-defined]
        else:
            logger.debug(f"[STATE_MACHINE] No session machine found for {session.terminal_id} (may be restored session)")

    def _update_issue_machine(
        self,
        session: Session,
        status: SessionStatus,
        pr_url: Optional[str],
    ) -> None:
        """Update the issue state machine."""
        issue_machine = self._get_issue_machine(session.issue)
        if issue_machine:
            logger.debug(f"[STATE_MACHINE] Found issue machine for issue #{session.issue.number}")
            # Only trigger pr_created for issue sessions (not review/rework sessions)
            # Review/rework sessions work on issues that already have PRs
            is_issue_session = session.terminal_id.startswith("issue-")
            if status == SessionStatus.COMPLETED and pr_url and is_issue_session:
                if issue_machine.can_transition("pr_created"):
                    logger.info(
                        "[STATE_MACHINE] Issue #%d: IN_PROGRESS -> PR_PENDING (PR: %s)",
                        session.issue.number,
                        pr_url,
                    )
                    issue_machine.pr_created(data={'pr_url': pr_url})  # type: ignore[attr-defined]
                else:
                    logger.warning(
                        "[STATE_MACHINE] Issue #%d pr_created ignored (state=%s)",
                        session.issue.number,
                        issue_machine.get_state().value,
                    )
            elif status == SessionStatus.BLOCKED:
                logger.info(f"[STATE_MACHINE] Issue #{session.issue.number}: IN_PROGRESS -> BLOCKED")
                issue_machine.block()  # type: ignore[attr-defined]
            elif status == SessionStatus.NEEDS_HUMAN:
                logger.info(f"[STATE_MACHINE] Issue #{session.issue.number}: IN_PROGRESS -> NEEDS_HUMAN")
                issue_machine.needs_human()  # type: ignore[attr-defined]
        else:
            logger.debug(f"[STATE_MACHINE] No issue machine found for issue #{session.issue.number} (may be restored session)")

    def _update_review_machine(self, session: Session) -> None:
        """Update the review state machine for a completed review session."""
        match = re.match(r"review-(\d+)", session.terminal_id)
        if not match:
            return

        pr_number_review = int(match.group(1))
        review_machine = self._get_review_machine(pr_number_review)
        if not review_machine:
            logger.debug(f"[STATE_MACHINE] No review machine found for PR #{pr_number_review}")
            return

        logger.debug(f"[STATE_MACHINE] Found review machine for PR #{pr_number_review}")
        try:
            pr_info = self.repository_host.get_pr(pr_number_review)
            if pr_info:
                self._emit_pr_view_changed(pr_info, issue_key=session.key.issue.stable_id(), issue_number=session.issue.number)
                self._process_review_outcome(pr_info, pr_number_review, review_machine)
        except Exception as e:
            logger.warning(f"Failed to check PR labels for review outcome: {e}")
            self.events.publish(TraceEvent(EventName.APPLY_FAILED, {
                "step_type": "review_outcome_check", "pr_number": pr_number_review,
                "issue_number": session.issue.number, "error": str(e),
            }))

    def _process_review_outcome(self, pr_info: Any, pr_number: int, review_machine: Any) -> None:
        """Process review outcome based on PR labels."""
        labels = pr_info.labels
        if self.config.code_reviewed_label and self.config.code_reviewed_label in labels:
            self._handle_review_approved(pr_info, pr_number, review_machine)
        elif self.config.get_label_needs_rework() in labels:
            self._handle_changes_requested(pr_number, review_machine)

    def _handle_review_approved(self, pr_info: Any, pr_number: int, review_machine: Any) -> None:
        """Handle approved review outcome."""
        logger.info(f"[STATE_MACHINE] PR #{pr_number}: IN_REVIEW -> APPROVED")
        if getattr(pr_info, "draft", None) is True:
            try:
                self.repository_host.set_pr_draft(pr_number, False)
                logger.info("[STATE_MACHINE] PR #%d marked ready for review", pr_number)
            except Exception as e:
                logger.warning("Failed to mark PR #%d ready for review: %s", pr_number, e)
        self._try_transition(review_machine, "approve", pr_number)

    def _handle_changes_requested(self, pr_number: int, review_machine: Any) -> None:
        """Handle changes requested review outcome."""
        logger.info(f"[STATE_MACHINE] PR #{pr_number}: IN_REVIEW -> CHANGES_REQUESTED")
        self._try_transition(review_machine, "request_changes", pr_number)
        if review_machine.can_transition("queue_rework"):
            logger.info(f"[STATE_MACHINE] PR #{pr_number}: CHANGES_REQUESTED -> REWORK_PENDING")
            review_machine.queue_rework()  # type: ignore[attr-defined]
        else:
            logger.warning("[STATE_MACHINE] PR #%d queue_rework ignored (state=%s)", pr_number, review_machine.get_state().value)

    def _try_transition(self, machine: Any, transition: str, pr_number: int) -> None:
        """Try to perform a state machine transition."""
        if machine.can_transition(transition):
            getattr(machine, transition)()
        else:
            logger.warning("[STATE_MACHINE] PR #%d %s ignored (state=%s)", pr_number, transition, machine.get_state().value)

    def _emit_pr_view_changed(
        self,
        pr_info: Any,
        issue_key: str | None,
        issue_number: int | None,
    ) -> None:
        payload = {
            "pr_number": pr_info.number,
            "labels": list(getattr(pr_info, "labels", []) or []),
            "pr_url": getattr(pr_info, "url", None),
        }
        if issue_key is not None:
            payload["issue_key"] = issue_key
        if issue_number is not None:
            payload["issue_number"] = issue_number
        self.events.publish(TraceEvent(EventName.PR_VIEW_CHANGED, payload))

    def _emit_pr_view_hint(
        self,
        pr_number: int,
        pr_url: str,
        issue_key: str,
        issue_number: int,
    ) -> None:
        payload = {
            "pr_number": pr_number,
            "labels": [],
            "pr_url": pr_url,
            "issue_key": issue_key,
            "issue_number": issue_number,
        }
        self.events.publish(TraceEvent(EventName.PR_VIEW_CHANGED, payload))

    def _determine_cleanup_strategy(
        self,
        session: Session,
        status: SessionStatus,
        pr_url: Optional[str],
        pr_number: Optional[int],
    ) -> tuple[bool, Optional[PendingCleanup]]:
        """Determine if cleanup should be deferred and create PendingCleanup if so.

        Returns:
            Tuple of (should_defer, pending_cleanup)
        """
        is_work_session = not session.terminal_id.startswith(("review-", "rework-"))
        should_defer = False
        pending_cleanup = None

        if status == SessionStatus.COMPLETED and is_work_session and pr_url and pr_number:
            # Check if we should defer cleanup based on review workflow
            if self.config.triage_review_agent:
                # Triage workflow: defer until triage review passes
                should_defer = self.config.cleanup.with_triage.close_ai_session_tabs
            elif self.config.code_review_agent:
                # Code review only: defer if configured to wait
                should_defer = (
                    self.config.cleanup.without_triage.wait_for_code_review
                    and self.config.cleanup.without_triage.close_ai_session_tabs
                )

        if should_defer:
            # should_defer is only True if pr_number and pr_url are set (line 337)
            assert pr_number is not None and pr_url is not None
            pending_cleanup = PendingCleanup(
                issue=session.issue,
                pr_number=pr_number,
                pr_url=pr_url,
                branch_name=session.branch_name,
                terminal_session_name=session.terminal_id,
                worktree_path=session.worktree_path,
            )
            logger.info(f"[CLEANUP] Deferred cleanup for #{session.issue.number} until review completes")

        return should_defer, pending_cleanup

    def _should_queue_review(
        self,
        session: Session,
        status: SessionStatus,
        pr_url: Optional[str],
        pr_number: Optional[int] = None,
    ) -> bool:
        """Determine if session should be added to discovered_reviews.

        Note: This returns True even for dry-run PRs (so pr-pending label gets added).
        The actual review queuing is controlled by the planner, which skips dry-run PRs.
        """
        is_review_session = session.terminal_id.startswith("review-")

        if pr_url and self.config.code_review_agent and not session.agent_config.skip_review and not is_review_session:
            logger.info(f"[REVIEW] Session #{session.issue.number} completed with PR, queuing code review")
            return True
        elif pr_url and is_review_session:
            logger.info(f"[REVIEW] Review session {session.terminal_id} completed - no re-queue needed")
        elif pr_url and not self.config.code_review_agent:
            logger.info(f"[REVIEW] Session #{session.issue.number} completed but code review not configured")
        elif pr_url and session.agent_config.skip_review:
            logger.info(f"[REVIEW] Session #{session.issue.number} skipping review (skip_review=true)")
        elif not pr_url:
            logger.info(f"[REVIEW] Session #{session.issue.number} completed but no PR found")

        return False

    def generate_completion_actions(
        self,
        session: Session,
        status: SessionStatus,
        processing_errors: Optional[list[str]] = None,
        diagnostic_path: Optional[str] = None,
    ) -> tuple[Action, ...]:
        """Generate label/comment actions for session completion.

        This encapsulates the POLICY logic for what labels to add/remove
        when a session completes with various statuses.

        Args:
            session: The completed session
            status: The completion status
            processing_errors: Errors from completion processor (push failed, etc.)
            diagnostic_path: Path to detailed failure diagnostic file (in worktree)

        Returns:
            Tuple of actions to apply
        """
        actions: list[Action] = []
        issue_number = session.issue.number
        in_progress_label = self.config.get_label_in_progress()
        expected = build_expected_for_mutation()
        is_issue_session = session.terminal_id.startswith("issue-")
        session_kind = session.terminal_id.split("-", 1)[0]

        # If agent said "completed" but critical processing failed (push/PR creation),
        # treat as blocked-failed. Non-critical failures (like comment timeouts)
        # should not block the issue.
        critical_errors = []
        if processing_errors:
            critical_errors = [
                error for error in processing_errors
                if error.startswith(ERROR_PREFIX_PUSH) or error.startswith(ERROR_PREFIX_CREATE_PR)
            ]
        if status == SessionStatus.COMPLETED and critical_errors:
            logger.info(
                "[COMPLETION] Agent said completed but processing failed: issue=%d errors=%s",
                issue_number, critical_errors
            )
            # Brief error hint for comment (not full details - those are in diagnostic file)
            first_error = critical_errors[0][:100] if critical_errors else "Unknown error"
            if len(first_error) == 100:
                first_error += "..."

            # Build diagnostic location info
            diagnostic_info = ""
            if diagnostic_path and session.worktree_path:
                # Show sanitized relative path (worktree folder name + diagnostic path)
                from pathlib import Path
                worktree_name = Path(session.worktree_path).name
                diagnostic_info = (
                    f"\n**Diagnostic file:** `{worktree_name}/{diagnostic_path}`\n"
                )

            actions.append(AddLabelAction(
                issue_number=issue_number,
                label=labels.BLOCKED_FAILED,
                reason="Processing failed after agent completion (push/PR creation failed)",
                expected=expected,
            ))
            actions.append(AddCommentAction(
                number=issue_number,
                comment=f"❌ **Processing Failed**\n\n"
                        f"The agent completed its work, but the orchestrator could not push or create a PR.\n\n"
                        f"**Error:** {first_error}\n"
                        f"{diagnostic_info}\n"
                        f"- Runtime: {session.runtime_minutes:.1f} minutes\n"
                        f"- Session: `{session.terminal_id}`\n\n"
                        f"This issue has been marked as `{labels.BLOCKED_FAILED}` and will not be automatically retried.\n"
                        f"Remove the label to retry.",
                reason="Notify about processing failure",
                expected=expected,
            ))
            actions.append(RemoveLabelAction(
                issue_number=issue_number,
                label=in_progress_label,
                reason="Processing failed - releasing claim",
                expected=expected,
            ))
            return tuple(actions)

        if status == SessionStatus.TIMED_OUT:
            # POLICY: Timeout → blocked-failed + comment + release claim
            if is_issue_session:
                actions.append(AddLabelAction(
                    issue_number=issue_number,
                    label=labels.BLOCKED_FAILED,
                    reason=f"Session timed out after {session.runtime_minutes} minutes",
                    expected=expected,
                ))
                timeout_mins = session.agent_config.timeout_minutes if session.agent_config else "unknown"
                actions.append(AddCommentAction(
                    number=issue_number,
                    comment=f"⏱️ **Session Timed Out**\n\n"
                            f"The agent session exceeded the {timeout_mins} minute timeout limit.\n\n"
                            f"- Runtime: {session.runtime_minutes:.1f} minutes\n"
                            f"- Session: `{session.terminal_id}`\n\n"
                            f"This issue has been marked as `{labels.BLOCKED_FAILED}` and will not be automatically retried.\n"
                            f"Remove the label to allow reprocessing.",
                    reason="Notify about session timeout",
                    expected=expected,
                ))
                actions.append(RemoveLabelAction(
                    issue_number=issue_number,
                    label=in_progress_label,
                    reason="Session timed out - releasing claim",
                    expected=expected,
                ))
            else:
                actions.append(AddCommentAction(
                    number=issue_number,
                    comment=f"⏱️ **{session_kind.capitalize()} Session Timed Out**\n\n"
                            f"The {session_kind} session exceeded its timeout and did not produce an outcome.\n\n"
                            f"- Runtime: {session.runtime_minutes:.1f} minutes\n"
                            f"- Session: `{session.terminal_id}`\n\n"
                            f"The PR remains pending; review will be retried automatically.",
                    reason=f"Notify about {session_kind} session timeout",
                    expected=expected,
                ))

        elif status == SessionStatus.FAILED:
            # POLICY: Failure (no completion record) → blocked-needs-human + comment + release claim
            # This requires human investigation - agent-done is MANDATORY but was not called
            if is_issue_session:
                actions.append(AddLabelAction(
                    issue_number=issue_number,
                    label=labels.BLOCKED_NEEDS_HUMAN,
                    reason="Session terminated without calling agent-done (mandatory)",
                    expected=expected,
                ))
                actions.append(AddCommentAction(
                    number=issue_number,
                    comment=f"🔍 **Session Needs Investigation**\n\n"
                            f"The agent session terminated without calling `agent-done`.\n\n"
                            f"**This is unexpected** - `agent-done` is mandatory and must be called "
                            f"to complete any session (completed, blocked, or needs_human).\n\n"
                            f"- Runtime: {session.runtime_minutes:.1f} minutes\n"
                            f"- Session: `{session.terminal_id}`\n\n"
                            f"**Possible causes:**\n"
                            f"- Agent crashed or was interrupted\n"
                            f"- Agent ignored the mandatory `agent-done` requirement\n"
                            f"- Infrastructure issue prevented completion\n\n"
                            f"This issue has been marked as `{labels.BLOCKED_NEEDS_HUMAN}` for investigation.\n"
                            f"Remove the label after investigating to allow reprocessing.",
                    reason="Notify about session needing human investigation",
                    expected=expected,
                ))
                actions.append(RemoveLabelAction(
                    issue_number=issue_number,
                    label=in_progress_label,
                    reason="Session failed - releasing claim",
                    expected=expected,
                ))
            else:
                # Review/rework session failed - needs human to check what happened
                actions.append(AddCommentAction(
                    number=issue_number,
                    comment=f"🔍 **{session_kind.capitalize()} Session Needs Investigation**\n\n"
                            f"The {session_kind} session terminated without calling `agent-done`.\n\n"
                            f"**This is unexpected** - `agent-done` is mandatory.\n\n"
                            f"- Runtime: {session.runtime_minutes:.1f} minutes\n"
                            f"- Session: `{session.terminal_id}`\n\n"
                            f"The PR remains pending; please investigate what happened.",
                    reason=f"Notify about {session_kind} session needing investigation",
                    expected=expected,
                ))

        elif status == SessionStatus.COMPLETED:
            # POLICY: Completion → release in-progress (claim maintained via pr-pending)
            actions.append(RemoveLabelAction(
                issue_number=issue_number,
                label=in_progress_label,
                reason="Session completed successfully",
                expected=expected,
            ))

        # Note: BLOCKED and NEEDS_HUMAN keep in-progress label to maintain ownership claim
        # This is intentional policy - the issue is still being worked on

        return tuple(actions)


def launch_review_by_number(
    n: int,
    pending_reviews: list["PendingReview"],
    launch_review_session_fn: Callable[["PendingReview"], Optional["Session"]],
) -> Optional["Session"]:
    """Launch review session by number - moved per method table."""
    r = next((r for r in pending_reviews if r.pr_number == n), None)
    return launch_review_session_fn(r) if r else None


def launch_rework_by_number(
    n: int,
    pending_reworks: list["PendingRework"],
    launch_rework_session_fn: Callable[["PendingRework"], Optional["Session"]],
) -> Optional["Session"]:
    """Launch rework session by number - moved per method table."""
    r = next((r for r in pending_reworks if r.resolve_issue_number() == n), None)
    return launch_rework_session_fn(r) if r else None


def get_review_machine(pr: int, issue: int, state_machines: "StateMachineManager") -> Optional["ReviewStateMachine"]:
    """Get review state machine - moved per method table."""
    return state_machines.get_review_machine(pr, issue)


def launch_triage_by_number(
    n: int,
    pending_triage_reviews: list["PendingTriageReview"],
    active_sessions: list["Session"],
    launch_triage_session_fn: Callable[["PendingTriageReview"], None],
) -> Optional["Session"]:
    """Launch triage session by number - moved per method table."""
    t = next((t for t in pending_triage_reviews if t.issue_number == n), None)
    if t:
        launch_triage_session_fn(t)
    return next((s for s in active_sessions if s.issue.number == n), None)
