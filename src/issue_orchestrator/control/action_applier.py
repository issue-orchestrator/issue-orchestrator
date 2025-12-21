"""ActionApplier - executes actions via ports/adapters.

This is the IO boundary for the orchestrator. It:
1. Takes Action objects (the plan)
2. Executes them via injected ports
3. Emits trace events for each action
4. Returns ActionResults

Usage:
    applier = ActionApplier(
        labels=label_set,
        sessions=session_manager,
        events=event_sink,
    )
    results = applier.apply_all(actions)
"""

import logging
from dataclasses import dataclass
from typing import Callable, Sequence

from ..ports import EventSink, TraceEvent
from ..ports.label_set import LabelSet
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
)
from .session_manager import SessionManager, SessionRef, SessionType, SessionContext

logger = logging.getLogger(__name__)


@dataclass
class ActionApplier:
    """Applies actions via ports/adapters.

    This is the IO boundary - all external calls go through here.
    Each action type has a handler that knows how to execute it.
    """

    labels: LabelSet
    sessions: SessionManager
    events: EventSink

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
            # These are handled by the orchestrator's state directly
            ActionType.QUEUE_REVIEW: self._apply_queue_operation,
            ActionType.QUEUE_REWORK: self._apply_queue_operation,
            ActionType.QUEUE_TRIAGE: self._apply_queue_operation,
            ActionType.ESCALATE_TO_HUMAN: self._apply_escalate,
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

    def _apply_sync_labels(self, action: Action) -> ActionResult:
        """Synchronize labels on an issue."""
        assert isinstance(action, SyncLabelsAction)

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
        """Launch a terminal session."""
        assert isinstance(action, LaunchSessionAction)

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

        from pathlib import Path

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

        The applier adds the needs-human label and emits an event.
        """
        assert isinstance(action, EscalateToHumanAction)

        try:
            self.labels.add_label(action.issue_number, "blocked-needs-human")

            self.events.publish(
                TraceEvent(
                    name="issue.escalated",
                    data={
                        "issue_number": action.issue_number,
                        "pr_number": action.pr_number,
                        "reason": action.escalation_reason,
                        "rework_cycles": action.rework_cycles,
                    },
                )
            )

            return ActionResult.ok(
                action,
                issue_number=action.issue_number,
                escalation_reason=action.escalation_reason,
            )
        except Exception as e:
            return ActionResult.fail(action, str(e))

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
