"""TriageWorkflow - triage review launch policy.

This module encapsulates the decision logic for launching triage review
sessions from the pending-triage queue.

The actual batch trigger (deciding WHEN a triage review should be created)
lives in the fact-gathering/planning path:
`fact_gatherer.gather_triage_facts` -> `planner._plan_triage_issue_creation`.

Usage:
    workflow = TriageWorkflow(config=config, events=event_sink)
    decision = workflow.should_launch_triage(pending_triage, active_sessions, paused)
    if decision.should_launch:
        for triage in decision.triage_to_launch:
            # Launch the triage session
"""

from dataclasses import dataclass
from typing import Sequence

from ...infra.config import Config
from ...events import EventName
from ...domain.models import PendingTriageReview
from ...ports import EventSink,  make_trace_event
from .decision_base import WorkflowDecision


@dataclass(frozen=True)
class TriageDecision(WorkflowDecision[PendingTriageReview]):
    """Decision about what triage actions to take.

    This is the output of the workflow's decision logic.
    """

    @property
    def triage_to_launch(self) -> tuple[PendingTriageReview, ...]:
        """Alias for items_to_launch for backwards compatibility."""
        return self.items_to_launch


class TriageWorkflow:
    """Decides when pending triage reviews should be launched.

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

    def is_configured(self) -> bool:
        """Check if triage review is configured."""
        return self.config.triage_review_agent is not None

    def should_launch_triage(
        self,
        pending_triage: Sequence[PendingTriageReview],
        active_session_count: int,
        paused: bool,
        *,
        reserved_capacity: int | None = None,
    ) -> TriageDecision:
        """Determine if and which triage reviews should be launched.

        Args:
            pending_triage: Queue of pending triage reviews
            active_session_count: Number of active sessions
            paused: Whether the orchestrator is paused
            reserved_capacity: When None (default), triage shares the worker
                budget and the gate/available slots derive from
                ``max_concurrent_sessions - active_session_count``, exactly as
                before. When set, triage has its own reserved additive budget
                (``triage.max_concurrent - active_triage``): the gate and the
                slot count use it directly, so triage may launch even when the
                worker budget is saturated.

        Returns:
            TriageDecision describing what should happen
        """
        # Check if configured
        if not self.is_configured():
            return TriageDecision.skip("No triage_review_agent configured")

        # Check if queue is empty
        if not pending_triage:
            return TriageDecision.skip("No pending triage reviews")

        gate_skip = self._gate_skip_reason(
            active_session_count, paused, reserved_capacity=reserved_capacity
        )
        if gate_skip:
            return TriageDecision.skip(gate_skip)

        # Determine which triage reviews to launch
        available = (
            reserved_capacity
            if reserved_capacity is not None
            else self.config.max_concurrent_sessions - active_session_count
        )
        triage_to_launch = list(pending_triage)[:available]

        self.events.publish(
            make_trace_event(
                EventName.TRIAGE_LAUNCHING,
                {
                    "count": len(triage_to_launch),
                    "capacity": available,
                    "pending": len(pending_triage),
                },
            )
        )

        return TriageDecision.launch(triage_to_launch, available)

    def should_create_health_review(
        self,
        active_session_count: int,
        paused: bool,
    ) -> bool:
        """Gate the periodic health-review anchor creation (ADR-0031 §4).

        Same owned paused/capacity gate as launch decisions: when the
        orchestrator is paused or at capacity the anchor is NOT created —
        due-ness persists, so creation retries once the gate opens — and
        TRIAGE_SKIPPED is emitted with the proper reason (#6763).
        """
        if not self.is_configured():
            return False
        return self._gate_skip_reason(active_session_count, paused) is None

    def _gate_skip_reason(
        self,
        active_session_count: int,
        paused: bool,
        *,
        reserved_capacity: int | None = None,
    ) -> str | None:
        """Owned paused/capacity gate shared by launch and creation decisions.

        Emits TRIAGE_SKIPPED with the rejection reason; returns None when
        triage work may proceed. When ``reserved_capacity`` is set the gate
        checks the reserved additive triage budget instead of the shared
        worker budget, so a saturated worker budget no longer blocks the tech
        lead; when None (default) the shared-budget behavior is unchanged.
        """
        if paused:
            self.events.publish(
                make_trace_event(
                    EventName.TRIAGE_SKIPPED,
                    {"reason": "orchestrator_paused"},
                )
            )
            return "Orchestrator paused"

        if reserved_capacity is not None:
            if reserved_capacity <= 0:
                self.events.publish(
                    make_trace_event(
                        EventName.TRIAGE_SKIPPED,
                        {
                            "reason": "no_reserved_capacity",
                            "reserved_remaining": reserved_capacity,
                        },
                    )
                )
                return (
                    "No reserved triage capacity "
                    f"(remaining={reserved_capacity})"
                )
            return None

        max_sessions = self.config.max_concurrent_sessions
        if max_sessions - active_session_count <= 0:
            self.events.publish(
                make_trace_event(
                    EventName.TRIAGE_SKIPPED,
                    {
                        "reason": "no_capacity",
                        "active": active_session_count,
                        "max": max_sessions,
                    },
                )
            )
            return f"No capacity (active={active_session_count}, max={max_sessions})"
        return None
