"""TechLeadWorkflow - tech_lead review launch policy.

This module encapsulates the decision logic for launching tech_lead review
sessions from the pending-tech-lead queue.

The actual batch trigger (deciding WHEN a tech_lead review should be created)
lives in the fact-gathering/planning path:
`fact_gatherer.gather_tech_lead_facts` -> `planner._plan_tech_lead_issue_creation`.

Usage:
    workflow = TechLeadWorkflow(config=config, events=event_sink)
    decision = workflow.should_launch_tech_lead(pending_tech_lead, active_sessions, paused)
    if decision.should_launch:
        for tech_lead in decision.tech_lead_to_launch:
            # Launch the tech_lead session
"""

from dataclasses import dataclass
from typing import Sequence

from ...infra.config import Config
from ...events import EventName
from ...domain.models import PendingTechLeadReview
from ...ports import EventSink,  make_trace_event
from .decision_base import WorkflowDecision


@dataclass(frozen=True)
class TechLeadDecision(WorkflowDecision[PendingTechLeadReview]):
    """Decision about what tech_lead actions to take.

    This is the output of the workflow's decision logic.
    """

    @property
    def tech_lead_to_launch(self) -> tuple[PendingTechLeadReview, ...]:
        """Alias for items_to_launch for backwards compatibility."""
        return self.items_to_launch


class TechLeadWorkflow:
    """Decides when pending tech_lead reviews should be launched.

    It contains POLICY (what should happen), not MECHANICS.
    """

    def __init__(self, config: Config, events: EventSink):
        """Initialize the workflow.

        Args:
            config: Configuration with tech_lead settings
            events: EventSink for trace events
        """
        self.config = config
        self.events = events

    def is_configured(self) -> bool:
        """Check if tech_lead review is configured."""
        return self.config.tech_lead_review_agent is not None

    def should_launch_tech_lead(
        self,
        pending_tech_lead: Sequence[PendingTechLeadReview],
        active_session_count: int,
        paused: bool,
        *,
        reserved_capacity: int | None = None,
    ) -> TechLeadDecision:
        """Determine if and which tech_lead reviews should be launched.

        Args:
            pending_tech_lead: Queue of pending tech_lead reviews
            active_session_count: Number of active sessions
            paused: Whether the orchestrator is paused
            reserved_capacity: When None (default), tech_lead shares the worker
                budget and the gate/available slots derive from
                ``max_concurrent_sessions - active_session_count``, exactly as
                before. When set, tech_lead has its own reserved additive budget
                (``tech_lead.max_concurrent - active_tech_lead``): the gate and the
                slot count use it directly, so tech_lead may launch even when the
                worker budget is saturated.

        Returns:
            TechLeadDecision describing what should happen
        """
        # Check if configured
        if not self.is_configured():
            return TechLeadDecision.skip("No tech_lead_review_agent configured")

        # Check if queue is empty
        if not pending_tech_lead:
            return TechLeadDecision.skip("No pending tech_lead reviews")

        gate_skip = self._gate_skip_reason(
            active_session_count, paused, reserved_capacity=reserved_capacity
        )
        if gate_skip:
            return TechLeadDecision.skip(gate_skip)

        # Determine which tech_lead reviews to launch
        available = (
            reserved_capacity
            if reserved_capacity is not None
            else self.config.max_concurrent_sessions - active_session_count
        )
        tech_lead_to_launch = list(pending_tech_lead)[:available]

        self.events.publish(
            make_trace_event(
                EventName.TECH_LEAD_LAUNCHING,
                {
                    "count": len(tech_lead_to_launch),
                    "capacity": available,
                    "pending": len(pending_tech_lead),
                },
            )
        )

        return TechLeadDecision.launch(tech_lead_to_launch, available)

    def should_create_health_review(
        self,
        active_session_count: int,
        paused: bool,
    ) -> bool:
        """Gate the periodic health-review anchor creation (ADR-0031 §4).

        Same owned paused/capacity gate as launch decisions: when the
        orchestrator is paused or at capacity the anchor is NOT created —
        due-ness persists, so creation retries once the gate opens — and
        TECH_LEAD_SKIPPED is emitted with the proper reason (#6763).
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

        Emits TECH_LEAD_SKIPPED with the rejection reason; returns None when
        tech_lead work may proceed. When ``reserved_capacity`` is set the gate
        checks the reserved additive tech_lead budget instead of the shared
        worker budget, so a saturated worker budget no longer blocks the tech
        lead; when None (default) the shared-budget behavior is unchanged.
        """
        if paused:
            self.events.publish(
                make_trace_event(
                    EventName.TECH_LEAD_SKIPPED,
                    {"reason": "orchestrator_paused"},
                )
            )
            return "Orchestrator paused"

        if reserved_capacity is not None:
            if reserved_capacity <= 0:
                self.events.publish(
                    make_trace_event(
                        EventName.TECH_LEAD_SKIPPED,
                        {
                            "reason": "no_reserved_capacity",
                            "reserved_remaining": reserved_capacity,
                        },
                    )
                )
                return (
                    "No reserved tech_lead capacity "
                    f"(remaining={reserved_capacity})"
                )
            return None

        max_sessions = self.config.max_concurrent_sessions
        if max_sessions - active_session_count <= 0:
            self.events.publish(
                make_trace_event(
                    EventName.TECH_LEAD_SKIPPED,
                    {
                        "reason": "no_capacity",
                        "active": active_session_count,
                        "max": max_sessions,
                    },
                )
            )
            return f"No capacity (active={active_session_count}, max={max_sessions})"
        return None
