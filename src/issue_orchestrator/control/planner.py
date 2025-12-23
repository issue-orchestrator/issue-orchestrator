"""Planner - pure policy decisions.

The planner answers "should we?" questions without side effects.
It takes an immutable snapshot of state and returns a Plan describing
what actions should be taken.

This separation from the orchestrator enables:
- Pure, fast tests (no mocks for tmux/GitHub)
- Explainability ("why didn't issue X run?")
- Reuse across execution strategies (tmux, iTerm, cloud)

Rule of thumb:
- "Should we?" → Planner (this module)
- "Can we?" → Orchestrator
- "How?" → Adapters

Usage:
    snapshot = orchestrator.create_snapshot()
    plan = planner.plan(snapshot)
    orchestrator.apply(plan)
"""

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional, Sequence

from ..config import Config
from ..models import (
    Issue,
    Session,
    PendingReview,
    PendingRework,
    PendingTriageReview,
)

if TYPE_CHECKING:
    from ..models import OrchestratorState
from .scheduler import Scheduler
from .dependency_evaluator import DependencyEvaluator
from .workflows import (
    ReviewWorkflow,
    ReviewDecision,
    ReworkWorkflow,
    ReworkDecision,
    TriageWorkflow,
    TriageDecision,
)
from .actions import (
    Action,
    ActionType,
    LaunchSessionAction,
    QueueReviewAction,
    QueueReworkAction,
    QueueTriageAction,
    EscalateToHumanAction,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class OrchestratorSnapshot:
    """Immutable snapshot of orchestrator state for planning.

    This is the input to the planner - a point-in-time view of all
    relevant state needed to make planning decisions.
    """

    issues: tuple[Issue, ...]
    active_sessions: tuple[Session, ...]
    pending_reviews: tuple[PendingReview, ...]
    pending_reworks: tuple[PendingRework, ...]
    pending_triage: tuple[PendingTriageReview, ...]
    paused: bool
    priority_queue: tuple[int, ...] = field(default_factory=tuple)
    issues_started_count: int = 0
    max_issues_to_start: Optional[int] = None

    @property
    def active_count(self) -> int:
        """Number of currently active sessions."""
        return len(self.active_sessions)

    @property
    def active_issue_numbers(self) -> frozenset[int]:
        """Issue numbers with active sessions."""
        return frozenset(s.issue.number for s in self.active_sessions)

    @classmethod
    def from_state(
        cls,
        issues: Sequence[Issue],
        state: "OrchestratorState",  # Forward reference to avoid circular import
        max_issues_to_start: Optional[int] = None,
    ) -> "OrchestratorSnapshot":
        """Create snapshot from mutable state.

        Args:
            issues: Current list of issues from GitHub
            state: Mutable orchestrator state object
            max_issues_to_start: Optional limit on issues to start this session
        """
        return cls(
            issues=tuple(issues),
            active_sessions=tuple(state.active_sessions),
            pending_reviews=tuple(state.pending_reviews),
            pending_reworks=tuple(state.pending_reworks),
            pending_triage=tuple(state.pending_triage_reviews),
            paused=state.paused,
            priority_queue=tuple(state.priority_queue),
            issues_started_count=state.issues_started_count,
            max_issues_to_start=max_issues_to_start,
        )


@dataclass(frozen=True)
class SkippedItem:
    """An item that was considered but not acted upon."""

    item_type: str  # "issue", "review", "rework", "triage"
    number: int
    reason: str


@dataclass(frozen=True)
class Plan:
    """Complete plan for one planning cycle.

    A Plan is the output of the planner - it describes what actions
    should be taken, plus explanations for items that were skipped.
    """

    actions: tuple[Action, ...]
    skipped: tuple[SkippedItem, ...]

    @classmethod
    def empty(cls) -> "Plan":
        """Create an empty plan (no actions)."""
        return cls(actions=(), skipped=())

    @property
    def action_count(self) -> int:
        """Number of actions in the plan."""
        return len(self.actions)

    def actions_of_type(self, action_type: ActionType) -> list[Action]:
        """Get all actions of a specific type."""
        return [a for a in self.actions if a.action_type == action_type]

    def has_action_for(self, number: int) -> bool:
        """Check if plan has any action for a given issue/PR number."""
        for action in self.actions:
            # Use getattr with default to safely check for number attributes
            if getattr(action, "number", None) == number:
                return True
            if getattr(action, "issue_number", None) == number:
                return True
            if getattr(action, "pr_number", None) == number:
                return True
        return False


class Planner:
    """Pure policy decisions - no side effects.

    The planner takes a snapshot of current state and returns a Plan
    describing what actions should be taken. It delegates to:
    - Scheduler for issue prioritization and availability
    - DependencyEvaluator for dependency checking
    - Workflow classes for review/rework/triage decisions

    The planner does NOT:
    - Make API calls
    - Mutate state
    - Start sessions
    - Emit events (beyond planning trace)
    """

    def __init__(
        self,
        config: Config,
        scheduler: Scheduler,
        dependency_evaluator: Optional[DependencyEvaluator] = None,
        review_workflow: Optional[ReviewWorkflow] = None,
        rework_workflow: Optional[ReworkWorkflow] = None,
        triage_workflow: Optional[TriageWorkflow] = None,
    ):
        """Initialize planner with its dependencies.

        Args:
            config: Application configuration
            scheduler: Issue prioritization and availability logic
            dependency_evaluator: Optional dependency checking
            review_workflow: Optional review decision logic
            rework_workflow: Optional rework decision logic
            triage_workflow: Optional triage decision logic
        """
        self.config = config
        self.scheduler = scheduler
        self.dependency_evaluator = dependency_evaluator
        self.review_workflow = review_workflow
        self.rework_workflow = rework_workflow
        self.triage_workflow = triage_workflow

    def plan(self, snapshot: OrchestratorSnapshot) -> Plan:
        """Create a plan for the current state.

        This is the main entry point. Given a snapshot of current state,
        determine what actions should be taken.

        Args:
            snapshot: Immutable view of current orchestrator state

        Returns:
            Plan with actions to execute and explanations for skipped items
        """
        actions: list[Action] = []
        skipped: list[SkippedItem] = []

        # Check if paused
        if snapshot.paused:
            logger.debug("Planner: orchestrator is paused, returning empty plan")
            return Plan.empty()

        # Calculate available capacity
        capacity = self.config.max_concurrent_sessions - snapshot.active_count
        if capacity <= 0:
            logger.debug("Planner: no capacity available (active=%d, max=%d)",
                        snapshot.active_count, self.config.max_concurrent_sessions)
            return Plan.empty()

        # PRIORITY ORDER: Reviews > Reworks > Triage > New Issues
        # This ensures completed work (PRs) gets reviewed before starting new work

        # 1. Plan review launches (highest priority)
        if capacity > 0 and self.review_workflow:
            review_actions, review_skipped = self._plan_reviews(snapshot, capacity)
            actions.extend(review_actions)
            skipped.extend(review_skipped)
            capacity -= len(review_actions)

        # 2. Plan rework launches
        if capacity > 0 and self.rework_workflow:
            rework_actions, rework_skipped = self._plan_reworks(snapshot, capacity)
            actions.extend(rework_actions)
            skipped.extend(rework_skipped)
            capacity -= len(rework_actions)

        # 3. Plan triage launches
        if capacity > 0 and self.triage_workflow:
            triage_actions, triage_skipped = self._plan_triage(snapshot, capacity)
            actions.extend(triage_actions)
            skipped.extend(triage_skipped)
            capacity -= len(triage_actions)

        # 4. Plan issue launches (only if no reviews/reworks/triage pending)
        # This ensures we don't start new work when there's existing work to review
        has_pending_work = (
            len(snapshot.pending_reviews) > 0 or
            len(snapshot.pending_reworks) > 0 or
            len(snapshot.pending_triage) > 0
        )
        if capacity > 0 and not has_pending_work:
            issue_actions, issue_skipped, _ = self._plan_issues(snapshot, capacity)
            actions.extend(issue_actions)
            skipped.extend(issue_skipped)
        elif has_pending_work:
            logger.debug("Planner: skipping new issues - pending work exists "
                        "(reviews=%d, reworks=%d, triage=%d)",
                        len(snapshot.pending_reviews),
                        len(snapshot.pending_reworks),
                        len(snapshot.pending_triage))

        return Plan(actions=tuple(actions), skipped=tuple(skipped))

    def _plan_issues(
        self,
        snapshot: OrchestratorSnapshot,
        capacity: int,
    ) -> tuple[list[Action], list[SkippedItem], int]:
        """Plan which issues to launch.

        Returns:
            Tuple of (actions, skipped_items, capacity_used)
        """
        actions: list[Action] = []
        skipped: list[SkippedItem] = []

        # Check max_issues_to_start limit
        if snapshot.max_issues_to_start is not None:
            remaining = snapshot.max_issues_to_start - snapshot.issues_started_count
            if remaining <= 0:
                logger.debug("Planner: max_issues_to_start reached (%d)",
                            snapshot.max_issues_to_start)
                return actions, skipped, 0
            capacity = min(capacity, remaining)

        # Get available issues (not blocked, not in-progress)
        available, dependency_blocked = self.scheduler.get_available_issues(
            list(snapshot.issues),
            check_dependencies=self.dependency_evaluator is not None,
        )

        # Record dependency-blocked items
        for issue, reason in dependency_blocked:
            skipped.append(SkippedItem(
                item_type="issue",
                number=issue.number,
                reason=f"dependency: {reason}",
            ))

        # Filter out issues already being worked on
        not_active = [
            issue for issue in available
            if issue.number not in snapshot.active_issue_numbers
        ]

        # Pick next batch based on priority
        to_launch = self.scheduler.pick_next_batch(
            available=not_active,
            current_count=snapshot.active_count,
            priority_overrides=list(snapshot.priority_queue),
        )

        # Create launch actions
        for issue in to_launch[:capacity]:
            actions.append(LaunchSessionAction(
                session_type="issue",
                number=issue.number,
                command="",  # Orchestrator will fill in
                working_dir="",  # Orchestrator will fill in
                reason=f"scheduled: priority={self._get_priority_reason(issue)}",
            ))

        return actions, skipped, len(actions)

    def _plan_reviews(
        self,
        snapshot: OrchestratorSnapshot,
        capacity: int,
    ) -> tuple[list[Action], list[SkippedItem]]:
        """Plan which reviews to launch."""
        actions: list[Action] = []
        skipped: list[SkippedItem] = []

        if not self.review_workflow or not self.review_workflow.is_configured():
            return actions, skipped

        decision: ReviewDecision = self.review_workflow.should_launch_reviews(
            pending_reviews=list(snapshot.pending_reviews),
            active_session_count=snapshot.active_count,
            paused=snapshot.paused,
        )

        if decision.skip_reason:
            for review in snapshot.pending_reviews:
                skipped.append(SkippedItem(
                    item_type="review",
                    number=review.pr_number,
                    reason=decision.skip_reason,
                ))
            return actions, skipped

        if decision.should_launch:
            for review in decision.reviews_to_launch[:capacity]:
                actions.append(LaunchSessionAction(
                    session_type="review",
                    number=review.pr_number,
                    command="",  # Orchestrator will fill in
                    working_dir="",  # Orchestrator will fill in
                    reason=f"review queued for PR #{review.pr_number}",
                ))

        return actions, skipped

    def _plan_reworks(
        self,
        snapshot: OrchestratorSnapshot,
        capacity: int,
    ) -> tuple[list[Action], list[SkippedItem]]:
        """Plan which reworks to launch."""
        actions: list[Action] = []
        skipped: list[SkippedItem] = []

        if not self.rework_workflow:
            return actions, skipped

        decision: ReworkDecision = self.rework_workflow.should_launch_reworks(
            pending_reworks=list(snapshot.pending_reworks),
            active_session_count=snapshot.active_count,
            paused=snapshot.paused,
        )

        if decision.skip_reason:
            for rework in snapshot.pending_reworks:
                skipped.append(SkippedItem(
                    item_type="rework",
                    number=rework.pr_number,
                    reason=decision.skip_reason,
                ))
            return actions, skipped

        if decision.should_launch:
            for rework in decision.reworks_to_launch[:capacity]:
                # Check for escalation
                escalation = self.rework_workflow.should_escalate(rework.rework_cycle)
                if escalation.should_escalate:
                    actions.append(EscalateToHumanAction(
                        issue_number=rework.issue_number,
                        pr_number=rework.pr_number,
                        escalation_reason=escalation.reason or "max rework cycles reached",
                        rework_cycles=rework.rework_cycle,
                        reason=f"escalating: cycle {rework.rework_cycle} >= max {escalation.max_cycles}",
                    ))
                else:
                    actions.append(LaunchSessionAction(
                        session_type="rework",
                        number=rework.pr_number,
                        command="",  # Orchestrator will fill in
                        working_dir="",  # Orchestrator will fill in
                        reason=f"rework cycle {rework.rework_cycle} for PR #{rework.pr_number}",
                    ))

        return actions, skipped

    def _plan_triage(
        self,
        snapshot: OrchestratorSnapshot,
        capacity: int,
    ) -> tuple[list[Action], list[SkippedItem]]:
        """Plan which triage reviews to launch."""
        actions: list[Action] = []
        skipped: list[SkippedItem] = []

        if not self.triage_workflow or not self.triage_workflow.is_configured():
            return actions, skipped

        decision: TriageDecision = self.triage_workflow.should_launch_triage(
            pending_triage=list(snapshot.pending_triage),
            active_session_count=snapshot.active_count,
            paused=snapshot.paused,
        )

        if decision.skip_reason:
            for triage in snapshot.pending_triage:
                skipped.append(SkippedItem(
                    item_type="triage",
                    number=triage.issue_number,
                    reason=decision.skip_reason,
                ))
            return actions, skipped

        if decision.should_launch:
            for triage in decision.triage_to_launch[:capacity]:
                actions.append(LaunchSessionAction(
                    session_type="triage",
                    number=triage.issue_number,
                    command="",  # Orchestrator will fill in
                    working_dir="",  # Orchestrator will fill in
                    title=triage.title,
                    reason=f"triage review for #{triage.issue_number}",
                ))

        return actions, skipped

    def _get_priority_reason(self, issue: Issue) -> str:
        """Get a human-readable priority explanation for an issue."""
        parts = []
        if issue.milestone:
            parts.append(f"milestone={issue.milestone}")
        # Could extract priority from title [Px-nnn] pattern
        import re
        match = re.search(r"\[P(\d)-\d+\]", issue.title)
        if match:
            parts.append(f"P{match.group(1)}")
        if not parts:
            parts.append(f"issue #{issue.number}")
        return ", ".join(parts)

    def explain_skip(self, issue_number: int, snapshot: OrchestratorSnapshot) -> str:
        """Explain why an issue would be skipped.

        Useful for debugging and UI display.
        """
        # Check if already active
        if issue_number in snapshot.active_issue_numbers:
            return f"Issue #{issue_number} already has an active session"

        # Check if paused
        if snapshot.paused:
            return "Orchestrator is paused"

        # Check capacity
        if snapshot.active_count >= self.config.max_concurrent_sessions:
            return f"At capacity ({snapshot.active_count}/{self.config.max_concurrent_sessions})"

        # Check max_issues_to_start
        if snapshot.max_issues_to_start is not None:
            if snapshot.issues_started_count >= snapshot.max_issues_to_start:
                return f"max_issues_to_start limit reached ({snapshot.max_issues_to_start})"

        # Check dependencies
        issue = next((i for i in snapshot.issues if i.number == issue_number), None)
        if issue and self.dependency_evaluator and issue.body:
            report = self.dependency_evaluator.evaluate(issue.number, issue.body)
            if not report.runnable:
                return f"Blocked by dependencies: {report.summary()}"

        return "Unknown reason"
