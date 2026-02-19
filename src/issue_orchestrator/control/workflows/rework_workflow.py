"""ReworkWorkflow - rework cycle management after review rejection.

This module encapsulates the decision logic for rework cycles:
- When to queue a rework session
- How to track rework cycle count
- When to escalate to human intervention

Usage:
    workflow = ReworkWorkflow(config=config, events=event_sink)
    decision = workflow.should_launch_reworks(pending_reworks, active_sessions, paused)
    if decision.should_launch:
        for rework in decision.reworks_to_launch:
            # Launch the rework session
"""

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional, Sequence

from ...infra.config import Config
from ...events import EventName
from ...domain.models import PendingRework
from ...ports import EventSink,  make_trace_event
from .decision_base import WorkflowDecision

if TYPE_CHECKING:
    from ..label_manager import LabelManager

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReworkDecision(WorkflowDecision[PendingRework]):
    """Decision about what rework actions to take.

    This is the output of the workflow's decision logic.
    """

    @property
    def reworks_to_launch(self) -> tuple[PendingRework, ...]:
        """Alias for items_to_launch for backwards compatibility."""
        return self.items_to_launch


@dataclass(frozen=True)
class EscalationDecision:
    """Decision about whether to escalate to human intervention."""

    should_escalate: bool = False
    reason: Optional[str] = None
    rework_cycle: int = 0
    max_cycles: int = 0

    @classmethod
    def escalate(cls, cycle: int, max_cycles: int, reason: str) -> "EscalationDecision":
        """Create a decision to escalate."""
        return cls(
            should_escalate=True,
            reason=reason,
            rework_cycle=cycle,
            max_cycles=max_cycles,
        )

    @classmethod
    def continue_rework(cls, cycle: int, max_cycles: int) -> "EscalationDecision":
        """Create a decision to continue with rework."""
        return cls(
            should_escalate=False,
            rework_cycle=cycle,
            max_cycles=max_cycles,
        )


class ReworkWorkflow:
    """Manages the rework cycle after review rejection.

    This workflow handles:
    - Determining when to launch rework sessions
    - Tracking rework cycle count
    - Deciding when to escalate to human review

    It contains POLICY (what should happen), not MECHANICS.
    """

    def __init__(self, config: Config, events: EventSink, label_manager: "LabelManager | None" = None):
        """Initialize the workflow.

        Args:
            config: Configuration with rework settings
            events: EventSink for trace events
            label_manager: Label registry for prefix-aware queries.
        """
        self.config = config
        self.events = events
        if label_manager is None:
            from ..label_manager import LabelManager
            label_manager = LabelManager(config)
        self._lm = label_manager

    def get_max_rework_cycles(self) -> int:
        """Get the maximum number of rework cycles before escalation."""
        return self.config.max_rework_cycles

    def should_launch_reworks(
        self,
        pending_reworks: Sequence[PendingRework],
        active_session_count: int,
        paused: bool,
    ) -> ReworkDecision:
        """Determine if and which reworks should be launched.

        Args:
            pending_reworks: Queue of pending reworks
            active_session_count: Number of active sessions
            paused: Whether the orchestrator is paused

        Returns:
            ReworkDecision describing what should happen
        """
        # Check if queue is empty
        if not pending_reworks:
            return ReworkDecision.skip("No pending reworks")

        # Check if paused
        if paused:
            self.events.publish(
                make_trace_event(
                    EventName.REWORK_SKIPPED,
                    {"reason": "orchestrator_paused"},
                )
            )
            return ReworkDecision.skip("Orchestrator paused")

        # Check capacity
        max_sessions = self.config.max_concurrent_sessions
        available = max_sessions - active_session_count

        if available <= 0:
            self.events.publish(
                make_trace_event(
                    EventName.REWORK_SKIPPED,
                    {
                        "reason": "no_capacity",
                        "active": active_session_count,
                        "max": max_sessions,
                    },
                )
            )
            return ReworkDecision.skip(
                f"No capacity (active={active_session_count}, max={max_sessions})"
            )

        # Determine which reworks to launch
        reworks_to_launch = list(pending_reworks)[:available]

        self.events.publish(
            make_trace_event(
                EventName.REWORK_LAUNCHING,
                {
                    "count": len(reworks_to_launch),
                    "capacity": available,
                    "pending": len(pending_reworks),
                },
            )
        )

        return ReworkDecision.launch(reworks_to_launch, available)

    def should_escalate(
        self,
        rework_cycle: int,
    ) -> EscalationDecision:
        """Determine if a PR should be escalated to human intervention.

        Args:
            rework_cycle: Current rework cycle number

        Returns:
            EscalationDecision with escalation details
        """
        max_cycles = self.get_max_rework_cycles()

        if rework_cycle >= max_cycles:
            reason = f"Exceeded max rework cycles ({rework_cycle} >= {max_cycles})"
            self.events.publish(
                make_trace_event(
                    EventName.REWORK_ESCALATING,
                    {
                        "rework_cycle": rework_cycle,
                        "max_cycles": max_cycles,
                        "reason": reason,
                    },
                )
            )
            return EscalationDecision.escalate(rework_cycle, max_cycles, reason)

        return EscalationDecision.continue_rework(rework_cycle, max_cycles)

    def extract_cycle_from_labels(self, labels: Sequence[str]) -> int:
        """Extract the rework cycle number from labels.

        Args:
            labels: List of label names

        Returns:
            Rework cycle number (0 if not found, meaning first cycle)
        """
        cycle = self._lm.extract_rework_cycle(labels)
        return cycle if cycle is not None else 0

    def get_next_cycle_label(self, current_cycle: int) -> str:
        """Get the label for the next rework cycle.

        Args:
            current_cycle: Current cycle number

        Returns:
            Label string for the next cycle
        """
        return self._lm.rework_cycle(current_cycle + 1)

    def get_current_cycle_label(self, cycle: int) -> str:
        """Get the label for the current rework cycle.

        Args:
            cycle: Cycle number

        Returns:
            Label string for the cycle
        """
        return self._lm.rework_cycle(cycle)
