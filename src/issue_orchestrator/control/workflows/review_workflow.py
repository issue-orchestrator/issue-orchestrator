"""ReviewWorkflow - code review lifecycle management.

This module encapsulates the decision logic for code reviews:
- When to queue a review
- When to launch a review session
- How to handle review completion (approved/changes_requested)
- When to escalate to human intervention

Usage:
    workflow = ReviewWorkflow(config=config, events=event_sink)
    decision = workflow.should_launch_review(pending_reviews, active_sessions, paused)
    if decision.should_launch:
        for review in decision.reviews_to_launch:
            # Launch the review session
"""

import logging
from dataclasses import dataclass, field
from typing import Optional, Sequence

from ...infra.config import Config
from ...events import EventName
from ...domain.models import PendingReview
from ...ports import EventSink, TraceEvent

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReviewDecision:
    """Decision about what review actions to take.

    This is the output of the workflow's decision logic.
    It describes WHAT should happen, not HOW.
    """

    should_launch: bool = False
    reviews_to_launch: tuple[PendingReview, ...] = field(default_factory=tuple)
    skip_reason: Optional[str] = None
    available_capacity: int = 0

    @classmethod
    def skip(cls, reason: str) -> "ReviewDecision":
        """Create a decision to skip review processing."""
        return cls(should_launch=False, skip_reason=reason)

    @classmethod
    def launch(
        cls,
        reviews: Sequence[PendingReview],
        capacity: int,
    ) -> "ReviewDecision":
        """Create a decision to launch reviews."""
        return cls(
            should_launch=True,
            reviews_to_launch=tuple(reviews),
            available_capacity=capacity,
        )


class ReviewWorkflow:
    """Manages the code review lifecycle.

    This workflow handles:
    - Determining when to launch review sessions
    - Tracking review state and outcomes
    - Deciding when to escalate to human review

    It contains POLICY (what should happen), not MECHANICS.
    """

    def __init__(self, config: Config, events: EventSink):
        """Initialize the workflow.

        Args:
            config: Configuration with review settings
            events: EventSink for trace events
        """
        self.config = config
        self.events = events

    def is_configured(self) -> bool:
        """Check if code review is configured."""
        return self.config.code_review_agent is not None

    def should_launch_reviews(
        self,
        pending_reviews: Sequence[PendingReview],
        active_session_count: int,
        paused: bool,
    ) -> ReviewDecision:
        """Determine if and which reviews should be launched.

        This is the main decision point for review launching.

        Args:
            pending_reviews: Queue of pending reviews
            active_session_count: Number of active sessions
            paused: Whether the orchestrator is paused

        Returns:
            ReviewDecision describing what should happen
        """
        # Check if configured
        if not self.is_configured():
            logger.info("Review launch skipped: no code_review_agent configured")
            return ReviewDecision.skip("No code_review_agent configured")

        # Check if queue is empty
        if not pending_reviews:
            logger.debug("Review launch skipped: no pending reviews")
            return ReviewDecision.skip("No pending reviews")

        # Check if paused
        if paused:
            self.events.publish(
                TraceEvent(
                    EventName.REVIEW_SKIPPED,
                    {"reason": "orchestrator_paused"},
                )
            )
            logger.info("Review launch skipped: orchestrator paused")
            return ReviewDecision.skip("Orchestrator paused")

        # Check capacity
        max_sessions = self.config.max_concurrent_sessions
        available = max_sessions - active_session_count

        if available <= 0:
            self.events.publish(
                TraceEvent(
                    EventName.REVIEW_SKIPPED,
                    {
                        "reason": "no_capacity",
                        "active": active_session_count,
                        "max": max_sessions,
                    },
                )
            )
            logger.info(
                "Review launch skipped: no capacity (active=%s max=%s)",
                active_session_count,
                max_sessions,
            )
            return ReviewDecision.skip(
                f"No capacity (active={active_session_count}, max={max_sessions})"
            )

        # Determine which reviews to launch
        reviews_to_launch = list(pending_reviews)[:available]

        self.events.publish(
            TraceEvent(
                EventName.REVIEW_LAUNCHING,
                {
                    "count": len(reviews_to_launch),
                    "capacity": available,
                    "pending": len(pending_reviews),
                },
            )
        )
        logger.info(
            "Review launch decision: launching=%s pending=%s capacity=%s",
            len(reviews_to_launch),
            len(pending_reviews),
            available,
        )

        return ReviewDecision.launch(reviews_to_launch, available)

    def should_queue_review(
        self,
        issue_number: int,
        pr_url: str,
        already_queued: Sequence[PendingReview],
    ) -> bool:
        """Determine if a review should be queued.

        Args:
            issue_number: The issue number
            pr_url: URL of the PR to review
            already_queued: Currently queued reviews

        Returns:
            True if the review should be queued
        """
        if not self.is_configured():
            return False

        # Check if already queued
        for review in already_queued:
            if review.issue_number == issue_number:
                logger.debug(f"Review for issue #{issue_number} already queued")
                return False

        return True

    def should_escalate(
        self,
        pr_number: int,
        rework_cycles: int,
    ) -> bool:
        """Determine if a PR should be escalated to human review.

        Args:
            pr_number: The PR number
            rework_cycles: Number of rework cycles completed

        Returns:
            True if should escalate to human review
        """
        max_cycles = self.config.max_rework_cycles
        return rework_cycles >= max_cycles

    def get_max_rework_cycles(self) -> int:
        """Get the maximum number of rework cycles before escalation."""
        return self.config.max_rework_cycles
