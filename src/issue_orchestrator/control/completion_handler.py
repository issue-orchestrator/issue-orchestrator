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
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional, Callable

if TYPE_CHECKING:
    from ..domain.state_machines.issue_machine import IssueStateMachine
    from ..domain.state_machines.session_machine import SessionStateMachine
    from ..domain.state_machines.review_machine import ReviewStateMachine

from ..config import Config
from ..events import EventName
from ..models import Session, SessionStatus, SessionHistoryEntry, PendingCleanup
from ..ports import EventSink, TraceEvent, RepositoryHost, Issue

logger = logging.getLogger(__name__)


@dataclass
class CompletionResult:
    """Result of processing a session completion."""

    history_entry: SessionHistoryEntry
    pr_url: Optional[str] = None
    pr_number: Optional[int] = None
    should_defer_cleanup: bool = False
    should_queue_review: bool = False
    pending_cleanup: Optional[PendingCleanup] = None


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
    ):
        self.config = config
        self.events = events
        self.repository_host = repository_host
        self._get_issue_machine = get_issue_machine_fn
        self._get_session_machine = get_session_machine_fn
        self._get_review_machine = get_review_machine_fn

    def process_completion(
        self,
        session: Session,
        status: SessionStatus,
    ) -> CompletionResult:
        """Process a session completion and update all state machines.

        Args:
            session: The completed session
            status: The completion status

        Returns:
            CompletionResult with history entry and cleanup decision
        """
        # Fetch PR info if completed
        pr_url, pr_number, prs = self._fetch_pr_info(session, status)

        # Create history entry
        history_entry = self._create_history_entry(
            session, status, pr_url
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
        should_queue_review = self._should_queue_review(session, status, pr_url)

        return CompletionResult(
            history_entry=history_entry,
            pr_url=pr_url,
            pr_number=pr_number,
            should_defer_cleanup=should_defer,
            should_queue_review=should_queue_review,
            pending_cleanup=pending_cleanup,
        )

    def _fetch_pr_info(
        self,
        session: Session,
        status: SessionStatus,
    ) -> tuple[Optional[str], Optional[int], Optional[list[dict[str, Any]]]]:
        """Fetch PR info for a completed session.

        Returns:
            Tuple of (pr_url, pr_number, prs_list)
        """
        pr_url = None
        pr_number = None
        prs = None

        if status == SessionStatus.COMPLETED:
            logger.debug("[ADAPTER] Using GitHubAdapter for get_prs_for_branch")
            pr_infos = self.repository_host.get_prs_for_branch(session.branch_name)
            if pr_infos:
                pr_url = pr_infos[0].url
                pr_number = pr_infos[0].number
                # Convert PRInfo to dict for backward compatibility
                prs = [{"url": pi.url, "number": pi.number, "title": pi.title} for pi in pr_infos]

        return pr_url, pr_number, prs

    def _create_history_entry(
        self,
        session: Session,
        status: SessionStatus,
        pr_url: Optional[str],
    ) -> SessionHistoryEntry:
        """Create a session history entry."""
        # Generate human-readable status reason
        status_reasons = {
            SessionStatus.COMPLETED: "PR created successfully",
            SessionStatus.BLOCKED: "Agent marked issue as blocked",
            SessionStatus.NEEDS_HUMAN: "Agent requested human input",
            SessionStatus.TIMED_OUT: f"Exceeded {session.agent_config.timeout_minutes} min timeout",
            SessionStatus.FAILED: "Session ended without PR or status update",
        }
        status_reason = status_reasons.get(status, "Unknown")

        return SessionHistoryEntry(
            issue_number=session.issue.number,
            title=session.issue.title,
            agent_type=session.issue.agent_type or "unknown",
            status=status.value,
            runtime_minutes=session.runtime_minutes,
            pr_url=pr_url,
            status_reason=status_reason,
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
                "session_id": session.tmux_session_name,
                "pr_url": pr_url,
                "runtime_minutes": session.runtime_minutes,
            }))
        elif status == SessionStatus.FAILED or status == SessionStatus.TIMED_OUT:
            self.events.publish(TraceEvent(EventName.SESSION_FAILED, {
                "issue_number": session.issue.number,
                "session_id": session.tmux_session_name,
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

        logger.debug(f"[STATE_MACHINE] Triggering transitions for session {session.tmux_session_name}")

        # 1. Update session state machine
        self._update_session_machine(session, status, status_reason)

        # 2. Update issue state machine
        self._update_issue_machine(session, status, pr_url)

        # 3. Update review state machine (if this is a review session)
        is_review = session.tmux_session_name.startswith("review-")
        if is_review and status == SessionStatus.COMPLETED:
            self._update_review_machine(session)

    def _update_session_machine(
        self,
        session: Session,
        status: SessionStatus,
        status_reason: str,
    ) -> None:
        """Update the session state machine."""
        session_machine = self._get_session_machine(session.tmux_session_name)
        if session_machine:
            logger.debug(f"[STATE_MACHINE] Found session machine for {session.tmux_session_name}")
            if status == SessionStatus.COMPLETED:
                logger.info(f"[STATE_MACHINE] Session {session.tmux_session_name}: RUNNING -> COMPLETED")
                session_machine.complete()  # type: ignore[attr-defined]
            elif status == SessionStatus.FAILED:
                logger.info(f"[STATE_MACHINE] Session {session.tmux_session_name}: RUNNING -> FAILED (reason: {status_reason})")
                session_machine.fail(data={'reason': status_reason})  # type: ignore[attr-defined]
            elif status == SessionStatus.TIMED_OUT:
                logger.info(f"[STATE_MACHINE] Session {session.tmux_session_name}: RUNNING -> TIMED_OUT")
                session_machine.timeout()  # type: ignore[attr-defined]
            elif status == SessionStatus.BLOCKED:
                logger.info(f"[STATE_MACHINE] Session {session.tmux_session_name}: RUNNING -> BLOCKED")
                session_machine.block()  # type: ignore[attr-defined]
            elif status == SessionStatus.NEEDS_HUMAN:
                logger.info(f"[STATE_MACHINE] Session {session.tmux_session_name}: RUNNING -> NEEDS_HUMAN")
                session_machine.needs_human()  # type: ignore[attr-defined]
        else:
            logger.debug(f"[STATE_MACHINE] No session machine found for {session.tmux_session_name} (may be restored session)")

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
            if status == SessionStatus.COMPLETED and pr_url:
                logger.info(f"[STATE_MACHINE] Issue #{session.issue.number}: IN_PROGRESS -> PR_PENDING (PR: {pr_url})")
                issue_machine.pr_created(data={'pr_url': pr_url})  # type: ignore[attr-defined]
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
        # Extract PR number from review session name (e.g., "review-123")
        match = re.match(r"review-(\d+)", session.tmux_session_name)
        if not match:
            return

        pr_number_review = int(match.group(1))
        review_machine = self._get_review_machine(pr_number_review)
        if not review_machine:
            logger.debug(f"[STATE_MACHINE] No review machine found for PR #{pr_number_review}")
            return

        logger.debug(f"[STATE_MACHINE] Found review machine for PR #{pr_number_review}")
        # Check PR labels to determine outcome via adapter (no subprocess)
        # The agent-done script adds either code-reviewed or needs-rework label
        try:
            pr_info = self.repository_host.get_pr(pr_number_review)
            if pr_info:
                labels = pr_info.labels
                if self.config.code_reviewed_label and self.config.code_reviewed_label in labels:
                    # Review was approved
                    logger.info(f"[STATE_MACHINE] PR #{pr_number_review}: IN_REVIEW -> APPROVED")
                    review_machine.approve()  # type: ignore[attr-defined]
                elif self.config.get_label_needs_rework() in labels:
                    # Changes requested
                    logger.info(f"[STATE_MACHINE] PR #{pr_number_review}: IN_REVIEW -> CHANGES_REQUESTED")
                    review_machine.request_changes()  # type: ignore[attr-defined]
                    logger.info(f"[STATE_MACHINE] PR #{pr_number_review}: CHANGES_REQUESTED -> REWORK_PENDING")
                    review_machine.queue_rework()  # type: ignore[attr-defined]
        except Exception as e:
            logger.warning(f"Failed to check PR labels for review outcome: {e}")

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
        is_work_session = not session.tmux_session_name.startswith(("review-", "rework-"))
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
            pending_cleanup = PendingCleanup(
                issue=session.issue,
                pr_number=pr_number,
                pr_url=pr_url,
                branch_name=session.branch_name,
                terminal_session_name=session.tmux_session_name,
                worktree_path=session.worktree_path,
            )
            logger.info(f"[CLEANUP] Deferred cleanup for #{session.issue.number} until review completes")

        return should_defer, pending_cleanup

    def _should_queue_review(
        self,
        session: Session,
        status: SessionStatus,
        pr_url: Optional[str],
    ) -> bool:
        """Determine if code review should be queued for this session."""
        is_review_session = session.tmux_session_name.startswith("review-")

        if pr_url and self.config.code_review_agent and not session.agent_config.skip_review and not is_review_session:
            logger.info(f"[REVIEW] Session #{session.issue.number} completed with PR, queuing code review")
            return True
        elif pr_url and is_review_session:
            logger.info(f"[REVIEW] Review session {session.tmux_session_name} completed - no re-queue needed")
        elif pr_url and not self.config.code_review_agent:
            logger.info(f"[REVIEW] Session #{session.issue.number} completed but code review not configured")
        elif pr_url and session.agent_config.skip_review:
            logger.info(f"[REVIEW] Session #{session.issue.number} skipping review (skip_review=true)")
        elif not pr_url:
            logger.info(f"[REVIEW] Session #{session.issue.number} completed but no PR found")

        return False
