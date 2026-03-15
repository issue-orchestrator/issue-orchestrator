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
from datetime import datetime
from typing import TYPE_CHECKING, Any, Optional, Callable

if TYPE_CHECKING:
    from ..domain.state_machines.issue_machine import IssueStateMachine
    from ..domain.state_machines.session_machine import SessionStateMachine
    from ..domain.state_machines.review_machine import ReviewStateMachine
    from ..domain.models import PendingReview, PendingRework, PendingTriageReview
    from .state_machine_manager import StateMachineManager
    from .label_manager import LabelManager

from ..domain.issue_key import StableIssueId
from ..infra.config import Config
from ..events import EventName
from ..infra.logging_config import log_context, get_repo_log_path
from ..domain.models import Session, SessionStatus, SessionHistoryEntry, PendingCleanup
from ..domain.session_key import TaskKind
from ..ports import EventSink,  make_trace_event, RepositoryHost, Issue
from ..ports.session_output import SessionOutput
from .actions import Action, AddLabelAction, RemoveLabelAction, AddCommentAction
from .completion_processor import (
    ERROR_PREFIX_PUSH,
    ERROR_PREFIX_CREATE_PR,
    ERROR_PREFIX_PUBLISH_BLOCKED,
)
from .reconciliation import build_expected_for_mutation, ExpectedState
from ..domain.triage_manifest import TriageManifest
from pathlib import Path
from ..infra.run_audit import write_run_audit

logger = logging.getLogger(__name__)


def _read_triage_manifest(session: Session) -> TriageManifest | None:
    """Read triage manifest from session if it exists.

    The triage_manifest path is stored in the session's run manifest.json
    during launch via ctx.update_manifest({"triage_manifest": path}).

    Returns None if:
    - Session has no worktree path
    - No run directory with triage_manifest path exists
    - Manifest file doesn't exist or can't be parsed
    """
    if not session.worktree_path:
        return None

    worktree = Path(session.worktree_path)
    sessions_dir = worktree / ".issue-orchestrator" / "sessions"
    if not sessions_dir.exists():
        return None

    # Find the run directory that has a triage_manifest entry in its manifest.json
    for run_dir in sessions_dir.iterdir():
        if not run_dir.is_dir():
            continue

        run_manifest_path = run_dir / "manifest.json"
        if not run_manifest_path.exists():
            continue

        try:
            import json
            run_manifest = json.loads(run_manifest_path.read_text())
            triage_manifest_path = run_manifest.get("triage_manifest")
            if triage_manifest_path:
                manifest_path = Path(triage_manifest_path)
                if manifest_path.exists():
                    return TriageManifest.read(manifest_path)
                else:
                    logger.warning(
                        "[triage] Manifest path in run manifest doesn't exist: %s",
                        manifest_path
                    )
        except Exception as e:
            logger.warning(
                "[triage] Failed to read manifest from %s: %s",
                run_dir, e, exc_info=True
            )

    return None


def _has_critical_errors(processing_errors: Optional[list[str]]) -> bool:
    """Check if processing_errors contains critical publish/finalize failures."""
    if not processing_errors:
        return False
    return any(
        error.startswith(ERROR_PREFIX_PUSH)
        or error.startswith(ERROR_PREFIX_CREATE_PR)
        or error.startswith(ERROR_PREFIX_PUBLISH_BLOCKED)
        for error in processing_errors
    )


def _has_review_exchange_errors(processing_errors: Optional[list[str]]) -> bool:
    """Check if processing_errors contains review exchange halt/failure markers."""
    if not processing_errors:
        return False
    return any(error.startswith("review_exchange:") for error in processing_errors)


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
        remove_session_machine_fn: Callable[[str], None] | None = None,
        label_manager: "LabelManager | None" = None,
    ):
        self.config = config
        self.events = events
        self.repository_host = repository_host
        self._get_issue_machine = get_issue_machine_fn
        self._get_session_machine = get_session_machine_fn
        self._get_review_machine = get_review_machine_fn
        self._session_output = session_output
        self._remove_session_machine = remove_session_machine_fn
        if label_manager is None:
            from .label_manager import LabelManager
            label_manager = LabelManager(config)
        self._lm = label_manager

    def _interrupted_retry_mode(self, session: Session) -> str | None:
        """Map session type to interrupted-retry mode."""
        if session.terminal_id.startswith("issue-") or session.terminal_id.startswith("rework-"):
            return "coding"
        if session.terminal_id.startswith("review-"):
            return "review"
        return None

    def _interrupted_retry_guard_label(self, mode: str) -> str:
        retry_cfg = self.config.retry.interrupted_sessions
        if mode == "coding":
            return retry_cfg.coding_guard_label
        return retry_cfg.review_guard_label

    def _is_interrupted_retry_enabled(self, mode: str) -> bool:
        retry_cfg = self.config.retry.interrupted_sessions
        if not retry_cfg.enabled:
            return False
        if mode == "coding":
            return retry_cfg.retry_coding
        if mode == "review":
            return retry_cfg.retry_review
        return False

    def _issue_has_label(self, issue_number: int, label: str) -> bool:
        """Best-effort label check from GitHub to guard retry loops."""
        try:
            issue = self.repository_host.get_issue(issue_number)
            if not issue:
                return False
            return label in issue.labels
        except Exception as exc:
            logger.warning(
                "[COMPLETION] Failed to read labels for issue #%d while evaluating interrupted retry: %s",
                issue_number,
                exc,
            )
            return False

    def _generate_interrupted_retry_actions(
        self,
        session: Session,
        expected: ExpectedState,
    ) -> list[Action] | None:
        """Generate auto-retry actions for interrupted sessions when configured."""
        mode = self._interrupted_retry_mode(session)
        if mode is None or not self._is_interrupted_retry_enabled(mode):
            return None

        guard_label = self._interrupted_retry_guard_label(mode)
        if self._issue_has_label(session.issue.number, guard_label):
            logger.info(
                "[COMPLETION] Interrupted auto-retry skipped for issue #%d (%s): guard label already present (%s)",
                session.issue.number,
                mode,
                guard_label,
            )
            return None

        session_kind = session.terminal_id.split("-", 1)[0]
        actions: list[Action] = [
            AddLabelAction(
                issue_number=session.issue.number,
                label=guard_label,
                reason=f"interrupted {mode} session auto-retry guard",
                expected=expected,
            ),
            AddCommentAction(
                number=session.issue.number,
                comment=(
                    f"🔁 **{session_kind.capitalize()} Session Interrupted**\n\n"
                    f"The {session_kind} session exited without a completion record (`completion command`).\n\n"
                    f"- Runtime: {session.runtime_minutes:.1f} minutes\n"
                    f"- Session: `{session.terminal_id}`\n\n"
                    "Auto-retry is enabled, so this will be retried on the next scheduler cycle.\n"
                    f"A guard label (`{guard_label}`) was added to prevent retry loops."
                ),
                reason=f"Notify interrupted {mode} session auto-retry",
                expected=expected,
            ),
        ]
        if session.terminal_id.startswith("issue-"):
            actions.append(RemoveLabelAction(
                issue_number=session.issue.number,
                label=self._lm.in_progress,
                reason="Interrupted issue session - releasing claim for auto-retry",
                expected=expected,
            ))
        return actions

    def _is_triage_session(self, session: Session) -> bool:
        """Check if this session is a triage review session."""
        if not self.config.triage_review_agent:
            return False
        # Check if the session's agent type matches triage agent
        return session.issue.agent_type == self.config.triage_review_agent

    def mark_session_retry(self, session: Session, reason: str) -> None:
        """Mark a session terminal when it will be retried.

        Validation retries re-launch a session with the same name. Ensure the
        existing session state machine reaches a terminal state so the next
        launch can create a fresh machine without invalid transitions.
        """
        session_machine = self._get_session_machine(session.terminal_id)
        if not session_machine:
            return
        if session_machine.can_transition("fail"):
            logger.info(
                "[STATE_MACHINE] Session %s: RUNNING -> FAILED (reason: %s)",
                session.terminal_id,
                reason,
            )
            session_machine.fail(data={'reason': reason})  # type: ignore[attr-defined]
        if self._remove_session_machine is not None:
            self._remove_session_machine(session.terminal_id)

    def _generate_triage_actions(
        self,
        session: Session,
        status: SessionStatus,
        processing_errors: Optional[list[str]],
        expected: ExpectedState,
    ) -> list[Action]:
        """Generate label actions for triage session completion.

        Adds triage-reviewed or triage-failed labels to all PRs in the manifest.
        """
        actions: list[Action] = []
        session_kind = session.terminal_id.split("-", 1)[0]

        if session_kind != "issue" or not self._is_triage_session(session):
            return actions

        triage_manifest = _read_triage_manifest(session)
        if not triage_manifest or not triage_manifest.prs:
            return actions

        # Determine which label to add based on success/failure
        if status == SessionStatus.COMPLETED and not _has_critical_errors(processing_errors):
            triage_label = self.config.triage_reviewed_label or "triage-reviewed"
            reason = "Triage completed successfully"
        else:
            triage_label = self.config.triage_failed_label or "triage-failed"
            reason = "Triage session failed"

        logger.info(
            "[triage] Adding '%s' label to %d PRs",
            triage_label, len(triage_manifest.prs)
        )

        for pr in triage_manifest.prs:
            actions.append(AddLabelAction(
                issue_number=pr.number,
                label=triage_label,
                reason=reason,
                expected=expected,
            ))

        return actions

    def process_completion(
        self,
        session: Session,
        status: SessionStatus,
        pr_url_hint: Optional[str] = None,
        processing_errors: Optional[list[str]] = None,
        diagnostic_path: Optional[str] = None,
        review_exchange_completed: bool = False,
        review_exchange_halted: bool = False,
        blocked_label: Optional[str] = None,
        blocked_reason: Optional[str] = None,
        completion_detail: Optional[dict[str, Any]] = None,
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

        review_exchange_halted = review_exchange_halted or _has_review_exchange_errors(processing_errors)

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
        elif status == SessionStatus.COMPLETED and review_exchange_halted:
            logger.info(
                "[COMPLETION] Review exchange halted - using FAILED for history/trace: issue=%d",
                session.issue.number,
            )
            history_status = SessionStatus.FAILED
            history_status_reason = "Review exchange halted"

        # Create history entry
        history_entry = self._create_history_entry(
            session, history_status, pr_url, status_reason_override=history_status_reason
        )

        # Emit trace events
        self._emit_trace_events(
            session, history_status, pr_url, pr_number,
            blocked_reason=blocked_reason,
            completion_detail=completion_detail,
        )

        # Update state machines
        self._update_state_machines(session, history_status, pr_url)

        # Determine cleanup strategy
        should_defer, pending_cleanup = self._determine_cleanup_strategy(
            session, status, pr_url, pr_number
        )

        # Determine if we should queue code review
        should_queue_review = self._should_queue_review(
            session,
            status,
            pr_url,
            pr_number,
            review_exchange_completed=review_exchange_completed,
            review_exchange_halted=review_exchange_halted,
        )

        # Generate actions for label/comment changes (policy logic)
        completion_actions = list(self.generate_completion_actions(
            session, status, processing_errors=processing_errors,
            diagnostic_path=diagnostic_path,
            review_exchange_halted=review_exchange_halted,
            blocked_label=blocked_label,
            blocked_reason=blocked_reason,
        ))
        if review_exchange_completed and pr_url:
            completion_actions.append(AddLabelAction(
                issue_number=session.issue.number,
                label=self._lm.pr_pending,
                reason="review exchange completed - awaiting merge",
                expected=build_expected_for_mutation(),
            ))
        completion_actions = tuple(completion_actions)

        if status in (
            SessionStatus.FAILED,
            SessionStatus.TIMED_OUT,
            SessionStatus.BLOCKED,
            SessionStatus.NEEDS_HUMAN,
        ) or processing_errors:
            log_path = get_repo_log_path(self.config.repo_root)
            run_dir = self._resolve_session_run_dir(session)
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

        # Enrich manifest with runtime context + log tail
        self._enrich_manifest_runtime(session, status)

        audit_actions = self._maybe_create_run_audit(
            session,
            status,
            processing_errors=processing_errors,
        )
        if audit_actions:
            completion_actions = tuple(list(completion_actions) + list(audit_actions))

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

    def _maybe_create_run_audit(
        self,
        session: Session,
        status: SessionStatus,
        *,
        processing_errors: Optional[list[str]] = None,
    ) -> tuple[Action, ...]:
        """Persist a run audit when explicitly requested by issue label."""
        labels = self._fetch_issue_labels_for_audit(session.issue.number)
        if self._lm.run_audit_requested not in labels:
            return ()
        if self._lm.run_audit_completed in labels:
            return ()

        run_dir = self._resolve_session_run_dir(session)
        if not run_dir:
            logger.warning(
                "[RUN_AUDIT] Requested for issue #%d but no run dir was available",
                session.issue.number,
            )
            return ()

        try:
            audit = write_run_audit(
                run_dir,
                issue_labels=labels,
                trigger_label=self._lm.run_audit_requested,
                completion_label=self._lm.run_audit_completed,
                processing_errors=processing_errors,
            )
            self._session_output.update_manifest(run_dir, {"run_audit_path": str(audit.path)})
        except Exception:
            logger.warning(
                "[RUN_AUDIT] Failed for issue #%d run_dir=%s",
                session.issue.number,
                run_dir,
                exc_info=True,
            )
            return ()

        expected = build_expected_for_mutation()
        logger.info(
            "[RUN_AUDIT] Wrote %s for issue #%d status=%s",
            audit.path,
            session.issue.number,
            status.value,
        )
        return (
            RemoveLabelAction(
                issue_number=session.issue.number,
                label=self._lm.run_audit_requested,
                reason="Run audit captured for this session",
                expected=expected,
            ),
            AddLabelAction(
                issue_number=session.issue.number,
                label=self._lm.run_audit_completed,
                reason="Run audit captured for this session",
                expected=expected,
            ),
        )

    def _fetch_issue_labels_for_audit(self, issue_number: int) -> list[str]:
        getter = getattr(self.repository_host, "get_issue_labels_fresh", None)
        if callable(getter):
            try:
                labels = getter(issue_number)
                if isinstance(labels, list):
                    return [str(label) for label in labels]
            except Exception as exc:
                logger.warning(
                    "[RUN_AUDIT] Fresh label read failed for issue #%d: %s",
                    issue_number,
                    exc,
                )

        try:
            issue = self.repository_host.get_issue(issue_number)
        except Exception as exc:
            logger.warning(
                "[RUN_AUDIT] Issue lookup failed for issue #%d: %s",
                issue_number,
                exc,
            )
            return []
        labels = getattr(issue, "labels", []) if issue is not None else []
        return [str(label) for label in labels]

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
        pr_url = None
        pr_number = None
        prs = None

        if status != SessionStatus.COMPLETED:
            return pr_url, pr_number, prs

        if pr_url_hint:
            return self._fetch_pr_info_from_hint(session, pr_url_hint)

        return self._fetch_pr_info_from_branch_or_review_fallback(session)

    def _fetch_pr_info_from_hint(
        self,
        session: Session,
        pr_url_hint: str,
    ) -> tuple[Optional[str], Optional[int], Optional[list[Any]]]:
        pr_url = pr_url_hint
        pr_number: Optional[int] = None
        prs: Optional[list[Any]] = None

        match = re.search(r"/pull/(\d+)", pr_url)
        if match:
            pr_number = int(match.group(1))
            try:
                pr_info = self.repository_host.get_pr(pr_number)
            except Exception as e:
                logger.warning("Failed to fetch PR %s for PR hint: %s", pr_number, e)
            else:
                if pr_info:
                    prs = [pr_info]

        logger.info(
            "[PR_HINT] Using PR from completion processor: %s (number=%s)",
            pr_url,
            pr_number,
            extra=log_context(issue_key=session.key.issue.stable_id(), session_id=session.terminal_id),
        )
        return pr_url, pr_number, prs

    def _fetch_pr_info_from_branch_or_review_fallback(
        self,
        session: Session,
    ) -> tuple[Optional[str], Optional[int], Optional[list[Any]]]:
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
            return pr_infos[0].url, pr_infos[0].number, list(pr_infos)

        if session.pr_number is None:
            return None, None, None

        try:
            review_pr = self.repository_host.get_pr(session.pr_number)
        except Exception as e:
            logger.warning(
                "Failed to fetch PR %s for review session fallback: %s",
                session.pr_number,
                e,
            )
            return None, None, None

        if review_pr:
            return review_pr.url, review_pr.number, [review_pr]

        return None, None, None

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
            SessionStatus.COMPLETED: "Completed without PR",
            SessionStatus.BLOCKED: "Agent marked issue as blocked",
            SessionStatus.NEEDS_HUMAN: "Agent requested human input",
            SessionStatus.TIMED_OUT: f"Exceeded {session.agent_config.timeout_minutes} min timeout",
            SessionStatus.FAILED: "Session ended without PR or status update",
        }
        if status == SessionStatus.COMPLETED and pr_url:
            status_reasons[SessionStatus.COMPLETED] = "PR created successfully"
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
            completed_at=datetime.now(),
        )

    def _emit_trace_events(
        self,
        session: Session,
        status: SessionStatus,
        pr_url: Optional[str],
        pr_number: Optional[int],
        *,
        blocked_reason: Optional[str] = None,
        completion_detail: Optional[dict[str, Any]] = None,
    ) -> None:
        """Emit trace events for session completion.

        ``completion_detail`` carries curated fields from the CompletionRecord
        so downstream consumers (timeline, UI) get the rich agent-provided data
        without rummaging across files.
        """
        detail = completion_detail or {}

        if status == SessionStatus.COMPLETED:
            self._emit_completed_events(session, pr_url, pr_number, detail)
        elif status in (SessionStatus.FAILED, SessionStatus.TIMED_OUT):
            self._emit_failure_event(session, status)
        elif status == SessionStatus.BLOCKED:
            self._emit_blocked_event(session, blocked_reason, detail)
        elif status == SessionStatus.NEEDS_HUMAN:
            self._emit_needs_human_event(session, blocked_reason, detail)

    def _emit_completed_events(
        self,
        session: Session,
        pr_url: Optional[str],
        pr_number: Optional[int],
        detail: dict[str, Any],
    ) -> None:
        """Emit events for a completed session (coding/rework only)."""
        # Review sessions get their events from _publish_review_outcome()
        if session.key.task == TaskKind.REVIEW:
            return

        agent = session.agent_label
        task = session.key.task.value if session.key else None
        rework_cycle = session.rework_cycle

        payload: dict[str, Any] = {
            "issue_number": session.issue.number,
            "session_id": session.terminal_id,
            "agent": agent,
            "task": task,
            "rework_cycle": rework_cycle,
            "pr_url": pr_url,
            "runtime_minutes": session.runtime_minutes,
        }
        completion_path_absolute = detail.get("completion_path_absolute")
        if isinstance(completion_path_absolute, str) and completion_path_absolute.strip():
            payload["completion_path_absolute"] = completion_path_absolute
        else:
            payload["completion_path_absolute"] = str((session.worktree_path / session.completion_path).resolve())
        run_dir = self._resolve_session_run_dir(session)
        if run_dir:
            payload["run_dir"] = str(run_dir)
        for key in ("implementation", "problems", "review_summary", "review_issues", "risk_level"):
            if detail.get(key):
                payload[key] = detail[key]
        self.events.publish(make_trace_event(EventName.SESSION_COMPLETED, payload))

        if pr_url and pr_number is not None:
            self.events.publish(make_trace_event(EventName.ISSUE_PR_CREATED, {
                "issue_number": session.issue.number,
                "pr_url": pr_url,
                "pr_number": pr_number,
                "agent": agent,
                "task": task,
                "rework_cycle": rework_cycle,
            }))

    def _emit_failure_event(
        self,
        session: Session,
        status: SessionStatus,
    ) -> None:
        """Emit SESSION_FAILED event for failed or timed-out sessions."""
        reason = (
            f"Exceeded {session.agent_config.timeout_minutes} min timeout"
            if status == SessionStatus.TIMED_OUT
            else "Session ended without PR or status update"
        )
        payload: dict[str, Any] = {
            "issue_number": session.issue.number,
            "session_id": session.terminal_id,
            "agent": session.agent_label,
            "task": session.key.task.value if session.key else None,
            "rework_cycle": session.rework_cycle,
            "error": reason,
            "runtime_minutes": session.runtime_minutes,
            "timeout_minutes": session.agent_config.timeout_minutes if session.agent_config else None,
        }
        run_dir = self._resolve_session_run_dir(session)
        if run_dir:
            payload["run_dir"] = str(run_dir)
        self.events.publish(make_trace_event(EventName.SESSION_FAILED, payload))

    def _emit_blocked_event(
        self,
        session: Session,
        blocked_reason: Optional[str],
        detail: dict[str, Any],
    ) -> None:
        """Emit ISSUE_BLOCKED event."""
        payload: dict[str, Any] = {
            "issue_number": session.issue.number,
            "agent": session.agent_label,
            "task": session.key.task.value if session.key else None,
            "rework_cycle": session.rework_cycle,
            "reason": blocked_reason or "Agent marked issue as blocked",
        }
        for key in ("attempted", "blocked_by"):
            if detail.get(key):
                payload[key] = detail[key]
        self.events.publish(make_trace_event(EventName.ISSUE_BLOCKED, payload))

    def _emit_needs_human_event(
        self,
        session: Session,
        blocked_reason: Optional[str],
        detail: dict[str, Any],
    ) -> None:
        """Emit ISSUE_NEEDS_HUMAN event."""
        payload: dict[str, Any] = {
            "issue_number": session.issue.number,
            "agent": session.agent_label,
            "task": session.key.task.value if session.key else None,
            "rework_cycle": session.rework_cycle,
            "reason": blocked_reason or "Agent requested human input",
        }
        if detail.get("question"):
            payload["question"] = detail["question"]
        self.events.publish(make_trace_event(EventName.ISSUE_NEEDS_HUMAN, payload))

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

        # 3. Update review state machine
        is_review = session.terminal_id.startswith("review-")
        is_rework = session.terminal_id.startswith("rework-")
        if is_review and status == SessionStatus.COMPLETED:
            self._update_review_machine(session)
        elif is_rework and status == SessionStatus.COMPLETED:
            self._complete_rework_review_machine(session)

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
                # Publish review outcome events from state machine transitions
                self._publish_review_outcome(review_machine, session, pr_number_review)
        except Exception as e:
            logger.warning(f"Failed to check PR labels for review outcome: {e}")
            self.events.publish(make_trace_event(EventName.APPLY_FAILED, {
                "step_type": "review_outcome_check", "pr_number": pr_number_review,
                "issue_number": session.issue.number, "error": str(e),
            }))

    def _complete_rework_review_machine(self, session: Session) -> None:
        """Advance the review state machine after a rework session completes.

        Transitions: REWORK_PENDING → REWORK_IN_PROGRESS → IN_REVIEW
        This allows the next review to transition from IN_REVIEW properly.
        """
        pr_number = session.pr_number
        if not pr_number:
            logger.debug("[STATE_MACHINE] Rework session %s has no pr_number, skipping review machine update", session.terminal_id)
            return

        review_machine = self._get_review_machine(pr_number)
        if not review_machine:
            logger.debug("[STATE_MACHINE] No review machine found for PR #%d (rework session %s)", pr_number, session.terminal_id)
            return

        # Transition through start_rework and complete_rework
        if review_machine.can_transition("start_rework"):
            logger.info("[STATE_MACHINE] PR #%d: REWORK_PENDING -> REWORK_IN_PROGRESS", pr_number)
            review_machine.start_rework()  # type: ignore[attr-defined]
        if review_machine.can_transition("complete_rework"):
            logger.info("[STATE_MACHINE] PR #%d: REWORK_IN_PROGRESS -> IN_REVIEW", pr_number)
            review_machine.complete_rework()  # type: ignore[attr-defined]

    def _publish_review_outcome(
        self,
        review_machine: Any,
        session: Session,
        pr_number: int,
    ) -> None:
        """Publish review.approved or review.changes_requested events.

        These events are defined in EventName but were never emitted.
        The ReviewStateMachine stores the outcome in last_transition after
        approve() or request_changes() — we read it and publish.
        """
        tr = review_machine.last_transition
        if not tr:
            return

        _REVIEW_OUTCOME_EVENTS = {"review.approved", "review.changes_requested"}
        if tr.event_name not in _REVIEW_OUTCOME_EVENTS:
            return

        payload: dict[str, Any] = {
            **tr.data,
            "pr_number": pr_number,
            "reviewer_agent": session.agent_label,
            "rework_cycle": session.rework_cycle,
        }
        self.events.publish(make_trace_event(EventName(tr.event_name), payload))

    def _process_review_outcome(self, pr_info: Any, pr_number: int, review_machine: Any) -> None:
        """Process review outcome based on PR labels."""
        labels = pr_info.labels
        if self.config.code_reviewed_label and self.config.code_reviewed_label in labels:
            self._handle_review_approved(pr_info, pr_number, review_machine)
        elif self._lm.needs_rework in labels:
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
        issue_key: StableIssueId | None,
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
        self.events.publish(make_trace_event(EventName.PR_VIEW_CHANGED, payload))

    def _emit_pr_view_hint(
        self,
        pr_number: int,
        pr_url: str,
        issue_key: StableIssueId,
        issue_number: int,
    ) -> None:
        payload = {
            "pr_number": pr_number,
            "labels": [],
            "pr_url": pr_url,
            "issue_key": issue_key,
            "issue_number": issue_number,
        }
        self.events.publish(make_trace_event(EventName.PR_VIEW_CHANGED, payload))

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
                terminal_id=session.terminal_id,
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
        review_exchange_completed: bool = False,
        review_exchange_halted: bool = False,
    ) -> bool:
        """Determine if session should be added to discovered_reviews.

        Note: This returns True even for dry-run PRs (so pr-pending label gets added).
        The actual review queuing is controlled by the planner, which skips dry-run PRs.
        """
        is_review_session = session.terminal_id.startswith("review-")
        if review_exchange_completed:
            logger.info(
                "[REVIEW] Review exchange completed - skipping PR review queue",
            )
            return False
        if review_exchange_halted:
            logger.info(
                "[REVIEW] Review exchange halted - skipping PR review queue",
            )
            return False

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

    def _generate_processing_failure_actions(
        self,
        session: Session,
        critical_errors: list[str],
        diagnostic_path: Optional[str],
        expected: ExpectedState,
    ) -> list[Action]:
        """Generate actions when agent said completed but push/PR creation failed.

        Tracks consecutive publish failures via publish-fail-count-N labels.
        After max_consecutive_publish_failures, escalates to needs-human.
        """
        from pathlib import Path

        issue_number = session.issue.number
        in_progress_label = self._lm.in_progress

        # Count previous consecutive publish failures from issue labels
        prev_count = self._lm.extract_publish_fail_count(session.issue.labels)
        new_count = prev_count + 1
        max_failures = self.config.max_consecutive_publish_failures

        # Brief error hint for comment (not full details - those are in diagnostic file)
        first_error = critical_errors[0][:100] if critical_errors else "Unknown error"
        if len(first_error) == 100:
            first_error += "..."

        # Build diagnostic location info
        diagnostic_info = ""
        if diagnostic_path and session.worktree_path:
            worktree_name = Path(session.worktree_path).name
            diagnostic_info = f"\n**Diagnostic file:** `{worktree_name}/{diagnostic_path}`\n"

        # Escalate to needs-human after too many consecutive failures
        if new_count >= max_failures:
            logger.info(
                "[COMPLETION] Publish failure count %d >= max %d, escalating to needs-human: issue=%d",
                new_count, max_failures, issue_number,
            )
            return [
                AddLabelAction(
                    issue_number=issue_number,
                    label=self._lm.needs_human,
                    reason=f"Publishing failed {new_count} consecutive times — escalating to needs-human",
                    expected=expected,
                ),
                AddCommentAction(
                    number=issue_number,
                    comment=f"❌ **Publishing Failed — Escalated**\n\n"
                            f"Publishing has failed **{new_count} consecutive times** "
                            f"(max: {max_failures}). This issue needs human investigation.\n\n"
                            f"**Latest error:** {first_error}\n"
                            f"{diagnostic_info}\n"
                            f"- Runtime: {session.runtime_minutes:.1f} minutes\n"
                            f"- Session: `{session.terminal_id}`\n",
                    reason="Escalate repeated publish failure to human",
                    expected=expected,
                ),
                RemoveLabelAction(
                    issue_number=issue_number,
                    label=in_progress_label,
                    reason="Publishing failed - releasing claim",
                    expected=expected,
                ),
                RemoveLabelAction(
                    issue_number=issue_number,
                    label=self._lm.needs_rework,
                    reason="Publishing failed - clearing needs-rework to prevent re-queuing loop",
                    expected=expected,
                ),
            ]

        actions: list[Action] = [
            AddLabelAction(
                issue_number=issue_number,
                label=self._lm.publish_failed,
                reason="Publishing failed after agent completion (push/PR creation failed)",
                expected=expected,
            ),
            AddCommentAction(
                number=issue_number,
                comment=f"❌ **Publishing Failed** (attempt {new_count}/{max_failures})\n\n"
                        f"The agent completed its work, but the orchestrator could not push or create a PR.\n\n"
                        f"**Error:** {first_error}\n"
                        f"{diagnostic_info}\n"
                        f"- Runtime: {session.runtime_minutes:.1f} minutes\n"
                        f"- Session: `{session.terminal_id}`\n\n"
                        f"This issue has been marked as `{self._lm.publish_failed}` and will not be automatically retried.\n"
                        f"Remove the label to retry.",
                reason="Notify about processing failure",
                expected=expected,
            ),
            RemoveLabelAction(
                issue_number=issue_number,
                label=in_progress_label,
                reason="Processing failed - releasing claim",
                expected=expected,
            ),
            RemoveLabelAction(
                issue_number=issue_number,
                label=self._lm.needs_rework,
                reason="Publishing failed - clearing needs-rework to prevent re-queuing loop",
                expected=expected,
            ),
        ]

        # Update publish-fail-count label (remove old, add new)
        if prev_count > 0:
            actions.append(RemoveLabelAction(
                issue_number=issue_number,
                label=self._lm.publish_fail_count_label(prev_count),
                reason="Updating publish failure count",
                expected=expected,
            ))
        actions.append(AddLabelAction(
            issue_number=issue_number,
            label=self._lm.publish_fail_count_label(new_count),
            reason=f"Publish failure #{new_count}",
            expected=expected,
        ))

        return actions

    def _generate_timeout_actions(
        self,
        session: Session,
        expected: ExpectedState,
    ) -> list[Action]:
        """Generate actions when session timed out."""
        issue_number = session.issue.number
        in_progress_label = self._lm.in_progress
        is_issue_session = session.terminal_id.startswith("issue-")
        session_kind = session.terminal_id.split("-", 1)[0]

        if is_issue_session:
            timeout_mins = session.agent_config.timeout_minutes if session.agent_config else "unknown"
            return [
                AddLabelAction(
                    issue_number=issue_number,
                    label=self._lm.blocked_failed,
                    reason=f"Session timed out after {session.runtime_minutes} minutes",
                    expected=expected,
                ),
                AddCommentAction(
                    number=issue_number,
                    comment=f"⏱️ **Session Timed Out**\n\n"
                            f"The agent session exceeded the {timeout_mins} minute timeout limit.\n\n"
                            f"- Runtime: {session.runtime_minutes:.1f} minutes\n"
                            f"- Session: `{session.terminal_id}`\n\n"
                            f"This issue has been marked as `{self._lm.blocked_failed}` and will not be automatically retried.\n"
                            f"Remove the label to allow reprocessing.",
                    reason="Notify about session timeout",
                    expected=expected,
                ),
                RemoveLabelAction(
                    issue_number=issue_number,
                    label=in_progress_label,
                    reason="Session timed out - releasing claim",
                    expected=expected,
                ),
            ]
        else:
            return [
                AddCommentAction(
                    number=issue_number,
                    comment=f"⏱️ **{session_kind.capitalize()} Session Timed Out**\n\n"
                            f"The {session_kind} session exceeded its timeout and did not produce an outcome.\n\n"
                            f"- Runtime: {session.runtime_minutes:.1f} minutes\n"
                            f"- Session: `{session.terminal_id}`\n\n"
                            f"The PR remains pending; review will be retried automatically.",
                    reason=f"Notify about {session_kind} session timeout",
                    expected=expected,
                ),
            ]

    def _generate_failure_actions(
        self,
        session: Session,
        expected: ExpectedState,
    ) -> list[Action]:
        """Generate actions when session failed without completion command."""
        if retry_actions := self._generate_interrupted_retry_actions(session, expected):
            return retry_actions

        issue_number = session.issue.number
        in_progress_label = self._lm.in_progress
        is_issue_session = session.terminal_id.startswith("issue-")
        session_kind = session.terminal_id.split("-", 1)[0]

        if is_issue_session:
            return [
                AddLabelAction(
                    issue_number=issue_number,
                    label=self._lm.needs_human,
                    reason="Session terminated without calling completion command (mandatory)",
                    expected=expected,
                ),
                AddCommentAction(
                    number=issue_number,
                    comment=f"🔍 **Session Needs Investigation**\n\n"
                            f"The agent session terminated without calling the completion command "
                            f"(`coding-done` or `reviewer-done`).\n\n"
                            f"**This is unexpected** - the completion command is mandatory and must be called "
                            f"to complete any session (completed, blocked, or needs_human).\n\n"
                            f"- Runtime: {session.runtime_minutes:.1f} minutes\n"
                            f"- Session: `{session.terminal_id}`\n\n"
                            f"**Possible causes:**\n"
                            f"- Agent crashed or was interrupted\n"
                            f"- Orchestrator shutdown/restart interrupted the session lifecycle\n"
                            f"- Agent ignored the mandatory completion command requirement\n"
                            f"- Infrastructure issue prevented completion\n\n"
                            f"This issue has been marked as `{self._lm.needs_human}` for investigation.\n"
                            f"Remove the label after investigating to allow reprocessing.",
                    reason="Notify about session needing human investigation",
                    expected=expected,
                ),
                RemoveLabelAction(
                    issue_number=issue_number,
                    label=in_progress_label,
                    reason="Session failed - releasing claim",
                    expected=expected,
                ),
            ]
        else:
            return [
                AddCommentAction(
                    number=issue_number,
                    comment=f"🔍 **{session_kind.capitalize()} Session Needs Investigation**\n\n"
                            f"The {session_kind} session terminated without calling the completion command.\n\n"
                            f"**This is unexpected** - the completion command is mandatory.\n\n"
                            f"- Runtime: {session.runtime_minutes:.1f} minutes\n"
                            f"- Session: `{session.terminal_id}`\n\n"
                            f"Possible causes include orchestrator shutdown/restart, agent crash, or workflow interruption.\n\n"
                            f"The PR remains pending; please investigate what happened.",
                    reason=f"Notify about {session_kind} session needing investigation",
                    expected=expected,
                ),
            ]

    def _generate_blocked_actions(
        self,
        session: Session,
        expected: ExpectedState,
        blocked_label: Optional[str] = None,
        blocked_reason: Optional[str] = None,
    ) -> list[Action]:
        """Generate actions when agent explicitly reported blocked."""
        is_issue_session = session.terminal_id.startswith("issue-")
        label = blocked_label or self._lm.blocked

        if is_issue_session:
            reason_text = blocked_reason.strip() if blocked_reason else "No reason provided."
            return [
                AddLabelAction(
                    issue_number=session.issue.number,
                    label=label,
                    reason="Agent reported issue as blocked",
                    expected=expected,
                ),
                AddCommentAction(
                    number=session.issue.number,
                    comment=f"🚧 **Session Blocked**\n\n"
                            f"The agent reported this issue as blocked.\n\n"
                            f"**Reason:** {reason_text}\n"
                            f"- Runtime: {session.runtime_minutes:.1f} minutes\n"
                            f"- Session: `{session.terminal_id}`\n\n"
                            f"This issue has been marked as `{label}` and will not be automatically retried.\n"
                            f"Remove the label to allow reprocessing.",
                    reason="Notify about blocked session and reason",
                    expected=expected,
                ),
                RemoveLabelAction(
                    issue_number=session.issue.number,
                    label=self._lm.in_progress,
                    reason="Session blocked - releasing claim",
                    expected=expected,
                ),
            ]
        return []

    def _enrich_manifest_runtime(
        self,
        session: Session,
        status: SessionStatus,
    ) -> None:
        """Write runtime context and log tail into the run manifest.

        Best-effort — failures are logged but never block completion.
        """
        from ..domain.run_manifest import RunManifest

        if not session.worktree_path:
            return

        run_dir = self._resolve_session_run_dir(session)
        if not run_dir:
            return

        try:
            manifest = RunManifest.load(run_dir)
        except Exception as exc:
            logger.warning(
                "[MANIFEST] Failed to load manifest for runtime enrichment: %s", exc,
            )
            return

        manifest.runtime_minutes = session.runtime_minutes
        if session.agent_config:
            manifest.timeout_minutes = session.agent_config.timeout_minutes

        # Capture log tail for all outcomes
        log_path = self._session_output.get_log_path(
            session.worktree_path, session.terminal_id
        )
        if log_path and log_path.exists():
            try:
                content = log_path.read_text()
                lines = content.strip().split("\n")
                manifest.log_tail = "\n".join(lines[-20:])
            except Exception as exc:
                logger.debug("[MANIFEST] Could not read log tail: %s", exc)

        try:
            manifest.save()
        except Exception as exc:
            logger.warning("[MANIFEST] Failed to save runtime enrichment: %s", exc)

    def _resolve_session_run_dir(self, session: Session) -> Path | None:
        """Resolve run_dir for events/diagnostics, including issue-scoped fallback.

        Some provider-backed runs use a phase session name in run artifacts while
        session.terminal_id carries a legacy issue-* token. Prefer exact lookup,
        then fall back to latest run in this worktree when it belongs to the same issue.
        """
        run_dir = self._session_output.find_run_dir(session.worktree_path, session.terminal_id)
        if run_dir:
            return run_dir
        fallback = self._session_output.find_run_dir(session.worktree_path)
        if not fallback:
            return None
        manifest = self._session_output.read_manifest(fallback) or {}
        if manifest.get("issue_number") == session.issue.number:
            return fallback
        return None

    def generate_completion_actions(
        self,
        session: Session,
        status: SessionStatus,
        processing_errors: Optional[list[str]] = None,
        diagnostic_path: Optional[str] = None,
        review_exchange_halted: bool = False,
        blocked_label: Optional[str] = None,
        blocked_reason: Optional[str] = None,
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
        expected = build_expected_for_mutation()

        # Check for critical processing errors (push/PR creation failures)
        critical_errors = [
            error for error in (processing_errors or [])
            if (
                error.startswith(ERROR_PREFIX_PUSH)
                or error.startswith(ERROR_PREFIX_CREATE_PR)
                or error.startswith(ERROR_PREFIX_PUBLISH_BLOCKED)
            )
        ]

        # If agent said "completed" but critical processing failed, treat as blocked-failed
        if status == SessionStatus.COMPLETED and critical_errors:
            logger.info(
                "[COMPLETION] Agent said completed but processing failed: issue=%d errors=%s",
                session.issue.number, critical_errors
            )
            return tuple(self._generate_processing_failure_actions(
                session, critical_errors, diagnostic_path, expected
            ))

        if status == SessionStatus.COMPLETED and review_exchange_halted:
            logger.info(
                "[COMPLETION] Review exchange halted - generating blocked-failed actions: issue=%d",
                session.issue.number,
            )
            return tuple(self._generate_review_exchange_halted_actions(session, expected))

        # Dispatch to status-specific action generators
        if status == SessionStatus.TIMED_OUT:
            return tuple(self._generate_timeout_actions(session, expected))

        if status == SessionStatus.FAILED:
            return tuple(self._generate_failure_actions(session, expected))

        if status == SessionStatus.BLOCKED:
            return tuple(self._generate_blocked_actions(
                session,
                expected,
                blocked_label=blocked_label,
                blocked_reason=blocked_reason,
            ))

        if status == SessionStatus.COMPLETED:
            # POLICY: Completion → release in-progress (claim maintained via pr-pending)
            actions: list[Action] = [RemoveLabelAction(
                issue_number=session.issue.number,
                label=self._lm.in_progress,
                reason="Session completed successfully",
                expected=expected,
            )]
            # Triage session completion: add labels to all PRs in the manifest
            actions.extend(self._generate_triage_actions(session, status, processing_errors, expected))
            return tuple(actions)

        # Note: NEEDS_HUMAN keeps in-progress label to maintain ownership claim
        # This is intentional policy - the issue is still being worked on
        return ()

    def _generate_review_exchange_halted_actions(
        self,
        session: Session,
        expected: ExpectedState,
    ) -> list[Action]:
        """Generate hold actions when a review exchange halts without progress."""
        issue_number = session.issue.number
        return [
            AddLabelAction(
                issue_number=issue_number,
                label=self._lm.blocked_failed,
                reason="Review exchange halted with no progress",
                expected=expected,
            ),
            AddCommentAction(
                number=issue_number,
                comment=(
                    "⚠️ **Review Exchange Halted**\n\n"
                    "The automated review exchange stopped because it could not make further progress.\n\n"
                    f"- Session: `{session.terminal_id}`\n"
                    f"- Runtime: {session.runtime_minutes:.1f} minutes\n\n"
                    f"This issue has been marked as `{self._lm.blocked_failed}` and will not be retried automatically.\n"
                    "Use Retry/Unblock when you want to run it again."
                ),
                reason="Notify that review exchange halted and issue is on hold",
                expected=expected,
            ),
            RemoveLabelAction(
                issue_number=issue_number,
                label=self._lm.in_progress,
                reason="Review exchange halted - releasing claim",
                expected=expected,
            ),
        ]


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
