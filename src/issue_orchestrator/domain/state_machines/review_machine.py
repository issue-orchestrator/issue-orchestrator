"""State machine for code review and merge lifecycle management.

This module implements the state machine for tracking a pull request through the
review, rework, and merge process.
"""

import logging
from enum import Enum
from typing import Optional

from transitions import Machine

from ..events import EventBus, ReviewEvent

logger = logging.getLogger(__name__)


class ReviewState(Enum):
    """States a code review can be in during its lifecycle."""

    PENDING = "pending"
    IN_REVIEW = "in_review"
    APPROVED = "approved"
    CHANGES_REQUESTED = "changes_requested"
    REWORK_PENDING = "rework_pending"
    REWORK_IN_PROGRESS = "rework_in_progress"
    CTO_PENDING = "cto_pending"
    CTO_REVIEWED = "cto_reviewed"
    MERGED = "merged"
    CLOSED = "closed"
    ESCALATED = "escalated"  # Rework limit exceeded, needs human intervention


class ReviewStateMachine:
    """State machine for managing code review and merge lifecycle.

    This state machine tracks a pull request through the review process:
    - PENDING: PR created, awaiting initial review
    - IN_REVIEW: PR is being actively reviewed
    - APPROVED: PR approved by reviewer
    - CHANGES_REQUESTED: Reviewer requested changes
    - REWORK_PENDING: Changes requested, rework not yet started
    - REWORK_IN_PROGRESS: Agent is addressing requested changes
    - CTO_PENDING: Awaiting CTO review (for complex changes)
    - CTO_REVIEWED: CTO has reviewed the changes
    - MERGED: PR has been merged
    - CLOSED: PR closed without merging
    - ESCALATED: Rework limit exceeded, requires human intervention

    The state machine tracks rework cycles and enforces limits. When the
    maximum rework cycles are exceeded, the review automatically escalates
    to ESCALATED state rather than silently blocking.

    Each state transition emits an event via the EventBus to enable
    decoupled components to react to state changes.

    Attributes:
        pr_number: The GitHub pull request number
        issue_number: The associated GitHub issue number
        state: Current state of the review
        event_bus: EventBus for publishing state change events
        rework_count: Number of times changes have been requested
        max_rework_cycles: Maximum allowed rework cycles (None for unlimited)
    """

    def __init__(
        self,
        pr_number: int,
        issue_number: int,
        event_bus: EventBus,
        initial_state: ReviewState = ReviewState.PENDING,
        max_rework_cycles: Optional[int] = None
    ):
        """Initialize the review state machine.

        Args:
            pr_number: The GitHub pull request number
            issue_number: The associated GitHub issue number
            event_bus: EventBus instance for publishing events
            initial_state: Starting state (defaults to PENDING)
            max_rework_cycles: Maximum allowed rework cycles (None for unlimited)
        """
        self.pr_number = pr_number
        self.issue_number = issue_number
        self.event_bus = event_bus
        self.state = initial_state
        self.rework_count = 0
        self.max_rework_cycles = max_rework_cycles

        # Define all possible states
        states = [state.value for state in ReviewState]

        # Define state transitions
        transitions = [
            # Start review on a pending PR
            {
                'trigger': 'start_review',
                'source': ReviewState.PENDING.value,
                'dest': ReviewState.IN_REVIEW.value,
                'after': '_on_review_started'
            },
            # PR approved from in_review
            {
                'trigger': 'approve',
                'source': ReviewState.IN_REVIEW.value,
                'dest': ReviewState.APPROVED.value,
                'after': '_on_approved'
            },
            # Changes requested from in_review
            {
                'trigger': 'request_changes',
                'source': ReviewState.IN_REVIEW.value,
                'dest': ReviewState.CHANGES_REQUESTED.value,
                'after': '_on_changes_requested',
                'before': '_increment_rework_count'
            },
            # Move from changes_requested to rework_pending (if within limit)
            {
                'trigger': 'queue_rework',
                'source': ReviewState.CHANGES_REQUESTED.value,
                'dest': ReviewState.REWORK_PENDING.value,
                'conditions': '_can_rework'
            },
            # Escalate when rework limit exceeded
            {
                'trigger': 'escalate',
                'source': ReviewState.CHANGES_REQUESTED.value,
                'dest': ReviewState.ESCALATED.value,
                'after': '_on_escalated'
            },
            # Start rework
            {
                'trigger': 'start_rework',
                'source': ReviewState.REWORK_PENDING.value,
                'dest': ReviewState.REWORK_IN_PROGRESS.value,
                'after': '_on_rework_started'
            },
            # Rework completed, return to in_review
            {
                'trigger': 'complete_rework',
                'source': ReviewState.REWORK_IN_PROGRESS.value,
                'dest': ReviewState.IN_REVIEW.value,
                'after': '_on_rework_completed'
            },
            # Send approved PR to CTO review
            {
                'trigger': 'request_cto_review',
                'source': ReviewState.APPROVED.value,
                'dest': ReviewState.CTO_PENDING.value,
                'after': '_on_cto_review_started'
            },
            # CTO review completed
            {
                'trigger': 'cto_reviewed',
                'source': ReviewState.CTO_PENDING.value,
                'dest': ReviewState.CTO_REVIEWED.value,
                'after': '_on_cto_reviewed'
            },
            # Merge from approved or cto_reviewed state
            {
                'trigger': 'merge',
                'source': [ReviewState.APPROVED.value, ReviewState.CTO_REVIEWED.value],
                'dest': ReviewState.MERGED.value,
                'after': '_on_merged'
            },
            # Close PR from various states
            {
                'trigger': 'close',
                'source': [
                    ReviewState.PENDING.value,
                    ReviewState.IN_REVIEW.value,
                    ReviewState.APPROVED.value,
                    ReviewState.CHANGES_REQUESTED.value,
                    ReviewState.REWORK_PENDING.value,
                    ReviewState.REWORK_IN_PROGRESS.value,
                    ReviewState.CTO_PENDING.value,
                    ReviewState.CTO_REVIEWED.value
                ],
                'dest': ReviewState.CLOSED.value,
                'after': '_on_closed'
            },
            # Reopen from in_review (if more changes requested after CTO review)
            {
                'trigger': 'request_changes_after_cto',
                'source': ReviewState.CTO_REVIEWED.value,
                'dest': ReviewState.CHANGES_REQUESTED.value,
                'after': '_on_changes_requested',
                'before': '_increment_rework_count'
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

        logger.info(f"ReviewStateMachine initialized for PR {pr_number} in state {initial_state.value}")

    def _increment_rework_count(self, event):
        """Increment the rework count before requesting changes.

        Args:
            event: Transition event data from transitions library
        """
        self.rework_count += 1
        logger.info(f"PR {self.pr_number} rework count incremented to {self.rework_count}")

    def _can_rework(self, event) -> bool:
        """Check if rework is allowed based on max_rework_cycles.

        Args:
            event: Transition event data from transitions library

        Returns:
            True if rework is allowed, False if max cycles exceeded
        """
        if self.max_rework_cycles is None:
            return True

        can_rework = self.rework_count <= self.max_rework_cycles
        if not can_rework:
            logger.warning(
                f"PR {self.pr_number} has exceeded max rework cycles "
                f"({self.rework_count} > {self.max_rework_cycles})"
            )
        return can_rework

    def _on_review_started(self, event):
        """Callback for start_review transition.

        Args:
            event: Transition event data from transitions library
        """
        data = event.kwargs.get('data', {})
        self.event_bus.publish(
            ReviewEvent.REVIEW_STARTED,
            entity_id=self.pr_number,
            data={**data, 'issue_number': self.issue_number},
            source="ReviewStateMachine"
        )
        logger.info(f"Review started for PR {self.pr_number}")

    def _on_approved(self, event):
        """Callback for approve transition.

        Args:
            event: Transition event data from transitions library
        """
        data = event.kwargs.get('data', {})
        self.event_bus.publish(
            ReviewEvent.APPROVED,
            entity_id=self.pr_number,
            data={**data, 'issue_number': self.issue_number},
            source="ReviewStateMachine"
        )
        logger.info(f"PR {self.pr_number} approved")

    def _on_changes_requested(self, event):
        """Callback for request_changes transition.

        Args:
            event: Transition event data from transitions library
        """
        data = event.kwargs.get('data', {})
        self.event_bus.publish(
            ReviewEvent.CHANGES_REQUESTED,
            entity_id=self.pr_number,
            data={**data, 'issue_number': self.issue_number, 'rework_count': self.rework_count},
            source="ReviewStateMachine"
        )
        logger.info(f"Changes requested for PR {self.pr_number} (rework count: {self.rework_count})")

    def _on_rework_started(self, event):
        """Callback for start_rework transition.

        Args:
            event: Transition event data from transitions library
        """
        data = event.kwargs.get('data', {})
        self.event_bus.publish(
            ReviewEvent.REWORK_STARTED,
            entity_id=self.pr_number,
            data={**data, 'issue_number': self.issue_number, 'rework_count': self.rework_count},
            source="ReviewStateMachine"
        )
        logger.info(f"Rework started for PR {self.pr_number}")

    def _on_rework_completed(self, event):
        """Callback for complete_rework transition.

        Args:
            event: Transition event data from transitions library
        """
        data = event.kwargs.get('data', {})
        self.event_bus.publish(
            ReviewEvent.REWORK_COMPLETED,
            entity_id=self.pr_number,
            data={**data, 'issue_number': self.issue_number, 'rework_count': self.rework_count},
            source="ReviewStateMachine"
        )
        logger.info(f"Rework completed for PR {self.pr_number}")

    def _on_cto_review_started(self, event):
        """Callback for request_cto_review transition.

        Args:
            event: Transition event data from transitions library
        """
        data = event.kwargs.get('data', {})
        self.event_bus.publish(
            ReviewEvent.CTO_REVIEW_STARTED,
            entity_id=self.pr_number,
            data={**data, 'issue_number': self.issue_number},
            source="ReviewStateMachine"
        )
        logger.info(f"CTO review requested for PR {self.pr_number}")

    def _on_cto_reviewed(self, event):
        """Callback for cto_reviewed transition.

        Args:
            event: Transition event data from transitions library
        """
        data = event.kwargs.get('data', {})
        self.event_bus.publish(
            ReviewEvent.CTO_APPROVED,
            entity_id=self.pr_number,
            data={**data, 'issue_number': self.issue_number},
            source="ReviewStateMachine"
        )
        logger.info(f"CTO review completed for PR {self.pr_number}")

    def _on_merged(self, event):
        """Callback for merge transition.

        Args:
            event: Transition event data from transitions library
        """
        data = event.kwargs.get('data', {})
        self.event_bus.publish(
            ReviewEvent.MERGED,
            entity_id=self.pr_number,
            data={**data, 'issue_number': self.issue_number, 'rework_count': self.rework_count},
            source="ReviewStateMachine"
        )
        logger.info(f"PR {self.pr_number} merged (after {self.rework_count} rework cycles)")

    def _on_closed(self, event):
        """Callback for close transition.

        Args:
            event: Transition event data from transitions library
        """
        data = event.kwargs.get('data', {})
        self.event_bus.publish(
            ReviewEvent.CLOSED,
            entity_id=self.pr_number,
            data={**data, 'issue_number': self.issue_number, 'rework_count': self.rework_count},
            source="ReviewStateMachine"
        )
        logger.info(f"PR {self.pr_number} closed without merging")

    def _on_escalated(self, event):
        """Callback for escalate transition.

        This is called when the rework limit has been exceeded and the
        review needs human intervention to proceed.

        Args:
            event: Transition event data from transitions library
        """
        data = event.kwargs.get('data', {})
        self.event_bus.publish(
            ReviewEvent.ESCALATED,
            entity_id=self.pr_number,
            data={
                **data,
                'issue_number': self.issue_number,
                'rework_count': self.rework_count,
                'max_rework_cycles': self.max_rework_cycles,
            },
            source="ReviewStateMachine"
        )
        logger.warning(
            f"PR {self.pr_number} ESCALATED: exceeded rework limit "
            f"({self.rework_count}/{self.max_rework_cycles} cycles)"
        )

    def get_state(self) -> ReviewState:
        """Get the current state as an enum.

        Returns:
            Current ReviewState enum value
        """
        return ReviewState(self.state)

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

    def get_rework_info(self) -> dict:
        """Get rework information for this review.

        Returns:
            Dictionary with rework information including:
            - rework_count: Number of times changes have been requested
            - max_rework_cycles: Maximum allowed rework cycles (or None)
            - can_rework: Whether another rework cycle is allowed
        """
        can_rework = True
        if self.max_rework_cycles is not None:
            can_rework = self.rework_count < self.max_rework_cycles

        return {
            'rework_count': self.rework_count,
            'max_rework_cycles': self.max_rework_cycles,
            'can_rework': can_rework
        }

    def has_exceeded_rework_limit(self) -> bool:
        """Check if the review has exceeded the maximum rework cycles.

        Returns:
            True if max cycles exceeded, False otherwise
        """
        if self.max_rework_cycles is None:
            return False
        return self.rework_count > self.max_rework_cycles
