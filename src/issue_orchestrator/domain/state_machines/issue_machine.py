"""State machine for issue lifecycle management.

This module implements the state machine for tracking an issue through its entire
lifecycle, from initial availability through completion.

The state machine is pure - it returns TransitionResult instead of publishing
events directly. The caller (control layer) is responsible for emitting
TraceEvents via EventSink.
"""

import logging
from enum import Enum
from typing import TYPE_CHECKING, Any, Optional

from transitions import MachineError, EventData, Machine

from .transition_result import TransitionResult
from .errors import InvalidStateTransition

if TYPE_CHECKING:
    from ...ports import Issue

logger = logging.getLogger(__name__)


class IssueState(Enum):
    """States an issue can be in during its lifecycle."""

    AVAILABLE = "available"
    CLAIMED = "claimed"
    IN_PROGRESS = "in_progress"
    BLOCKED = "blocked"
    NEEDS_HUMAN = "needs_human"
    PR_PENDING = "pr_pending"
    COMPLETED = "completed"


# NOTE:
# This class is the mutation target for `transitions.Machine`.
# It must remain a simple data object with no logic or recursion.
class _Model:
    """Internal transitions model.

    transitions injects trigger methods onto this object. The outer IssueStateMachine
    wrapper exposes a typed/stable API and prevents dynamic methods from leaking.
    """

    def __init__(self, initial_state: str) -> None:
        self.state = initial_state


class IssueStateMachine:
    """State machine for managing issue lifecycle.

    This state machine tracks an issue through the following states:
    - AVAILABLE: Issue is available for claiming
    - CLAIMED: Issue has been claimed by an agent
    - IN_PROGRESS: Active work session in progress
    - BLOCKED: Work is blocked waiting for resolution
    - NEEDS_HUMAN: Human intervention required
    - PR_PENDING: Pull request has been created and awaiting merge
    - COMPLETED: Issue is complete (PR merged)

    The state machine is pure - transitions store their result in last_transition
    which callers use to emit appropriate TraceEvents via EventSink.

    Attributes:
        issue_number: The GitHub issue number this state machine tracks
        state: Current state of the issue
        last_transition: Result of the most recent transition (for event emission)
    """

    def __init__(self, issue: "Issue", initial_state: IssueState = IssueState.AVAILABLE):
        """Initialize the issue state machine.

        Args:
            issue: The Issue object (provides identity via .key)
            initial_state: Starting state (defaults to AVAILABLE)
        """
        self.issue = issue
        self._model = _Model(initial_state.value)
        self.state = self._model.state
        self.last_transition: Optional[TransitionResult] = None

        # Define all possible states
        states = [state.value for state in IssueState]

        # Define state transitions
        transitions = [
            # Claim an available issue
            {
                'trigger': 'claim',
                'source': IssueState.AVAILABLE.value,
                'dest': IssueState.CLAIMED.value,
                'after': self._on_claimed
            },
            # Start work on a claimed issue
            {
                'trigger': 'start',
                'source': IssueState.CLAIMED.value,
                'dest': IssueState.IN_PROGRESS.value,
                'after': self._on_started
            },
            # Block an in-progress issue
            {
                'trigger': 'block',
                'source': IssueState.IN_PROGRESS.value,
                'dest': IssueState.BLOCKED.value,
                'after': self._on_blocked
            },
            # Mark issue as needing human intervention
            {
                'trigger': 'needs_human',
                'source': IssueState.IN_PROGRESS.value,
                'dest': IssueState.NEEDS_HUMAN.value,
                'after': self._on_needs_human
            },
            # Unblock and return to in-progress
            {
                'trigger': 'unblock',
                'source': [IssueState.BLOCKED.value, IssueState.NEEDS_HUMAN.value],
                'dest': IssueState.IN_PROGRESS.value,
                'after': self._on_unblocked
            },
            # Create PR from in-progress work
            {
                'trigger': 'pr_created',
                'source': IssueState.IN_PROGRESS.value,
                'dest': IssueState.PR_PENDING.value,
                'after': self._on_pr_created
            },
            # PR merged - issue complete
            {
                'trigger': 'pr_merged',
                'source': IssueState.PR_PENDING.value,
                'dest': IssueState.COMPLETED.value,
                'after': self._on_completed
            },
            # PR closed/rejected - return to in-progress
            {
                'trigger': 'pr_closed',
                'source': IssueState.PR_PENDING.value,
                'dest': IssueState.IN_PROGRESS.value,
                'after': self._on_pr_rejected
            },
            # Release issue back to available from various states
            {
                'trigger': 'release',
                'source': [
                    IssueState.CLAIMED.value,
                    IssueState.IN_PROGRESS.value,
                    IssueState.BLOCKED.value,
                    IssueState.NEEDS_HUMAN.value
                ],
                'dest': IssueState.AVAILABLE.value,
                'after': self._on_released
            }
        ]

        # Create the state machine with internal model
        self.machine = Machine(
            model=self._model,
            states=states,
            transitions=transitions,
            initial=initial_state.value,
            send_event=True,
            auto_transitions=False
        )

        logger.info(f"IssueStateMachine initialized for issue {self.issue.number} in state {initial_state.value}")

    @property
    def issue_number(self) -> int:
        """Backwards-compatible access to issue number."""
        return self.issue.number

    def _on_claimed(self, event: EventData) -> None:
        """Callback for claim transition."""
        data = event.kwargs.get('data', {})
        self.last_transition = TransitionResult(
            success=True,
            from_state=IssueState.AVAILABLE.value,
            to_state=IssueState.CLAIMED.value,
            event_name="issue.claimed",
            entity_id=self.issue_number,
            data=data,
        )
        logger.info(f"Issue {self.issue_number} claimed")

    def _on_started(self, event: EventData) -> None:
        """Callback for start transition."""
        data = event.kwargs.get('data', {})
        self.last_transition = TransitionResult(
            success=True,
            from_state=IssueState.CLAIMED.value,
            to_state=IssueState.IN_PROGRESS.value,
            event_name="issue.started",
            entity_id=self.issue_number,
            data=data,
        )
        logger.info(f"Issue {self.issue_number} work started")

    def _on_blocked(self, event: EventData) -> None:
        """Callback for block transition."""
        data = event.kwargs.get('data', {})
        self.last_transition = TransitionResult(
            success=True,
            from_state=IssueState.IN_PROGRESS.value,
            to_state=IssueState.BLOCKED.value,
            event_name="issue.blocked",
            entity_id=self.issue_number,
            data=data,
        )
        logger.warning(f"Issue {self.issue_number} blocked")

    def _on_needs_human(self, event: EventData) -> None:
        """Callback for needs_human transition."""
        data = event.kwargs.get('data', {})
        self.last_transition = TransitionResult(
            success=True,
            from_state=IssueState.IN_PROGRESS.value,
            to_state=IssueState.NEEDS_HUMAN.value,
            event_name="issue.needs_human",
            entity_id=self.issue_number,
            data=data,
        )
        logger.warning(f"Issue {self.issue_number} needs human intervention")

    def _on_unblocked(self, event: EventData) -> None:
        """Callback for unblock transition."""
        data = event.kwargs.get('data', {})
        from_state = event.transition.source if hasattr(event, 'transition') and event.transition else IssueState.BLOCKED.value
        self.last_transition = TransitionResult(
            success=True,
            from_state=from_state,
            to_state=IssueState.IN_PROGRESS.value,
            event_name="issue.unblocked",
            entity_id=self.issue_number,
            data=data,
        )
        logger.info(f"Issue {self.issue_number} unblocked")

    def _on_pr_created(self, event: EventData) -> None:
        """Callback for pr_created transition."""
        data = event.kwargs.get('data', {})
        self.last_transition = TransitionResult(
            success=True,
            from_state=IssueState.IN_PROGRESS.value,
            to_state=IssueState.PR_PENDING.value,
            event_name="issue.pr_created",
            entity_id=self.issue_number,
            data=data,
        )
        logger.info(f"PR created for issue {self.issue_number}")

    def _on_pr_rejected(self, event: EventData) -> None:
        """Callback for pr_closed transition."""
        data = event.kwargs.get('data', {})
        self.last_transition = TransitionResult(
            success=True,
            from_state=IssueState.PR_PENDING.value,
            to_state=IssueState.IN_PROGRESS.value,
            event_name="issue.pr_rejected",
            entity_id=self.issue_number,
            data=data,
        )
        logger.info(f"PR rejected for issue {self.issue_number}")

    def _on_completed(self, event: EventData) -> None:
        """Callback for pr_merged transition."""
        data = event.kwargs.get('data', {})
        self.last_transition = TransitionResult(
            success=True,
            from_state=IssueState.PR_PENDING.value,
            to_state=IssueState.COMPLETED.value,
            event_name="issue.completed",
            entity_id=self.issue_number,
            data=data,
        )
        logger.info(f"Issue {self.issue_number} completed")

    def _on_released(self, event: EventData) -> None:
        """Callback for release transition."""
        data = event.kwargs.get('data', {})
        from_state = event.transition.source if hasattr(event, 'transition') and event.transition else IssueState.IN_PROGRESS.value
        self.last_transition = TransitionResult(
            success=True,
            from_state=from_state,
            to_state=IssueState.AVAILABLE.value,
            event_name="issue.released",
            entity_id=self.issue_number,
            data=data,
        )
        logger.info(f"Issue {self.issue_number} released back to available")

    def get_state(self) -> IssueState:
        """Get the current state as an enum."""
        return IssueState(self.state)

    def can_transition(self, trigger: str) -> bool:
        """Check if a transition is valid from the current state."""
        trigger_func = getattr(self._model, f'may_{trigger}', None)
        if trigger_func and callable(trigger_func):
            return bool(trigger_func())
        return False

    # -------------------------------------------------------------------------
    # Typed transition methods (quarantine transitions' dynamic surface)
    # -------------------------------------------------------------------------

    def _invoke(self, name: str, **kwargs: Any) -> None:
        """Invoke a transitions-injected trigger on the internal model.

        This quarantines transitions' dynamic surface area inside the wrapper.
        """
        try:
            fn = getattr(self._model, name)
            fn(**kwargs)
        except MachineError as e:
            raise InvalidStateTransition(str(e)) from e
        finally:
            # Keep backward-compatible `state` attribute in sync
            self.state = self._model.state

    def claim(self, **kwargs: Any) -> None:
        """Claim an available issue."""
        self._invoke('claim', **kwargs)

    def start(self, **kwargs: Any) -> None:
        """Start work on a claimed issue."""
        self._invoke('start', **kwargs)

    def block(self, **kwargs: Any) -> None:
        """Block an in-progress issue."""
        self._invoke('block', **kwargs)

    def needs_human(self, **kwargs: Any) -> None:
        """Mark issue as needing human intervention."""
        self._invoke('needs_human', **kwargs)

    def unblock(self, **kwargs: Any) -> None:
        """Unblock and return to in-progress."""
        self._invoke('unblock', **kwargs)

    def pr_created(self, **kwargs: Any) -> None:
        """Mark PR as created."""
        self._invoke('pr_created', **kwargs)

    def pr_merged(self, **kwargs: Any) -> None:
        """Mark PR as merged."""
        self._invoke('pr_merged', **kwargs)

    def pr_closed(self, **kwargs: Any) -> None:
        """Mark PR as closed/rejected."""
        self._invoke('pr_closed', **kwargs)

    def release(self, **kwargs: Any) -> None:
        """Release issue back to available."""
        self._invoke('release', **kwargs)
