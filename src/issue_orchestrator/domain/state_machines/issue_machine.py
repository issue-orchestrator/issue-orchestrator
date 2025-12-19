"""State machine for issue lifecycle management.

This module implements the state machine for tracking an issue through its entire
lifecycle, from initial availability through completion.
"""

import logging
from enum import Enum
from typing import Optional

from transitions import Machine

from ..events import EventBus, IssueEvent

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

    Each state transition emits an event via the EventBus to enable
    decoupled components to react to state changes.

    Attributes:
        issue_number: The GitHub issue number this state machine tracks
        state: Current state of the issue
        event_bus: EventBus for publishing state change events
    """

    def __init__(self, issue_number: int, event_bus: EventBus, initial_state: IssueState = IssueState.AVAILABLE):
        """Initialize the issue state machine.

        Args:
            issue_number: The GitHub issue number
            event_bus: EventBus instance for publishing events
            initial_state: Starting state (defaults to AVAILABLE)
        """
        self.issue_number = issue_number
        self.event_bus = event_bus
        self.state = initial_state

        # Define all possible states
        states = [state.value for state in IssueState]

        # Define state transitions
        transitions = [
            # Claim an available issue
            {
                'trigger': 'claim',
                'source': IssueState.AVAILABLE.value,
                'dest': IssueState.CLAIMED.value,
                'after': '_on_claimed'
            },
            # Start work on a claimed issue
            {
                'trigger': 'start',
                'source': IssueState.CLAIMED.value,
                'dest': IssueState.IN_PROGRESS.value,
                'after': '_on_started'
            },
            # Block an in-progress issue
            {
                'trigger': 'block',
                'source': IssueState.IN_PROGRESS.value,
                'dest': IssueState.BLOCKED.value,
                'after': '_on_blocked'
            },
            # Mark issue as needing human intervention
            {
                'trigger': 'needs_human',
                'source': IssueState.IN_PROGRESS.value,
                'dest': IssueState.NEEDS_HUMAN.value,
                'after': '_on_needs_human'
            },
            # Unblock and return to in-progress
            {
                'trigger': 'unblock',
                'source': [IssueState.BLOCKED.value, IssueState.NEEDS_HUMAN.value],
                'dest': IssueState.IN_PROGRESS.value,
                'after': '_on_unblocked'
            },
            # Create PR from in-progress work
            {
                'trigger': 'pr_created',
                'source': IssueState.IN_PROGRESS.value,
                'dest': IssueState.PR_PENDING.value,
                'after': '_on_pr_created'
            },
            # PR merged - issue complete
            {
                'trigger': 'pr_merged',
                'source': IssueState.PR_PENDING.value,
                'dest': IssueState.COMPLETED.value,
                'after': '_on_completed'
            },
            # PR closed/rejected - return to in-progress
            {
                'trigger': 'pr_closed',
                'source': IssueState.PR_PENDING.value,
                'dest': IssueState.IN_PROGRESS.value,
                'after': '_on_pr_rejected'
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
                'after': '_on_released'
            }
        ]

        # Create the state machine
        self.machine = Machine(
            model=self,
            states=states,
            transitions=transitions,
            initial=initial_state.value,
            send_event=True,
            auto_transitions=False
        )

        logger.info(f"IssueStateMachine initialized for issue {issue_number} in state {initial_state.value}")

    def _on_claimed(self, event):
        """Callback for claim transition.

        Args:
            event: Transition event data from transitions library
        """
        data = event.kwargs.get('data', {})
        self.event_bus.publish(
            IssueEvent.CLAIMED,
            entity_id=self.issue_number,
            data=data,
            source="IssueStateMachine"
        )
        logger.info(f"Issue {self.issue_number} claimed")

    def _on_started(self, event):
        """Callback for start transition.

        Args:
            event: Transition event data from transitions library
        """
        data = event.kwargs.get('data', {})
        self.event_bus.publish(
            IssueEvent.SESSION_STARTED,
            entity_id=self.issue_number,
            data=data,
            source="IssueStateMachine"
        )
        logger.info(f"Issue {self.issue_number} work started")

    def _on_blocked(self, event):
        """Callback for block transition.

        Args:
            event: Transition event data from transitions library
        """
        data = event.kwargs.get('data', {})
        self.event_bus.publish(
            IssueEvent.BLOCKED,
            entity_id=self.issue_number,
            data=data,
            source="IssueStateMachine"
        )
        logger.warning(f"Issue {self.issue_number} blocked")

    def _on_needs_human(self, event):
        """Callback for needs_human transition.

        Args:
            event: Transition event data from transitions library
        """
        data = event.kwargs.get('data', {})
        self.event_bus.publish(
            IssueEvent.NEEDS_HUMAN,
            entity_id=self.issue_number,
            data=data,
            source="IssueStateMachine"
        )
        logger.warning(f"Issue {self.issue_number} needs human intervention")

    def _on_unblocked(self, event):
        """Callback for unblock transition.

        Args:
            event: Transition event data from transitions library
        """
        data = event.kwargs.get('data', {})
        self.event_bus.publish(
            IssueEvent.UNBLOCKED,
            entity_id=self.issue_number,
            data=data,
            source="IssueStateMachine"
        )
        logger.info(f"Issue {self.issue_number} unblocked")

    def _on_pr_created(self, event):
        """Callback for pr_created transition.

        Args:
            event: Transition event data from transitions library
        """
        data = event.kwargs.get('data', {})
        self.event_bus.publish(
            IssueEvent.PR_CREATED,
            entity_id=self.issue_number,
            data=data,
            source="IssueStateMachine"
        )
        logger.info(f"PR created for issue {self.issue_number}")

    def _on_pr_rejected(self, event):
        """Callback for pr_closed transition.

        Args:
            event: Transition event data from transitions library
        """
        data = event.kwargs.get('data', {})
        self.event_bus.publish(
            IssueEvent.PR_REJECTED,
            entity_id=self.issue_number,
            data=data,
            source="IssueStateMachine"
        )
        logger.info(f"PR rejected for issue {self.issue_number}")

    def _on_completed(self, event):
        """Callback for pr_merged transition.

        Args:
            event: Transition event data from transitions library
        """
        data = event.kwargs.get('data', {})
        self.event_bus.publish(
            IssueEvent.COMPLETED,
            entity_id=self.issue_number,
            data=data,
            source="IssueStateMachine"
        )
        logger.info(f"Issue {self.issue_number} completed")

    def _on_released(self, event):
        """Callback for release transition.

        Args:
            event: Transition event data from transitions library
        """
        data = event.kwargs.get('data', {})
        self.event_bus.publish(
            IssueEvent.RELEASED,
            entity_id=self.issue_number,
            data=data,
            source="IssueStateMachine"
        )
        logger.info(f"Issue {self.issue_number} released back to available")

    def get_state(self) -> IssueState:
        """Get the current state as an enum.

        Returns:
            Current IssueState enum value
        """
        return IssueState(self.state)

    def can_transition(self, trigger: str) -> bool:
        """Check if a transition is valid from the current state.

        Args:
            trigger: Name of the transition to check

        Returns:
            True if the transition is valid, False otherwise
        """
        trigger_func = getattr(self, f'may_{trigger}', None)
        if trigger_func and callable(trigger_func):
            return bool(trigger_func())
        return False
