"""TriageWorkflow - failure investigation and batch triage management.

This module encapsulates the decision logic for triage:
- When to trigger a triage review (failure batch threshold)
- When to queue a triage investigation
- How to handle triage outcomes

Usage:
    workflow = TriageWorkflow(config=config, events=event_sink)
    decision = workflow.should_launch_triage(pending_triage, active_sessions, paused)
    if decision.should_launch:
        for triage in decision.triage_to_launch:
            # Launch the triage session
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional, Sequence

from ...config import Config
from ...models import PendingTriageReview
from ...ports import EventSink, TraceEvent

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TriageDecision:
    """Decision about what triage actions to take.

    This is the output of the workflow's decision logic.
    """

    should_launch: bool = False
    triage_to_launch: tuple[PendingTriageReview, ...] = field(default_factory=tuple)
    skip_reason: Optional[str] = None
    available_capacity: int = 0

    @classmethod
    def skip(cls, reason: str) -> "TriageDecision":
        """Create a decision to skip triage processing."""
        return cls(should_launch=False, skip_reason=reason)

    @classmethod
    def launch(
        cls,
        triage: Sequence[PendingTriageReview],
        capacity: int,
    ) -> "TriageDecision":
        """Create a decision to launch triage sessions."""
        return cls(
            should_launch=True,
            triage_to_launch=tuple(triage),
            available_capacity=capacity,
        )


@dataclass(frozen=True)
class BatchTriageDecision:
    """Decision about whether to trigger a batch triage review."""

    should_trigger: bool = False
    failure_count: int = 0
    threshold: int = 0
    cooldown_remaining: Optional[timedelta] = None

    @classmethod
    def trigger(cls, failure_count: int, threshold: int) -> "BatchTriageDecision":
        """Create a decision to trigger batch triage."""
        return cls(
            should_trigger=True,
            failure_count=failure_count,
            threshold=threshold,
        )

    @classmethod
    def skip_cooldown(
        cls,
        failure_count: int,
        threshold: int,
        remaining: timedelta,
    ) -> "BatchTriageDecision":
        """Create a decision to skip due to cooldown."""
        return cls(
            should_trigger=False,
            failure_count=failure_count,
            threshold=threshold,
            cooldown_remaining=remaining,
        )

    @classmethod
    def skip_threshold(cls, failure_count: int, threshold: int) -> "BatchTriageDecision":
        """Create a decision to skip due to threshold not met."""
        return cls(
            should_trigger=False,
            failure_count=failure_count,
            threshold=threshold,
        )


class TriageWorkflow:
    """Manages triage reviews for failures and batch investigations.

    This workflow handles:
    - Determining when to launch triage sessions
    - Tracking failure batches for batch triage
    - Deciding when to trigger batch triage reviews

    It contains POLICY (what should happen), not MECHANICS.
    """

    def __init__(self, config: Config, events: EventSink):
        """Initialize the workflow.

        Args:
            config: Configuration with triage settings
            events: EventSink for trace events
        """
        self.config = config
        self.events = events
        self._last_batch_triage: Optional[datetime] = None

    def is_configured(self) -> bool:
        """Check if triage review is configured."""
        return self.config.triage_review_agent is not None

    def should_launch_triage(
        self,
        pending_triage: Sequence[PendingTriageReview],
        active_session_count: int,
        paused: bool,
    ) -> TriageDecision:
        """Determine if and which triage reviews should be launched.

        Args:
            pending_triage: Queue of pending triage reviews
            active_session_count: Number of active sessions
            paused: Whether the orchestrator is paused

        Returns:
            TriageDecision describing what should happen
        """
        # Check if configured
        if not self.is_configured():
            return TriageDecision.skip("No triage_review_agent configured")

        # Check if queue is empty
        if not pending_triage:
            return TriageDecision.skip("No pending triage reviews")

        # Check if paused
        if paused:
            self.events.publish(
                TraceEvent(
                    name="triage.skipped",
                    data={"reason": "orchestrator_paused"},
                )
            )
            return TriageDecision.skip("Orchestrator paused")

        # Check capacity
        max_sessions = self.config.max_concurrent_sessions
        available = max_sessions - active_session_count

        if available <= 0:
            self.events.publish(
                TraceEvent(
                    name="triage.skipped",
                    data={
                        "reason": "no_capacity",
                        "active": active_session_count,
                        "max": max_sessions,
                    },
                )
            )
            return TriageDecision.skip(
                f"No capacity (active={active_session_count}, max={max_sessions})"
            )

        # Determine which triage reviews to launch
        triage_to_launch = list(pending_triage)[:available]

        self.events.publish(
            TraceEvent(
                name="triage.launching",
                data={
                    "count": len(triage_to_launch),
                    "capacity": available,
                    "pending": len(pending_triage),
                },
            )
        )

        return TriageDecision.launch(triage_to_launch, available)

    def get_batch_threshold(self) -> int:
        """Get the failure count threshold for batch triage."""
        return self.config.triage_review_threshold or 3

    def get_batch_cooldown(self) -> timedelta:
        """Get the cooldown period between batch triage reviews."""
        # Default to 30 minutes cooldown between batch triage reviews
        return timedelta(minutes=30)

    def should_trigger_batch_triage(
        self,
        failure_count: int,
        now: Optional[datetime] = None,
    ) -> BatchTriageDecision:
        """Determine if a batch triage review should be triggered.

        Args:
            failure_count: Number of recent failures
            now: Current time (for testing)

        Returns:
            BatchTriageDecision with trigger details
        """
        if not self.is_configured():
            return BatchTriageDecision.skip_threshold(failure_count, 0)

        threshold = self.get_batch_threshold()

        # Check threshold
        if failure_count < threshold:
            return BatchTriageDecision.skip_threshold(failure_count, threshold)

        # Check cooldown
        if self._last_batch_triage is not None:
            now = now or datetime.now()
            cooldown = self.get_batch_cooldown()
            elapsed = now - self._last_batch_triage
            if elapsed < cooldown:
                remaining = cooldown - elapsed
                return BatchTriageDecision.skip_cooldown(
                    failure_count, threshold, remaining
                )

        self.events.publish(
            TraceEvent(
                name="triage.batch_triggered",
                data={
                    "failure_count": failure_count,
                    "threshold": threshold,
                },
            )
        )

        return BatchTriageDecision.trigger(failure_count, threshold)

    def record_batch_triage_started(self, now: Optional[datetime] = None) -> None:
        """Record that a batch triage review was started.

        Args:
            now: Current time (for testing)
        """
        self._last_batch_triage = now or datetime.now()

    def should_queue_failure_investigation(
        self,
        issue_number: int,
        failure_reason: str,
        already_queued: Sequence[PendingTriageReview],
    ) -> bool:
        """Determine if a failure investigation should be queued.

        Args:
            issue_number: The issue number that failed
            failure_reason: Reason for the failure
            already_queued: Currently queued triage reviews

        Returns:
            True if the investigation should be queued
        """
        if not self.is_configured():
            return False

        # Check if already queued
        for triage in already_queued:
            if triage.issue_number == issue_number:
                logger.debug(
                    f"Triage for issue #{issue_number} already queued"
                )
                return False

        return True
