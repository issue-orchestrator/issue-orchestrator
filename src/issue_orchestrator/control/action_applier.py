"""ActionApplier - executes actions via ports/adapters.

This is the IO boundary for the orchestrator. It:
1. Takes Action objects (the plan)
2. Executes them via injected ports
3. Emits trace events for each action
4. Returns ActionResults

When reconciliation is enabled (reconcile=True with fresh_issue_reader provided):
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
        fresh_issue_reader=github_fresh_reader,  # Optional, for reconciliation
        reconcile=True,  # Enable reconciliation
    )
    results = applier.apply_all(actions)
"""

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Sequence

from ..events import EventName
from ..infra.logging_config import issue_log
from ..ports import EventSink, TraceEvent
from ..ports.label_set import LabelSet
from ..ports.fresh_issue_reader import FreshIssueReader
from ..ports.repository_host import RepositoryHost
from ..ports.worktree_manager import WorktreeManager
from ..domain.models import Session
from .reconciliation import (
    ExternalSnapshot,
    ReconciliationRequired,
    require_reconciliation,
)
from .claim_gate import ClaimGate, ClaimLostError
from .actions import (
    Action,
    ActionResult,
    ActionType,
    AddLabelAction,
    RemoveLabelAction,
    SyncLabelsAction,
    LaunchSessionAction,
    StopSessionAction,
    QueueReviewAction,
    EscalateToHumanAction,
    AddCommentAction,
    CreateTriageIssueAction,
    CleanupSessionAction,
    RemoveWorktreeAction,
)
from .session_manager import SessionManager, SessionRef, SessionType, SessionContext

logger = logging.getLogger(__name__)

# Type alias for session launcher callback
# Takes (session_type, number) and returns Optional[Session]
# This allows orchestrator to inject entity lookup + SessionLauncher
SessionLauncherCallback = Callable[[SessionType, int], Optional[Session]]

# Type alias for lease_id lookup callback
# Takes issue_number and returns lease_id if active session exists
LeaseIdLookup = Callable[[int], str | None]


@dataclass
class ActionApplier:
    """Applies actions via ports/adapters.

    This is the IO boundary - all external calls go through here.
    Each action type has a handler that knows how to execute it.

    When reconciliation is enabled (reconcile=True):
    - Before label mutations, fetches current labels from fresh_issue_reader
    - Verifies state hasn't changed unexpectedly
    - Emits reconciliation events for traceability
    """

    labels: LabelSet
    sessions: SessionManager
    events: EventSink
    repository_host: Optional[RepositoryHost] = None  # For issue creation, labels
    worktree_manager: Optional[WorktreeManager] = None  # For worktree operations
    fresh_issue_reader: Optional[FreshIssueReader] = None
    reconcile: bool = False  # If True, verify state before mutations
    # Session launcher callback - handles entity lookup + launching
    # Injected by orchestrator, allows ActionApplier to launch sessions without
    # knowing about Issue/PendingReview/PendingRework entities
    session_launcher: Optional[SessionLauncherCallback] = None
    # Claim/lease verification for multi-orchestrator coordination
    claim_gate: Optional[ClaimGate] = None
    # Callback to look up lease_id for an issue from active sessions
    lease_id_lookup: Optional[LeaseIdLookup] = None

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
        except ReconciliationRequired:
            # Re-raise ReconciliationRequired - it must propagate to orchestrator
            raise
        except ClaimLostError:
            # Re-raise ClaimLostError - it must propagate to orchestrator
            raise
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

        # Enforce expected state before mutation (raises ReconciliationRequired)
        self._require_expected(action, action.issue_number)
        # Verify claim ownership before write (raises ClaimLostError)
        self._verify_claim_before_write(action, action.issue_number)

        try:
            self.labels.add_label(action.issue_number, action.label)
            logger.info(issue_log(action.issue_number, "Label added: %s"), action.label)
            self._emit_issue_labels_changed(action.issue_number, [action.label], [])
            return ActionResult.ok(
                action,
                issue_number=action.issue_number,
                label=action.label,
            )
        except Exception as e:
            logger.error(issue_log(action.issue_number, "Failed to add label %s: %s"), action.label, e)
            return ActionResult.fail(action, str(e))

    def _apply_remove_label(self, action: Action) -> ActionResult:
        """Remove a label from an issue."""
        assert isinstance(action, RemoveLabelAction)

        # Enforce expected state before mutation (raises ReconciliationRequired)
        self._require_expected(action, action.issue_number)
        # Verify claim ownership before write (raises ClaimLostError)
        self._verify_claim_before_write(action, action.issue_number)

        try:
            self.labels.remove_label(action.issue_number, action.label)
            logger.info(issue_log(action.issue_number, "Label removed: %s"), action.label)
            self._emit_issue_labels_changed(action.issue_number, [], [action.label])
            return ActionResult.ok(
                action,
                issue_number=action.issue_number,
                label=action.label,
            )
        except Exception as e:
            logger.error(issue_log(action.issue_number, "Failed to remove label %s: %s"), action.label, e)
            return ActionResult.fail(action, str(e))

    def _apply_add_comment(self, action: Action) -> ActionResult:
        """Add a comment to an issue or PR."""
        assert isinstance(action, AddCommentAction)
        assert self.repository_host is not None, "repository_host required for add_comment"

        # Enforce expected state before mutation (raises ReconciliationRequired)
        self._require_expected(action, action.number)
        # Verify claim ownership before write (raises ClaimLostError)
        self._verify_claim_before_write(action, action.number)

        try:
            self.repository_host.add_comment(action.number, action.comment)
            logger.info(issue_log(action.number, "Comment added (%d chars)"), len(action.comment))
            return ActionResult.ok(
                action,
                number=action.number,
                is_pr=action.is_pr,
            )
        except Exception as e:
            logger.error(issue_log(action.number, "Failed to add comment: %s"), e)
            return ActionResult.fail(action, str(e))

    def _fetch_current_labels(self, issue_number: int) -> set[str] | None:
        """Fetch current labels for an issue if fresh_issue_reader is available.

        Returns:
            Set of label names, or None if fresh_issue_reader not configured
        """
        if self.fresh_issue_reader is None:
            return None
        try:
            labels = self.fresh_issue_reader.read_issue_labels(issue_number)
            return set(labels)
        except Exception as e:
            logger.warning(
                issue_log(issue_number, "Failed to fetch labels for reconciliation: %s"),
                e,
            )
            return None

    def _require_expected(self, action: Action, issue_number: int) -> None:
        """Enforce reconciliation before a mutation if action has expected state.

        This is the hard gate for optimistic concurrency control. If the action
        has an ExpectedState attached, we fetch current state and verify it
        satisfies the constraints. If not, we raise ReconciliationRequired.

        Args:
            action: The action being applied (checks action.expected)
            issue_number: The issue/PR number to check

        Raises:
            ReconciliationRequired: If current state doesn't satisfy expected
        """
        if action.expected is None:
            # No expected state attached - allow (for backwards compatibility)
            return

        if not self.reconcile:
            # Reconciliation disabled - skip enforcement
            return

        # Fetch current state
        current_labels = self._fetch_current_labels(issue_number)
        if current_labels is None:
            # Can't verify - fail closed (require fresh_issue_reader for reconciliation)
            logger.warning(
                issue_log(issue_number, "Reconciliation required but cannot fetch labels - failing closed"),
            )
            raise ReconciliationRequired(
                entity_type="issue",
                entity_id=issue_number,
                expected=ExternalSnapshot.for_issue(issue_number, set(action.expected.required_labels)),
                actual=ExternalSnapshot.for_issue(issue_number, set()),
                reason="Cannot fetch current labels to verify expected state",
            )

        actual = ExternalSnapshot.for_issue(issue_number, current_labels)

        # This raises ReconciliationRequired if constraints not satisfied
        require_reconciliation(action.expected, actual, entity_type="issue")

    def _verify_claim_before_write(self, action: Action, issue_number: int) -> None:
        """Verify claim ownership before a write operation.

        For multi-orchestrator coordination, this verifies the current orchestrator
        still owns the claim for this issue before making any external mutation.

        Args:
            action: The action being applied (for logging the operation type)
            issue_number: The issue number to verify claim for

        Raises:
            ClaimLostError: If the claim has been lost to another orchestrator
        """
        if not self.claim_gate:
            # No claim gate configured - skip verification
            return

        if not self.lease_id_lookup:
            # No lease_id lookup configured - skip verification
            return

        lease_id = self.lease_id_lookup(issue_number)
        if not lease_id:
            # No active session with lease for this issue - skip verification
            return

        # Verify claim ownership - raises ClaimLostError if lost
        self.claim_gate.verify_or_raise(
            issue_number=issue_number,
            lease_id=lease_id,
            operation=action.action_type.value,
        )

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
                issue_log(issue_number, "Reconciliation enabled but cannot fetch labels"),
            )
            return True, "Cannot fetch current labels", set()

        # Check 1: Labels we plan to remove should exist
        missing_to_remove = set(remove_labels) - current
        if missing_to_remove:
            msg = f"Labels to remove not present: {missing_to_remove}"
            logger.warning(issue_log(issue_number, "Reconciliation: %s"), msg)
            # This is a warning, not a hard failure - label may have been
            # removed externally which is fine
            self.events.publish(TraceEvent(
                EventName.RECONCILIATION_WARNING,
                {
                    "issue_number": issue_number,
                    "message": msg,
                    "missing_labels": list(missing_to_remove),
                },
            ))

        # Check 2: Labels we expect to be there for this transition
        # For now, we just log what we found vs expected
        self.events.publish(TraceEvent(
            EventName.RECONCILIATION_CHECKED,
            {
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
        1. Enforces expected state constraints (hard gate)
        2. Fetches current labels before mutations
        3. Logs any unexpected state (e.g., labels to remove not present)
        4. Emits reconciliation events for traceability
        """
        assert isinstance(action, SyncLabelsAction)

        # Enforce expected state before mutation (raises ReconciliationRequired)
        self._require_expected(action, action.issue_number)
        # Verify claim ownership before write (raises ClaimLostError)
        self._verify_claim_before_write(action, action.issue_number)

        # Soft reconciliation check (backwards compatibility - logs warnings)
        should_proceed, msg, _current_labels = self._check_reconciliation_for_sync(
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

        self._emit_issue_labels_changed(
            action.issue_number,
            list(action.add_labels),
            list(action.remove_labels),
        )
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
                    session_name=session.terminal_id,
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

        ref = SessionRef(session_type=action.session_type, number=action.number)

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

        ref = SessionRef(session_type=action.session_type, number=action.number)

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
        1. Enforce expected state (reconciliation)
        2. Add needs-human label to the PR
        3. Remove needs-rework label from the PR
        4. Post an explanatory comment
        5. Emit trace event
        """
        assert isinstance(action, EscalateToHumanAction)

        # Enforce expected state before mutation (raises ReconciliationRequired)
        self._require_expected(action, action.pr_number)
        # Verify claim ownership before write (raises ClaimLostError)
        # Claims are on issues, not PRs, so use issue_number
        self._verify_claim_before_write(action, action.issue_number)

        errors = []

        added_labels: list[str] = []
        removed_labels: list[str] = []

        # Add needs-human label
        try:
            self.labels.add_label(action.pr_number, action.needs_human_label)
            added_labels.append(action.needs_human_label)
        except Exception as e:
            errors.append(f"add label: {e}")

        # Remove needs-rework label
        try:
            self.labels.remove_label(action.pr_number, action.needs_rework_label)
            removed_labels.append(action.needs_rework_label)
        except Exception as e:
            # Not a hard failure - label may already be removed
            logger.debug("Failed to remove needs-rework label: %s", e)
        self._emit_pr_view_changed(
            pr_number=action.pr_number,
            issue_number=action.issue_number,
            added=added_labels,
            removed=removed_labels,
        )

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

        logger.warning(
            issue_log(action.issue_number, "PR #%d escalated to %s after %d rework cycles"),
            action.pr_number, action.needs_human_label, action.rework_cycles,
        )

        # Emit trace event
        self.events.publish(
            TraceEvent(
                EventName.REVIEW_ESCALATED,
                {
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
                EventName.ACTION_START,
                {
                    "action_type": action.action_type.value,
                    "reason": action.reason,
                },
            )
        )

    def _emit_action_end(self, action: Action, result: ActionResult) -> None:
        """Emit a trace event when completing an action."""
        self.events.publish(
            TraceEvent(
                EventName.ACTION_END,
                {
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

        # Enforce expected state before mutation (raises ReconciliationRequired)
        if action.pr_number:
            self._require_expected(action, action.pr_number)
        # Verify claim ownership before write (raises ClaimLostError)
        # Claims are on issues, not PRs, so use issue_number
        if action.issue_number:
            self._verify_claim_before_write(action, action.issue_number)

        # Add review label if available
        if self.labels and action.code_review_label and action.pr_number:
            try:
                self.labels.add_label(action.pr_number, action.code_review_label)
                logger.info(issue_log(action.issue_number, "Review label '%s' added to PR #%d"), action.code_review_label, action.pr_number)
                self._emit_pr_view_changed(
                    pr_number=action.pr_number,
                    issue_number=action.issue_number,
                    added=[action.code_review_label],
                    removed=[],
                )
            except Exception as e:
                logger.warning(issue_log(action.issue_number, "Failed to add review label to PR #%d: %s"), action.pr_number, e)

        self.events.publish(TraceEvent(EventName.REVIEW_QUEUED, {
            "pr_number": action.pr_number,
            "issue_number": action.issue_number,
            "pr_url": action.pr_url,
            "code_review_label": action.code_review_label,
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
            result = self.repository_host.create_issue(
                title=action.title,
                body=action.body,
                labels=list(action.labels),
                milestone=action.milestone,
            )

            issue_number = result.get("number") if result else None
            if issue_number:
                logger.info(
                    "[APPLIER] Created triage issue #%d for %d PRs (milestone=%s)",
                    issue_number, action.pr_count, action.milestone
                )
                self._emit_issue_labels_changed(
                    issue_number,
                    list(action.labels),
                    [],
                )
                self.events.publish(TraceEvent(EventName.TRIAGE_ISSUE_CREATED, {
                    "issue_number": issue_number,
                    "pr_count": action.pr_count,
                }))
                return ActionResult.ok(
                    action,
                    issue_number=issue_number,
                    pr_count=action.pr_count,
                )

            logger.warning(
                "[APPLIER] Triage issue creation returned None (title=%s labels=%s)",
                action.title,
                list(action.labels),
            )
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
                        issue_log(action.issue_number, "Closed terminal session"),
                    )
            except Exception as e:
                errors.append(f"close session: {e}")
                logger.warning(
                    issue_log(action.issue_number, "Failed to close session: %s"),
                    e,
                )

        # Remove worktree if configured
        if action.remove_worktrees and action.worktree_path:
            if self.worktree_manager:
                try:
                    self.worktree_manager.remove(Path(action.worktree_path))
                    logger.info(
                        issue_log(action.issue_number, "Removed worktree: %s"),
                        action.worktree_path,
                    )
                except Exception as e:
                    errors.append(f"remove worktree: {e}")
                    logger.warning(
                        issue_log(action.issue_number, "Failed to remove worktree: %s"),
                        e,
                    )
            else:
                errors.append("no worktree_manager configured")

        self.events.publish(TraceEvent(EventName.CLEANUP_COMPLETED, {
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

    def _emit_issue_labels_changed(
        self,
        issue_number: int,
        added: list[str],
        removed: list[str],
    ) -> None:
        if not added and not removed:
            return
        self.events.publish(TraceEvent(
            EventName.ISSUE_LABELS_CHANGED,
            {
                "issue_number": issue_number,
                "issue_key": str(issue_number),
                "added": added,
                "removed": removed,
            },
        ))

    def _emit_pr_view_changed(
        self,
        pr_number: int,
        issue_number: int | None,
        added: list[str],
        removed: list[str],
    ) -> None:
        if not added and not removed:
            return
        payload: dict[str, int | list[str] | str] = {
            "pr_number": pr_number,
            "added": added,
            "removed": removed,
        }
        if issue_number is not None:
            payload["issue_number"] = issue_number
            payload["issue_key"] = str(issue_number)
        self.events.publish(TraceEvent(EventName.PR_VIEW_CHANGED, payload))
