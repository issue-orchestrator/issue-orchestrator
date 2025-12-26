"""ActionApplier - executes actions via ports/adapters.

This is the IO boundary for the orchestrator. It:
1. Takes Action objects (the plan)
2. Executes them via injected ports
3. Emits trace events for each action
4. Returns ActionResults

When reconciliation is enabled (reconcile=True with issue_tracker provided):
- Before any label mutation, fetches current labels
- Verifies current state is as expected
- Aborts with ReconciliationRequired if mismatch
- Only proceeds with mutation if state matches

Usage:
    applier = ActionApplier(
        labels=label_set,
        sessions=session_manager,
        events=event_sink,
        repository_host=github_adapter,  # For issue creation, label sync
        worktree_manager=git_worktree_manager,  # For worktree removal
        issue_tracker=github_adapter,  # Optional, for reconciliation
        reconcile=True,  # Enable reconciliation
    )
    results = applier.apply_all(actions)
"""

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional, Sequence, TYPE_CHECKING

from ..ports import EventSink, TraceEvent
from ..ports.label_set import LabelSet
from ..ports.issue_tracker import IssueTracker
from ..ports.repository_host import RepositoryHost
from ..ports.worktree_manager import WorktreeManager
from ..models import Session
from .actions import (
    Action,
    ActionResult,
    ActionResultType,
    ActionType,
    AddLabelAction,
    RemoveLabelAction,
    SyncLabelsAction,
    LaunchSessionAction,
    StopSessionAction,
    TransitionAction,
    QueueReviewAction,
    QueueReworkAction,
    QueueTriageAction,
    EscalateToHumanAction,
    AddCommentAction,
    CreateTriageIssueAction,
    CleanupSessionAction,
    RemoveWorktreeAction,
)
from .session_manager import SessionManager, SessionRef, SessionType, SessionContext
from .reconciliation import (
    ExternalSnapshot,
    ExpectedState,
    ReconciliationRequired,
    check_reconciliation,
)

logger = logging.getLogger(__name__)

# Type alias for session launcher callback
# Takes (session_type, number) and returns Optional[Session]
# This allows orchestrator to inject entity lookup + SessionLauncher
SessionLauncherCallback = Callable[[str, int], Optional[Session]]


@dataclass
class ActionApplier:
    """Applies actions via ports/adapters.

    This is the IO boundary - all external calls go through here.
    Each action type has a handler that knows how to execute it.

    When reconciliation is enabled (reconcile=True):
    - Before label mutations, fetches current labels from issue_tracker
    - Verifies state hasn't changed unexpectedly
    - Emits reconciliation events for traceability
    """

    labels: LabelSet
    sessions: SessionManager
    events: EventSink
    repository_host: Optional[RepositoryHost] = None  # For issue creation, labels
    worktree_manager: Optional[WorktreeManager] = None  # For worktree operations
    issue_tracker: Optional[IssueTracker] = None
    reconcile: bool = False  # If True, verify state before mutations
    # Session launcher callback - handles entity lookup + launching
    # Injected by orchestrator, allows ActionApplier to launch sessions without
    # knowing about Issue/PendingReview/PendingRework entities
    session_launcher: Optional[SessionLauncherCallback] = None

    def apply(self, action: Action) -> ActionResult:
        """Apply a single action.

        Args:
            action: The action to apply

        Returns:
            ActionResult indicating success/failure
        """
        self._emit_action_start(action)

        try:
            result = self._dispatch(action)
        except Exception as e:
            logger.exception(f"Action failed: {action}")
            result = ActionResult.fail(action, str(e))

        self._emit_action_end(action, result)
        return result

    def apply_all(self, actions: Sequence[Action]) -> list[ActionResult]:
        """Apply multiple actions in sequence.

        Args:
            actions: The actions to apply

        Returns:
            List of ActionResults
        """
        return [self.apply(action) for action in actions]

    def _dispatch(self, action: Action) -> ActionResult:
        """Dispatch an action to the appropriate handler."""
        handlers: dict[ActionType, Callable[[Action], ActionResult]] = {
            ActionType.ADD_LABEL: self._apply_add_label,
            ActionType.REMOVE_LABEL: self._apply_remove_label,
            ActionType.SYNC_LABELS: self._apply_sync_labels,
            ActionType.LAUNCH_SESSION: self._apply_launch_session,
            ActionType.STOP_SESSION: self._apply_stop_session,
            # Queue operations - IO is handled here, state update by orchestrator
            ActionType.QUEUE_REVIEW: self._apply_queue_review,
            ActionType.QUEUE_REWORK: self._apply_queue_operation,
            ActionType.QUEUE_TRIAGE: self._apply_queue_operation,
            ActionType.ESCALATE_TO_HUMAN: self._apply_escalate,
            # Issue creation
            ActionType.CREATE_TRIAGE_ISSUE: self._apply_create_triage_issue,
            # Cleanup operations
            ActionType.CLEANUP_SESSION: self._apply_cleanup_session,
            ActionType.REMOVE_WORKTREE: self._apply_remove_worktree,
            # Comments
            ActionType.ADD_COMMENT: self._apply_add_comment,
        }

        handler = handlers.get(action.action_type)
        if handler is None:
            return ActionResult.skip(
                action, f"No handler for action type: {action.action_type}"
            )

        return handler(action)

    def _apply_add_label(self, action: Action) -> ActionResult:
        """Add a label to an issue."""
        assert isinstance(action, AddLabelAction)

        try:
            self.labels.add_label(action.issue_number, action.label)
            return ActionResult.ok(
                action,
                issue_number=action.issue_number,
                label=action.label,
            )
        except Exception as e:
            return ActionResult.fail(action, str(e))

    def _apply_remove_label(self, action: Action) -> ActionResult:
        """Remove a label from an issue."""
        assert isinstance(action, RemoveLabelAction)

        try:
            self.labels.remove_label(action.issue_number, action.label)
            return ActionResult.ok(
                action,
                issue_number=action.issue_number,
                label=action.label,
            )
        except Exception as e:
            return ActionResult.fail(action, str(e))

    def _apply_add_comment(self, action: Action) -> ActionResult:
        """Add a comment to an issue or PR."""
        assert isinstance(action, AddCommentAction)

        try:
            self.repository_host.add_comment(action.number, action.comment)
            return ActionResult.ok(
                action,
                number=action.number,
                is_pr=action.is_pr,
            )
        except Exception as e:
            return ActionResult.fail(action, str(e))

    def _fetch_current_labels(self, issue_number: int) -> set[str] | None:
        """Fetch current labels for an issue if issue_tracker is available.

        Returns:
            Set of label names, or None if issue_tracker not configured
        """
        if self.issue_tracker is None:
            return None
        try:
            labels = self.issue_tracker.get_issue_labels(issue_number)
            return set(labels)
        except Exception as e:
            logger.warning(
                "Failed to fetch labels for issue #%d: %s",
                issue_number, e
            )
            return None

    def _check_reconciliation_for_sync(
        self,
        issue_number: int,
        add_labels: tuple[str, ...],
        remove_labels: tuple[str, ...],
    ) -> tuple[bool, str, set[str]]:
        """Check reconciliation for a sync operation.

        Args:
            issue_number: Issue to check
            add_labels: Labels we plan to add
            remove_labels: Labels we plan to remove

        Returns:
            Tuple of (should_proceed, message, current_labels).
            If reconciliation is not enabled or can't run, returns (True, "", current_labels).
        """
        if not self.reconcile:
            return True, "", set()

        current = self._fetch_current_labels(issue_number)
        if current is None:
            # Can't verify - proceed with warning
            logger.warning(
                "Reconciliation enabled but cannot fetch labels for #%d",
                issue_number
            )
            return True, "Cannot fetch current labels", set()

        # Check 1: Labels we plan to remove should exist
        missing_to_remove = set(remove_labels) - current
        if missing_to_remove:
            msg = f"Labels to remove not present: {missing_to_remove}"
            logger.warning("Reconciliation: %s for issue #%d", msg, issue_number)
            # This is a warning, not a hard failure - label may have been
            # removed externally which is fine
            self.events.publish(TraceEvent(
                name="reconciliation.warning",
                data={
                    "issue_number": issue_number,
                    "message": msg,
                    "missing_labels": list(missing_to_remove),
                },
            ))

        # Check 2: Labels we expect to be there for this transition
        # For now, we just log what we found vs expected
        self.events.publish(TraceEvent(
            name="reconciliation.checked",
            data={
                "issue_number": issue_number,
                "current_labels": list(current),
                "add_labels": list(add_labels),
                "remove_labels": list(remove_labels),
            },
        ))

        return True, "", current

    def _apply_sync_labels(self, action: Action) -> ActionResult:
        """Synchronize labels on an issue.

        If reconciliation is enabled:
        1. Fetches current labels before mutations
        2. Logs any unexpected state (e.g., labels to remove not present)
        3. Emits reconciliation events for traceability
        """
        assert isinstance(action, SyncLabelsAction)

        # Reconciliation check
        should_proceed, msg, current_labels = self._check_reconciliation_for_sync(
            action.issue_number,
            action.add_labels,
            action.remove_labels,
        )
        if not should_proceed:
            return ActionResult.fail(action, f"Reconciliation failed: {msg}")

        errors = []

        # Add labels
        for label in action.add_labels:
            try:
                self.labels.add_label(action.issue_number, label)
            except Exception as e:
                errors.append(f"add {label}: {e}")

        # Remove labels
        for label in action.remove_labels:
            try:
                self.labels.remove_label(action.issue_number, label)
            except Exception as e:
                errors.append(f"remove {label}: {e}")

        if errors:
            return ActionResult.fail(action, "; ".join(errors))

        return ActionResult.ok(
            action,
            issue_number=action.issue_number,
            added=list(action.add_labels),
            removed=list(action.remove_labels),
        )

    def _apply_launch_session(self, action: Action) -> ActionResult:
        """Launch a terminal session.

        Uses the injected session_launcher callback to handle entity lookup
        and actual session launching. This keeps ActionApplier unaware of
        Issue/PendingReview/PendingRework entity types.
        """
        assert isinstance(action, LaunchSessionAction)

        # Use the callback if provided (preferred path - handles entity lookup)
        if self.session_launcher is not None:
            session = self.session_launcher(action.session_type, action.number)
            if session:
                return ActionResult.ok(
                    action,
                    session_name=session.tmux_session_name,
                    issue_number=session.issue.number,
                )
            else:
                return ActionResult.fail(
                    action,
                    f"Failed to launch {action.session_type} session for #{action.number}"
                )

        # Fallback: use command/working_dir from action (for testing or direct calls)
        if not action.command or not action.working_dir:
            return ActionResult.fail(
                action,
                "No session_launcher callback and action missing command/working_dir"
            )

        # Map session type string to enum
        session_type_map = {
            "issue": SessionType.ISSUE,
            "review": SessionType.REVIEW,
            "rework": SessionType.REWORK,
            "triage": SessionType.TRIAGE,
        }

        session_type = session_type_map.get(action.session_type)
        if session_type is None:
            return ActionResult.fail(
                action, f"Unknown session type: {action.session_type}"
            )

        ref = SessionRef(session_type=session_type, number=action.number)

        # Check if already running
        if self.sessions.exists(ref):
            return ActionResult.skip(action, f"Session {ref.name} already running")

        ctx = SessionContext(
            ref=ref,
            command=action.command,
            working_dir=Path(action.working_dir),
            title=action.title,
        )

        success = self.sessions.start(ctx)

        if success:
            return ActionResult.ok(action, session_name=ref.name)
        else:
            return ActionResult.fail(action, "Failed to start session")

    def _apply_stop_session(self, action: Action) -> ActionResult:
        """Stop a terminal session."""
        assert isinstance(action, StopSessionAction)

        session_type_map = {
            "issue": SessionType.ISSUE,
            "review": SessionType.REVIEW,
            "rework": SessionType.REWORK,
            "triage": SessionType.TRIAGE,
        }

        session_type = session_type_map.get(action.session_type)
        if session_type is None:
            return ActionResult.fail(
                action, f"Unknown session type: {action.session_type}"
            )

        ref = SessionRef(session_type=session_type, number=action.number)

        # Check if running
        if not self.sessions.exists(ref):
            return ActionResult.skip(action, f"Session {ref.name} not running")

        self.sessions.stop(ref)
        return ActionResult.ok(action, session_name=ref.name)

    def _apply_queue_operation(self, action: Action) -> ActionResult:
        """Queue operations are handled by orchestrator state.

        The applier just signals success - actual queuing is done by the caller.
        """
        return ActionResult.ok(action, note="Queue operation delegated to orchestrator")

    def _apply_escalate(self, action: Action) -> ActionResult:
        """Escalate to human intervention.

        The full escalation flow:
        1. Add needs-human label to the PR
        2. Remove needs-rework label from the PR
        3. Post an explanatory comment
        4. Emit trace event
        """
        assert isinstance(action, EscalateToHumanAction)

        errors = []

        # Add needs-human label
        try:
            self.labels.add_label(action.pr_number, action.needs_human_label)
        except Exception as e:
            errors.append(f"add label: {e}")

        # Remove needs-rework label
        try:
            self.labels.remove_label(action.pr_number, action.needs_rework_label)
        except Exception as e:
            # Not a hard failure - label may already be removed
            logger.debug("Failed to remove needs-rework label: %s", e)

        # Post explanatory comment
        if self.repository_host:
            comment = f"""## ⚠️ Escalated to Human Review

This PR has gone through {action.rework_cycles - 1} rework cycles without passing review.
Maximum rework cycles ({action.max_rework_cycles}) exceeded.

**A human needs to review and either:**
- Approve the PR manually
- Provide specific guidance for the agent
- Take over the implementation
"""
            try:
                self.repository_host.add_comment(action.pr_number, comment)
            except Exception as e:
                errors.append(f"add comment: {e}")

        print(f"⚠️  PR #{action.pr_number} escalated to {action.needs_human_label} after {action.rework_cycles} rework cycles")

        # Emit trace event
        self.events.publish(
            TraceEvent(
                name="review.escalated",
                data={
                    "pr_number": action.pr_number,
                    "issue_number": action.issue_number,
                    "rework_count": action.rework_cycles - 1,
                    "max_rework_cycles": action.max_rework_cycles,
                },
            )
        )

        if errors:
            return ActionResult.fail(action, "; ".join(errors))

        return ActionResult.ok(
            action,
            issue_number=action.issue_number,
            pr_number=action.pr_number,
            escalation_reason=action.escalation_reason,
        )

    def _emit_action_start(self, action: Action) -> None:
        """Emit a trace event when starting an action."""
        self.events.publish(
            TraceEvent(
                name="action.start",
                data={
                    "action_type": action.action_type.value,
                    "reason": action.reason,
                },
            )
        )

    def _emit_action_end(self, action: Action, result: ActionResult) -> None:
        """Emit a trace event when completing an action."""
        self.events.publish(
            TraceEvent(
                name="action.end",
                data={
                    "action_type": action.action_type.value,
                    "result": result.result_type.value,
                    "error": result.error,
                },
            )
        )

    def _apply_queue_review(self, action: Action) -> ActionResult:
        """Queue a PR for code review.

        Handles the IO part (adding review label). State update is handled
        by the orchestrator after this returns.
        """
        assert isinstance(action, QueueReviewAction)

        # Add review label if available
        if self.labels and action.code_review_label and action.pr_number:
            try:
                self.labels.add_label(action.pr_number, action.code_review_label)
                logger.info("Added '%s' label to PR #%d", action.code_review_label, action.pr_number)
            except Exception as e:
                logger.warning("Failed to add review label to PR #%d: %s", action.pr_number, e)

        self.events.publish(TraceEvent("review.queued", {
            "pr_number": action.pr_number,
            "issue_number": action.issue_number,
            "pr_url": action.pr_url,
        }))

        return ActionResult.ok(
            action,
            pr_number=action.pr_number,
            issue_number=action.issue_number,
        )

    def _apply_create_triage_issue(self, action: Action) -> ActionResult:
        """Create a triage review issue.

        Creates the GitHub issue via repository_host.
        """
        assert isinstance(action, CreateTriageIssueAction)

        if not self.repository_host:
            return ActionResult.fail(
                action, "No repository_host configured for issue creation"
            )

        try:
            issue_number = self.repository_host.create_issue(
                title=action.title,
                body=action.body,
                labels=list(action.labels),
            )

            if issue_number:
                logger.info(
                    "[APPLIER] Created triage issue #%d for %d PRs",
                    issue_number, action.pr_count
                )
                self.events.publish(TraceEvent("triage.issue_created", {
                    "issue_number": issue_number,
                    "pr_count": action.pr_count,
                }))
                return ActionResult.ok(
                    action,
                    issue_number=issue_number,
                    pr_count=action.pr_count,
                )
            else:
                return ActionResult.fail(action, "Issue creation returned None")

        except Exception as e:
            logger.exception("Failed to create triage issue")
            return ActionResult.fail(action, str(e))

    def _apply_cleanup_session(self, action: Action) -> ActionResult:
        """Clean up a completed session.

        Closes terminal tab and removes worktree as configured.
        """
        assert isinstance(action, CleanupSessionAction)

        errors = []

        # Close terminal session if configured
        if action.close_tabs and action.terminal_session_name:
            try:
                # Use sessions manager to stop the session
                # First determine session type from name
                session_type = SessionType.ISSUE
                if action.terminal_session_name.startswith("review-"):
                    session_type = SessionType.REVIEW
                elif action.terminal_session_name.startswith("rework-"):
                    session_type = SessionType.REWORK
                elif action.terminal_session_name.startswith("triage-"):
                    session_type = SessionType.TRIAGE

                ref = SessionRef(session_type=session_type, number=action.issue_number)
                if self.sessions.exists(ref):
                    self.sessions.stop(ref)
                    logger.info(
                        "[APPLIER] Closed terminal session for #%d",
                        action.issue_number
                    )
            except Exception as e:
                errors.append(f"close session: {e}")
                logger.warning(
                    "[APPLIER] Failed to close session for #%d: %s",
                    action.issue_number, e
                )

        # Remove worktree if configured
        if action.remove_worktrees and action.worktree_path:
            if self.worktree_manager:
                try:
                    self.worktree_manager.remove(Path(action.worktree_path))
                    logger.info(
                        "[APPLIER] Removed worktree for #%d",
                        action.issue_number
                    )
                except Exception as e:
                    errors.append(f"remove worktree: {e}")
                    logger.warning(
                        "[APPLIER] Failed to remove worktree for #%d: %s",
                        action.issue_number, e
                    )
            else:
                errors.append("no worktree_manager configured")

        self.events.publish(TraceEvent("cleanup.completed", {
            "issue_number": action.issue_number,
            "pr_number": action.pr_number,
        }))

        if errors:
            return ActionResult.fail(action, "; ".join(errors))

        return ActionResult.ok(
            action,
            issue_number=action.issue_number,
            pr_number=action.pr_number,
        )

    def _apply_remove_worktree(self, action: Action) -> ActionResult:
        """Remove a git worktree."""
        assert isinstance(action, RemoveWorktreeAction)

        if not self.worktree_manager:
            return ActionResult.fail(
                action, "No worktree_manager configured"
            )

        try:
            self.worktree_manager.remove(Path(action.worktree_path))
            return ActionResult.ok(action, worktree_path=action.worktree_path)
        except Exception as e:
            return ActionResult.fail(action, str(e))
