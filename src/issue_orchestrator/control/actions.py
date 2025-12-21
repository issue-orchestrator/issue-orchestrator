"""Action dataclasses - the Plan/Apply boundary.

Actions are the output of planning logic and the input to the applier.
They describe WHAT should happen, not HOW.

This separation enables:
- Planning code to be tested without IO (pure logic)
- Applier to be tested with fake ports
- Clear audit trail of decisions

Usage:
    # In workflow/planner
    actions = [
        AddLabelAction(issue_number=123, label="in-progress"),
        LaunchSessionAction(session_type="issue", number=123, ...),
    ]

    # In applier
    for action in actions:
        result = applier.apply(action)
"""

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Optional


class ActionType(Enum):
    """Types of actions the orchestrator can take."""

    # Label operations
    ADD_LABEL = "add_label"
    REMOVE_LABEL = "remove_label"
    SYNC_LABELS = "sync_labels"

    # Session operations
    LAUNCH_SESSION = "launch_session"
    STOP_SESSION = "stop_session"

    # GitHub operations
    CREATE_PR = "create_pr"
    ADD_COMMENT = "add_comment"
    CLOSE_ISSUE = "close_issue"

    # State transitions
    TRANSITION = "transition"

    # Worktree operations
    CREATE_WORKTREE = "create_worktree"
    REMOVE_WORKTREE = "remove_worktree"

    # Queue operations
    QUEUE_REVIEW = "queue_review"
    QUEUE_REWORK = "queue_rework"
    QUEUE_TRIAGE = "queue_triage"

    # Escalation
    ESCALATE_TO_HUMAN = "escalate_to_human"


@dataclass(frozen=True)
class Action:
    """Base action class.

    All actions are immutable data objects that describe an intended change.
    The actual execution is handled by the ActionApplier.
    """

    action_type: ActionType
    reason: str = ""  # Why this action is being taken (for audit)

    def __post_init__(self):
        # Validate that subclasses set the correct action_type
        pass


@dataclass(frozen=True)
class AddLabelAction(Action):
    """Add a label to an issue."""

    issue_number: int = 0
    label: str = ""
    action_type: ActionType = field(default=ActionType.ADD_LABEL, init=False)


@dataclass(frozen=True)
class RemoveLabelAction(Action):
    """Remove a label from an issue."""

    issue_number: int = 0
    label: str = ""
    action_type: ActionType = field(default=ActionType.REMOVE_LABEL, init=False)


@dataclass(frozen=True)
class SyncLabelsAction(Action):
    """Synchronize labels on an issue to match desired state."""

    issue_number: int = 0
    add_labels: tuple[str, ...] = field(default_factory=tuple)
    remove_labels: tuple[str, ...] = field(default_factory=tuple)
    action_type: ActionType = field(default=ActionType.SYNC_LABELS, init=False)


@dataclass(frozen=True)
class LaunchSessionAction(Action):
    """Launch a terminal session for an agent."""

    session_type: str = ""  # "issue", "review", "rework", "triage"
    number: int = 0  # Issue or PR number
    command: str = ""
    working_dir: str = ""
    title: Optional[str] = None
    action_type: ActionType = field(default=ActionType.LAUNCH_SESSION, init=False)


@dataclass(frozen=True)
class StopSessionAction(Action):
    """Stop a terminal session."""

    session_type: str = ""
    number: int = 0
    action_type: ActionType = field(default=ActionType.STOP_SESSION, init=False)


@dataclass(frozen=True)
class TransitionAction(Action):
    """Trigger a state machine transition."""

    machine_type: str = ""  # "issue", "session", "review"
    entity_id: int | str = 0
    trigger: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    action_type: ActionType = field(default=ActionType.TRANSITION, init=False)


@dataclass(frozen=True)
class CreateWorktreeAction(Action):
    """Create a git worktree for an issue."""

    issue_number: int = 0
    branch_name: str = ""
    worktree_path: str = ""
    action_type: ActionType = field(default=ActionType.CREATE_WORKTREE, init=False)


@dataclass(frozen=True)
class RemoveWorktreeAction(Action):
    """Remove a git worktree."""

    worktree_path: str = ""
    action_type: ActionType = field(default=ActionType.REMOVE_WORKTREE, init=False)


@dataclass(frozen=True)
class QueueReviewAction(Action):
    """Queue a PR for code review."""

    issue_number: int = 0
    pr_number: int = 0
    pr_url: str = ""
    branch_name: str = ""
    action_type: ActionType = field(default=ActionType.QUEUE_REVIEW, init=False)


@dataclass(frozen=True)
class QueueReworkAction(Action):
    """Queue an issue for rework."""

    issue_number: int = 0
    pr_number: int = 0
    pr_url: str = ""
    branch_name: str = ""
    rework_cycle: int = 1
    action_type: ActionType = field(default=ActionType.QUEUE_REWORK, init=False)


@dataclass(frozen=True)
class QueueTriageAction(Action):
    """Queue an issue for triage review."""

    issue_number: int = 0
    title: str = ""
    action_type: ActionType = field(default=ActionType.QUEUE_TRIAGE, init=False)


@dataclass(frozen=True)
class EscalateToHumanAction(Action):
    """Escalate an issue to human intervention."""

    issue_number: int = 0
    pr_number: int = 0
    escalation_reason: str = ""
    rework_cycles: int = 0
    action_type: ActionType = field(default=ActionType.ESCALATE_TO_HUMAN, init=False)


@dataclass(frozen=True)
class AddCommentAction(Action):
    """Add a comment to an issue or PR."""

    number: int = 0  # Issue or PR number
    comment: str = ""
    is_pr: bool = False
    action_type: ActionType = field(default=ActionType.ADD_COMMENT, init=False)


# Action result types


class ActionResultType(Enum):
    """Result of applying an action."""

    SUCCESS = "success"
    FAILURE = "failure"
    SKIPPED = "skipped"  # Already applied or not applicable


@dataclass(frozen=True)
class ActionResult:
    """Result of applying an action.

    Attributes:
        action: The action that was applied
        result_type: Success, failure, or skipped
        error: Error message if failed
        details: Additional details about the result
    """

    action: Action
    result_type: ActionResultType
    error: Optional[str] = None
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def success(self) -> bool:
        """Check if the action succeeded."""
        return self.result_type == ActionResultType.SUCCESS

    @classmethod
    def ok(cls, action: Action, **details) -> "ActionResult":
        """Create a successful result."""
        return cls(
            action=action,
            result_type=ActionResultType.SUCCESS,
            details=details,
        )

    @classmethod
    def fail(cls, action: Action, error: str, **details) -> "ActionResult":
        """Create a failed result."""
        return cls(
            action=action,
            result_type=ActionResultType.FAILURE,
            error=error,
            details=details,
        )

    @classmethod
    def skip(cls, action: Action, reason: str) -> "ActionResult":
        """Create a skipped result."""
        return cls(
            action=action,
            result_type=ActionResultType.SKIPPED,
            details={"skip_reason": reason},
        )
