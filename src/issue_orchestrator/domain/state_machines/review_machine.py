"""State machine for code review and merge lifecycle management.

This module implements the state machine for tracking a pull request through the
review, rework, and merge process.

The state machine is pure - it returns TransitionResult instead of publishing
events directly. The caller (control layer) is responsible for emitting
TraceEvents via EventSink.
"""

import logging
from enum import Enum
from typing import Any, Optional

from transitions import MachineError, EventData, Machine

from .transition_result import TransitionResult
from .errors import InvalidStateTransition

logger = logging.getLogger(__name__)


class ReviewState(Enum):
    """States a code review can be in during its lifecycle."""

    PENDING = "pending"
    IN_REVIEW = "in_review"
    APPROVED = "approved"
    CHANGES_REQUESTED = "changes_requested"
    REWORK_PENDING = "rework_pending"
    REWORK_IN_PROGRESS = "rework_in_progress"
    TECH_LEAD_PENDING = "tech_lead_pending"
    TECH_LEAD_REVIEWED = "tech_lead_reviewed"
    MERGED = "merged"
    CLOSED = "closed"
    ESCALATED = "escalated"  # Rework limit exceeded, needs human intervention


# NOTE:
# This class is the mutation target for `transitions.Machine`.
# It must remain a simple data object with no logic or recursion.
class _Model:
    """Internal transitions model.

    transitions injects trigger methods onto this object. The outer ReviewStateMachine
    wrapper exposes a typed/stable API and prevents dynamic methods from leaking.
    """

    def __init__(self, initial_state: str) -> None:
        self.state = initial_state


class ReviewStateMachine:
    """State machine for managing code review and merge lifecycle.

    This state machine tracks a pull request through the review process:
    - PENDING: PR created, awaiting initial review
    - IN_REVIEW: PR is being actively reviewed
    - APPROVED: PR approved by reviewer
    - CHANGES_REQUESTED: Reviewer requested changes
    - REWORK_PENDING: Changes requested, rework not yet started
    - REWORK_IN_PROGRESS: Agent is addressing requested changes
    - TECH_LEAD_PENDING: Awaiting tech_lead review (for complex changes)
    - TECH_LEAD_REVIEWED: tech_lead has reviewed the changes
    - MERGED: PR has been merged
    - CLOSED: PR closed without merging
    - ESCALATED: Rework limit exceeded, requires human intervention

    The state machine tracks rework cycles and enforces limits. When the
    maximum rework cycles are exceeded, the review automatically escalates
    to ESCALATED state rather than silently blocking.

    The state machine is pure - transitions store their result in last_transition
    which callers use to emit appropriate TraceEvents via EventSink.

    Attributes:
        pr_number: The GitHub pull request number
        issue_number: The associated GitHub issue number
        state: Current state of the review
        rework_count: Number of times changes have been requested
        max_rework_cycles: Maximum allowed rework cycles (None for unlimited)
        last_transition: Result of the most recent transition (for event emission)
    """

    def __init__(
        self,
        pr_number: int,
        issue_number: int,
        initial_state: ReviewState = ReviewState.PENDING,
        max_rework_cycles: Optional[int] = None
    ):
        """Initialize the review state machine.

        Args:
            pr_number: The GitHub pull request number
            issue_number: The associated GitHub issue number
            initial_state: Starting state (defaults to PENDING)
            max_rework_cycles: Maximum allowed rework cycles (None for unlimited)
        """
        self.pr_number = pr_number
        self.issue_number = issue_number
        self._model = _Model(initial_state.value)
        self.state = self._model.state
        self.rework_count = 0
        self.max_rework_cycles = max_rework_cycles
        self.last_transition: Optional[TransitionResult] = None

        # Define all possible states
        states = [state.value for state in ReviewState]

        # Define state transitions
        transitions = [
            # Start review on a pending PR
            {
                'trigger': 'start_review',
                'source': ReviewState.PENDING.value,
                'dest': ReviewState.IN_REVIEW.value,
                'after': self._on_review_started
            },
            # PR approved from in_review
            {
                'trigger': 'approve',
                'source': ReviewState.IN_REVIEW.value,
                'dest': ReviewState.APPROVED.value,
                'after': self._on_approved
            },
            # Changes requested from in_review
            {
                'trigger': 'request_changes',
                'source': ReviewState.IN_REVIEW.value,
                'dest': ReviewState.CHANGES_REQUESTED.value,
                'after': self._on_changes_requested,
                'before': self._increment_rework_count
            },
            # Move from changes_requested to rework_pending (if within limit)
            {
                'trigger': 'queue_rework',
                'source': ReviewState.CHANGES_REQUESTED.value,
                'dest': ReviewState.REWORK_PENDING.value,
                'conditions': self._can_rework
            },
            # Escalate when rework limit exceeded
            {
                'trigger': 'escalate',
                'source': ReviewState.CHANGES_REQUESTED.value,
                'dest': ReviewState.ESCALATED.value,
                'after': self._on_escalated
            },
            # Start rework
            {
                'trigger': 'start_rework',
                'source': ReviewState.REWORK_PENDING.value,
                'dest': ReviewState.REWORK_IN_PROGRESS.value,
                'after': self._on_rework_started
            },
            # Rework completed, return to in_review
            {
                'trigger': 'complete_rework',
                'source': ReviewState.REWORK_IN_PROGRESS.value,
                'dest': ReviewState.IN_REVIEW.value,
                'after': self._on_rework_completed
            },
            # Send approved PR to tech_lead review
            {
                'trigger': 'request_tech_lead_review',
                'source': ReviewState.APPROVED.value,
                'dest': ReviewState.TECH_LEAD_PENDING.value,
                'after': self._on_tech_lead_review_started
            },
            # tech_lead review completed
            {
                'trigger': 'tech_lead_reviewed',
                'source': ReviewState.TECH_LEAD_PENDING.value,
                'dest': ReviewState.TECH_LEAD_REVIEWED.value,
                'after': self._on_tech_lead_reviewed
            },
            # Merge from approved or tech_lead_reviewed state
            {
                'trigger': 'merge',
                'source': [ReviewState.APPROVED.value, ReviewState.TECH_LEAD_REVIEWED.value],
                'dest': ReviewState.MERGED.value,
                'after': self._on_merged
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
                    ReviewState.TECH_LEAD_PENDING.value,
                    ReviewState.TECH_LEAD_REVIEWED.value
                ],
                'dest': ReviewState.CLOSED.value,
                'after': self._on_closed
            },
            # Reopen from in_review (if more changes requested after tech_lead review)
            {
                'trigger': 'request_changes_after_tech_lead',
                'source': ReviewState.TECH_LEAD_REVIEWED.value,
                'dest': ReviewState.CHANGES_REQUESTED.value,
                'after': self._on_changes_requested,
                'before': self._increment_rework_count
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

        logger.info(f"ReviewStateMachine initialized for PR {pr_number} in state {initial_state.value}")

    def _increment_rework_count(self, event: EventData) -> None:
        """Increment the rework count before requesting changes."""
        self.rework_count += 1
        logger.info(f"PR {self.pr_number} rework count incremented to {self.rework_count}")

    def _can_rework(self, event: EventData) -> bool:
        """Check if rework is allowed based on max_rework_cycles.

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

    def _on_review_started(self, event: EventData) -> None:
        """Callback for start_review transition."""
        data = event.kwargs.get('data', {})
        self.last_transition = TransitionResult(
            success=True,
            from_state=ReviewState.PENDING.value,
            to_state=ReviewState.IN_REVIEW.value,
            event_name="review.started",
            entity_id=self.pr_number,
            data={**data, 'issue_number': self.issue_number},
        )
        logger.info(f"Review started for PR {self.pr_number}")

    def _on_approved(self, event: EventData) -> None:
        """Callback for approve transition."""
        data = event.kwargs.get('data', {})
        self.last_transition = TransitionResult(
            success=True,
            from_state=ReviewState.IN_REVIEW.value,
            to_state=ReviewState.APPROVED.value,
            event_name="review.approved",
            entity_id=self.pr_number,
            data={**data, 'issue_number': self.issue_number},
        )
        logger.info(f"PR {self.pr_number} approved")

    def _on_changes_requested(self, event: EventData) -> None:
        """Callback for request_changes transition."""
        data = event.kwargs.get('data', {})
        from_state = event.transition.source if hasattr(event, 'transition') and event.transition else ReviewState.IN_REVIEW.value
        self.last_transition = TransitionResult(
            success=True,
            from_state=from_state,
            to_state=ReviewState.CHANGES_REQUESTED.value,
            event_name="review.changes_requested",
            entity_id=self.pr_number,
            data={**data, 'issue_number': self.issue_number, 'rework_count': self.rework_count},
        )
        logger.info(f"Changes requested for PR {self.pr_number} (rework count: {self.rework_count})")

    def _on_rework_started(self, event: EventData) -> None:
        """Callback for start_rework transition."""
        data = event.kwargs.get('data', {})
        self.last_transition = TransitionResult(
            success=True,
            from_state=ReviewState.REWORK_PENDING.value,
            to_state=ReviewState.REWORK_IN_PROGRESS.value,
            event_name="review.rework_started",
            entity_id=self.pr_number,
            data={**data, 'issue_number': self.issue_number, 'rework_count': self.rework_count},
        )
        logger.info(f"Rework started for PR {self.pr_number}")

    def _on_rework_completed(self, event: EventData) -> None:
        """Callback for complete_rework transition."""
        data = event.kwargs.get('data', {})
        self.last_transition = TransitionResult(
            success=True,
            from_state=ReviewState.REWORK_IN_PROGRESS.value,
            to_state=ReviewState.IN_REVIEW.value,
            event_name="review.rework_completed",
            entity_id=self.pr_number,
            data={**data, 'issue_number': self.issue_number, 'rework_count': self.rework_count},
        )
        logger.info(f"Rework completed for PR {self.pr_number}")

    def _on_tech_lead_review_started(self, event: EventData) -> None:
        """Callback for request_tech_lead_review transition."""
        data = event.kwargs.get('data', {})
        self.last_transition = TransitionResult(
            success=True,
            from_state=ReviewState.APPROVED.value,
            to_state=ReviewState.TECH_LEAD_PENDING.value,
            event_name="review.tech_lead_started",
            entity_id=self.pr_number,
            data={**data, 'issue_number': self.issue_number},
        )
        logger.info(f"tech_lead review requested for PR {self.pr_number}")

    def _on_tech_lead_reviewed(self, event: EventData) -> None:
        """Callback for tech_lead_reviewed transition."""
        data = event.kwargs.get('data', {})
        self.last_transition = TransitionResult(
            success=True,
            from_state=ReviewState.TECH_LEAD_PENDING.value,
            to_state=ReviewState.TECH_LEAD_REVIEWED.value,
            event_name="review.tech_lead_approved",
            entity_id=self.pr_number,
            data={**data, 'issue_number': self.issue_number},
        )
        logger.info(f"tech_lead review completed for PR {self.pr_number}")

    def _on_merged(self, event: EventData) -> None:
        """Callback for merge transition."""
        data = event.kwargs.get('data', {})
        from_state = event.transition.source if hasattr(event, 'transition') and event.transition else ReviewState.APPROVED.value
        self.last_transition = TransitionResult(
            success=True,
            from_state=from_state,
            to_state=ReviewState.MERGED.value,
            event_name="review.merged",
            entity_id=self.pr_number,
            data={**data, 'issue_number': self.issue_number, 'rework_count': self.rework_count},
        )
        logger.info(f"PR {self.pr_number} merged (after {self.rework_count} rework cycles)")

    def _on_closed(self, event: EventData) -> None:
        """Callback for close transition."""
        data = event.kwargs.get('data', {})
        from_state = event.transition.source if hasattr(event, 'transition') and event.transition else ReviewState.PENDING.value
        self.last_transition = TransitionResult(
            success=True,
            from_state=from_state,
            to_state=ReviewState.CLOSED.value,
            event_name="review.closed",
            entity_id=self.pr_number,
            data={**data, 'issue_number': self.issue_number, 'rework_count': self.rework_count},
        )
        logger.info(f"PR {self.pr_number} closed without merging")

    def _on_escalated(self, event: EventData) -> None:
        """Callback for escalate transition.

        This is called when the rework limit has been exceeded and the
        review needs human intervention to proceed.
        """
        data = event.kwargs.get('data', {})
        self.last_transition = TransitionResult(
            success=True,
            from_state=ReviewState.CHANGES_REQUESTED.value,
            to_state=ReviewState.ESCALATED.value,
            event_name="review.escalated",
            entity_id=self.pr_number,
            data={
                **data,
                'issue_number': self.issue_number,
                'rework_count': self.rework_count,
                'max_rework_cycles': self.max_rework_cycles,
            },
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
        trigger_func = getattr(self._model, f'may_{trigger}', None)
        if trigger_func and callable(trigger_func):
            return bool(trigger_func())
        return False

    def get_rework_info(self) -> dict[str, Any]:
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

    def start_review(self, **kwargs: Any) -> None:
        """Start the review."""
        self._invoke('start_review', **kwargs)

    def approve(self, **kwargs: Any) -> None:
        """Approve the review."""
        self._invoke('approve', **kwargs)

    def request_changes(self, **kwargs: Any) -> None:
        """Request changes on the review."""
        self._invoke('request_changes', **kwargs)

    def queue_rework(self, **kwargs: Any) -> None:
        """Queue the review for rework."""
        self._invoke('queue_rework', **kwargs)

    def escalate(self, **kwargs: Any) -> None:
        """Escalate the review."""
        self._invoke('escalate', **kwargs)

    def start_rework(self, **kwargs: Any) -> None:
        """Start rework on the review."""
        self._invoke('start_rework', **kwargs)

    def complete_rework(self, **kwargs: Any) -> None:
        """Complete rework on the review."""
        self._invoke('complete_rework', **kwargs)

    def request_tech_lead_review(self, **kwargs: Any) -> None:
        """Request tech_lead review."""
        self._invoke('request_tech_lead_review', **kwargs)

    def tech_lead_reviewed(self, **kwargs: Any) -> None:
        """Mark tech_lead review as completed."""
        self._invoke('tech_lead_reviewed', **kwargs)

    def merge(self, **kwargs: Any) -> None:
        """Merge the PR."""
        self._invoke('merge', **kwargs)

    def close(self, **kwargs: Any) -> None:
        """Close the PR."""
        self._invoke('close', **kwargs)

    def request_changes_after_tech_lead(self, **kwargs: Any) -> None:
        """Request changes after tech_lead."""
        self._invoke('request_changes_after_tech_lead', **kwargs)
