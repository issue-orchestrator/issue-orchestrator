"""Planner - pure policy decisions.

The planner answers "should we?" questions without side effects.
It takes an immutable snapshot of state and returns a Plan describing
what actions should be taken.

This separation from the orchestrator enables:
- Pure, fast tests (no mocks for tmux/GitHub)
- Explainability ("why didn't issue X run?")
- Reuse across execution strategies (tmux, cloud)

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

from ..infra.config import Config
from ..ports.issue import Issue
from ..domain.models import (
    Session,
    PendingReview,
    PendingRework,
    PendingTriageReview,
    DiscoveredReview,
    DiscoveredRework,
    DiscoveredEscalation,
    DiscoveredFailure,
    TriageFacts,
    CleanupFacts,
)

if TYPE_CHECKING:
    from ..domain.models import OrchestratorState
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
    AddLabelAction,
    RemoveLabelAction,
    LaunchSessionAction,
    QueueReviewAction,
    QueueReworkAction,
    QueueTriageAction,
    CreateTriageIssueAction,
    EscalateToHumanAction,
    CleanupSessionAction,
)
from .reconciliation import build_expected_for_mutation
from ..infra import labels

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
    # Discovered facts for Planner-centric queue management
    discovered_reviews: tuple[DiscoveredReview, ...] = field(default_factory=tuple)
    discovered_reworks: tuple[DiscoveredRework, ...] = field(default_factory=tuple)
    discovered_escalations: tuple[DiscoveredEscalation, ...] = field(default_factory=tuple)
    discovered_failures: tuple[DiscoveredFailure, ...] = field(default_factory=tuple)
    triage_facts: Optional[TriageFacts] = None
    cleanup_facts: Optional[CleanupFacts] = None
    # Issues with stale in-progress labels (label present but no active session)
    stale_in_progress_issues: tuple[Issue, ...] = field(default_factory=tuple)
    # Issues that failed this cycle - skip until cache refresh (prevents immediate retry)
    failed_this_cycle: frozenset[int] = field(default_factory=frozenset)

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
        discovered_reviews: Sequence[DiscoveredReview] = (),
        discovered_reworks: Sequence[DiscoveredRework] = (),
        discovered_escalations: Sequence[DiscoveredEscalation] = (),
        discovered_failures: Sequence[DiscoveredFailure] = (),
        triage_facts: Optional[TriageFacts] = None,
        cleanup_facts: Optional[CleanupFacts] = None,
        stale_in_progress_issues: Sequence[Issue] = (),
    ) -> "OrchestratorSnapshot":
        """Create snapshot from mutable state.

        Args:
            issues: Current list of issues from GitHub
            state: Mutable orchestrator state object
            max_issues_to_start: Optional limit on issues to start this session
            discovered_reviews: Reviews discovered from session completions/scans
            discovered_reworks: Reworks discovered from scans
            discovered_escalations: Escalations discovered from scans
            discovered_failures: Failures discovered from session completions (for triage)
            triage_facts: Facts about triage trigger conditions
            cleanup_facts: Facts about pending cleanups and their review status
            stale_in_progress_issues: Issues with stale in-progress labels
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
            discovered_reviews=tuple(discovered_reviews),
            discovered_reworks=tuple(discovered_reworks),
            discovered_escalations=tuple(discovered_escalations),
            discovered_failures=tuple(discovered_failures),
            triage_facts=triage_facts,
            cleanup_facts=cleanup_facts,
            stale_in_progress_issues=tuple(stale_in_progress_issues),
            failed_this_cycle=frozenset(state.failed_this_cycle),
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

        # === PHASE 1: Queue population actions (don't consume capacity) ===

        # 1a. Clean up stale in-progress labels (no session running)
        stale_cleanup_actions = self._plan_stale_cleanup(snapshot)
        actions.extend(stale_cleanup_actions)

        # 1b. Queue discovered reviews from session completions/scans
        queue_actions = self._plan_discovered_reviews(snapshot)
        actions.extend(queue_actions)

        # 1c. Queue discovered reworks from scans
        rework_queue_actions = self._plan_discovered_reworks(snapshot)
        actions.extend(rework_queue_actions)

        # 1d. Handle escalations (PRs exceeding max rework cycles)
        escalation_actions = self._plan_discovered_escalations(snapshot)
        actions.extend(escalation_actions)

        # 1e. Queue triage reviews for session failures
        failure_triage_actions = self._plan_discovered_failures(snapshot)
        actions.extend(failure_triage_actions)

        # 1f. Create triage issue if threshold met
        triage_create_action = self._plan_triage_issue_creation(snapshot)
        if triage_create_action:
            actions.append(triage_create_action)

        # 1g. Process cleanups for reviewed PRs
        cleanup_actions = self._plan_cleanups(snapshot)
        actions.extend(cleanup_actions)

        # === PHASE 2: Session launch actions (consume capacity) ===

        # Calculate available capacity
        capacity = self.config.max_concurrent_sessions - snapshot.active_count
        if capacity <= 0:
            logger.debug("Planner: no capacity available (active=%d, max=%d)",
                        snapshot.active_count, self.config.max_concurrent_sessions)
            # Still return queue actions even if no capacity for launches
            return Plan(actions=tuple(actions), skipped=tuple(skipped))

        # PRIORITY ORDER: Reviews > Reworks > Triage > New Issues
        # This ensures completed work (PRs) gets reviewed before starting new work

        # 2. Plan review launches (highest priority)
        if capacity > 0 and self.review_workflow:
            review_actions, review_skipped = self._plan_reviews(snapshot, capacity)
            actions.extend(review_actions)
            skipped.extend(review_skipped)
            capacity -= len(review_actions)

        # 3. Plan rework launches
        if capacity > 0 and self.rework_workflow:
            rework_actions, rework_skipped = self._plan_reworks(snapshot, capacity)
            actions.extend(rework_actions)
            skipped.extend(rework_skipped)
            capacity -= len(rework_actions)

        # 4. Plan triage launches
        if capacity > 0 and self.triage_workflow:
            triage_actions, triage_skipped = self._plan_triage(snapshot, capacity)
            actions.extend(triage_actions)
            skipped.extend(triage_skipped)
            capacity -= len(triage_actions)

        # 5. Plan issue launches (only if no reviews/reworks/triage pending)
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

    def _plan_discovered_reviews(self, snapshot: OrchestratorSnapshot) -> list[Action]:
        """Plan queue actions for discovered reviews from session completions.

        Returns:
            List of QueueReviewAction for reviews not already queued
        """
        actions: list[Action] = []

        if not snapshot.discovered_reviews:
            return actions

        # Get already-queued PR numbers
        queued_pr_numbers = {r.pr_number for r in snapshot.pending_reviews}

        for review in snapshot.discovered_reviews:
            if review.pr_number not in queued_pr_numbers:
                # Add pr-pending label to prevent issue re-pickup while awaiting merge
                actions.append(AddLabelAction(
                    issue_number=review.issue_number,
                    label=labels.PR_PENDING,
                    reason=f"session completed with PR #{review.pr_number} - awaiting merge",
                    expected=build_expected_for_mutation(),
                ))
                # Only queue review if code review agent is configured
                if self.config.code_review_agent:
                    actions.append(QueueReviewAction(
                        issue_number=review.issue_number,
                        pr_number=review.pr_number,
                        pr_url=review.pr_url,
                        branch_name=review.branch_name,
                        code_review_label=self.config.code_review_label or "",
                        agent_label=review.agent_label,
                        reason=f"session completed with PR #{review.pr_number}",
                        expected=build_expected_for_mutation(),
                    ))
                    logger.debug("Planner: queuing review for PR #%d", review.pr_number)
                else:
                    logger.debug("Planner: no code_review_agent - skipping review queue for PR #%d", review.pr_number)
            else:
                logger.debug("Planner: PR #%d already queued, skipping", review.pr_number)

        return actions

    def _plan_triage_issue_creation(self, snapshot: OrchestratorSnapshot) -> Optional[CreateTriageIssueAction]:
        """Plan triage issue creation if threshold is met.

        Returns:
            CreateTriageIssueAction if threshold met and no existing issue, else None
        """
        if not snapshot.triage_facts:
            return None

        facts = snapshot.triage_facts
        if facts.pr_count < facts.threshold:
            logger.debug("Planner: triage threshold not met (%d/%d)",
                        facts.pr_count, facts.threshold)
            return None

        if facts.existing_triage_issue:
            logger.debug("Planner: triage issue #%d already exists",
                        facts.existing_triage_issue)
            return None

        # Build issue body from PR list
        pr_list = "\n".join(f"- PR #{pr[0]}: {pr[1]}" for pr in facts.prs)
        body = f"""## Triage Batch Review Triggered

{facts.pr_count} PRs have passed code review and are ready for triage review:

{pr_list}

Review these PRs for patterns, architectural concerns, and process improvements.
Flip labels from `{facts.watch_label}` to `{self.config.triage_reviewed_label}` after review.
"""
        title = f"Triage Batch Review: {facts.pr_count} PRs pending"
        labels = (self.config.triage_review_agent,) if self.config.triage_review_agent else ()

        logger.info("Planner: creating triage issue for %d PRs", facts.pr_count)
        return CreateTriageIssueAction(
            title=title,
            body=body,
            labels=labels,
            pr_count=facts.pr_count,
            reason=f"threshold met: {facts.pr_count} >= {facts.threshold}",
        )

    def _plan_discovered_reworks(self, snapshot: OrchestratorSnapshot) -> list[Action]:
        """Plan queue actions for discovered reworks from scans.

        Returns:
            List of QueueReworkAction for reworks not already queued
        """
        actions: list[Action] = []

        if not snapshot.discovered_reworks:
            return actions

        # Get already-queued issue numbers (using stable_id for IssueKey comparison)
        queued_issue_ids = {int(r.issue_key.stable_id()) for r in snapshot.pending_reworks}

        for rework in snapshot.discovered_reworks:
            if rework.issue_number not in queued_issue_ids:
                actions.append(QueueReworkAction(
                    issue_number=rework.issue_number,
                    pr_number=rework.pr_number,
                    pr_url="",  # Not tracked in DiscoveredRework
                    branch_name=rework.branch_name,
                    rework_cycle=rework.rework_cycle,
                    reason=f"scan found PR needing rework (cycle {rework.rework_cycle})",
                ))
                logger.debug("Planner: queuing rework for issue #%d (cycle %d)",
                            rework.issue_number, rework.rework_cycle)
            else:
                logger.debug("Planner: issue #%d already queued for rework, skipping",
                            rework.issue_number)

        return actions

    def _plan_discovered_escalations(self, snapshot: OrchestratorSnapshot) -> list[Action]:
        """Plan escalation actions for PRs exceeding max rework cycles.

        Returns:
            List of EscalateToHumanAction for escalations
        """
        actions: list[Action] = []

        if not snapshot.discovered_escalations:
            return actions

        for escalation in snapshot.discovered_escalations:
            actions.append(EscalateToHumanAction(
                issue_number=escalation.issue_number,
                pr_number=escalation.pr_number,
                escalation_reason="max rework cycles exceeded",
                rework_cycles=escalation.rework_cycle - 1,  # Completed cycles, not current
                needs_human_label=self.config.get_label_needs_human(),
                needs_rework_label=self.config.get_label_needs_rework(),
                max_rework_cycles=self.config.max_rework_cycles,
                reason=f"PR #{escalation.pr_number} exceeded max rework cycles ({escalation.rework_cycle - 1})",
                expected=build_expected_for_mutation(),
            ))
            logger.info("Planner: escalating PR #%d after %d rework cycles",
                       escalation.pr_number, escalation.rework_cycle - 1)

        return actions

    def _plan_discovered_failures(self, snapshot: OrchestratorSnapshot) -> list[Action]:
        """Plan triage actions for session failures.

        When a session fails or times out, the Planner decides whether to
        queue a triage review based on:
        - triage_review_on_failure config setting
        - triage_review_agent being configured
        - not already queued for this issue

        Returns:
            List of QueueTriageAction for failures to investigate
        """
        actions: list[Action] = []

        if not snapshot.discovered_failures:
            return actions

        # Check if triage-on-failure is enabled
        if not self.config.triage_review_on_failure:
            logger.debug("Planner: triage_review_on_failure disabled, skipping %d failures",
                        len(snapshot.discovered_failures))
            return actions

        # Check if triage agent is configured
        if not self.config.triage_review_agent:
            logger.debug("Planner: no triage_review_agent configured, skipping failures")
            return actions

        # Get issue numbers already queued for triage
        already_queued = {t.issue_number for t in snapshot.pending_triage}

        for failure in snapshot.discovered_failures:
            if failure.issue_number in already_queued:
                logger.debug("Planner: issue #%d already queued for triage, skipping",
                           failure.issue_number)
                continue

            actions.append(QueueTriageAction(
                issue_number=failure.issue_number,
                title=f"Investigate: {failure.issue_title} ({failure.failure_reason})",
                reason=f"Session failed with status '{failure.failure_reason}'",
            ))
            logger.info("Planner: queuing triage for failed issue #%d (%s)",
                       failure.issue_number, failure.failure_reason)

        return actions

    def _plan_cleanups(self, snapshot: OrchestratorSnapshot) -> list[Action]:
        """Plan cleanup actions for sessions with reviewed PRs.

        Returns:
            List of CleanupSessionAction for cleanups ready to process
        """
        actions: list[Action] = []

        if not snapshot.cleanup_facts:
            return actions

        facts = snapshot.cleanup_facts

        # For each pending cleanup, check if its PR is in the reviewed set
        for cleanup in facts.pending_cleanups:
            # cleanup is a tuple of (issue_number, pr_number, terminal_session_name, worktree_path)
            issue_number, pr_number, terminal_session_name, worktree_path = cleanup
            if pr_number in facts.reviewed_pr_numbers:
                actions.append(CleanupSessionAction(
                    issue_number=issue_number,
                    pr_number=pr_number,
                    terminal_session_name=terminal_session_name,
                    worktree_path=worktree_path,
                    close_tabs=facts.close_tabs,
                    remove_worktrees=facts.remove_worktrees,
                    reason=f"PR #{pr_number} has been reviewed",
                ))
                logger.info("Planner: cleanup for issue #%d (PR #%d reviewed)",
                           issue_number, pr_number)

        return actions

    def _plan_stale_cleanup(self, snapshot: OrchestratorSnapshot) -> list[Action]:
        """Plan cleanup actions for issues with stale in-progress labels.

        When an issue has the in-progress label but no active session exists,
        the label is stale and should be removed. This allows the issue to be
        retried or processed normally.

        Returns:
            List of RemoveLabelAction for stale in-progress labels
        """
        actions: list[Action] = []

        if not snapshot.stale_in_progress_issues:
            return actions

        for issue in snapshot.stale_in_progress_issues:
            actions.append(RemoveLabelAction(
                issue_number=issue.number,
                label=labels.IN_PROGRESS,
                reason="stale - no running session",
                expected=build_expected_for_mutation(),
            ))
            logger.info("Planner: removing stale in-progress label from issue #%d",
                       issue.number)

        return actions

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

        # Get available issues (not blocked, not in-progress with running session)
        available, dependency_blocked = self.scheduler.get_available_issues(
            list(snapshot.issues),
            check_dependencies=self.dependency_evaluator is not None,
            active_sessions=list(snapshot.active_sessions),
        )

        # Record dependency-blocked items and add cross-milestone labels
        for issue, reason in dependency_blocked:
            skipped.append(SkippedItem(
                item_type="issue",
                number=issue.number,
                reason=f"dependency: {reason}",
            ))
            # Add cross-milestone label if this is a milestone scope violation
            if "cross-milestone" in reason.lower():
                actions.append(AddLabelAction(
                    issue_number=issue.number,
                    label=labels.BLOCKED_CROSS_MILESTONE,
                    reason=f"dependency violates milestone scope: {reason}",
                    expected=self._build_expected_for_mutation(
                        issue.number, snapshot, reason="add cross-milestone label"
                    ),
                ))

        # Filter out issues already being worked on, just completed, or failed this cycle
        issues_with_pending_reviews = {r.issue_number for r in snapshot.discovered_reviews}
        issues_with_pending_reworks = {r.issue_number for r in snapshot.discovered_reworks}
        excluded_issues = (
            snapshot.active_issue_numbers |
            issues_with_pending_reviews |
            issues_with_pending_reworks |
            snapshot.failed_this_cycle  # Skip issues that failed until cache refresh
        )
        not_active = [
            issue for issue in available
            if issue.number not in excluded_issues
        ]

        # Log if we're skipping issues due to recent failure
        skipped_due_to_failure = [i for i in available if i.number in snapshot.failed_this_cycle]
        for issue in skipped_due_to_failure:
            skipped.append(SkippedItem(
                item_type="issue",
                number=issue.number,
                reason="failed this cycle - waiting for cache refresh",
            ))
            logger.debug("Planner: skipping issue #%d - failed this cycle", issue.number)

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
                # Use issue_key's stable_id for identification
                issue_num = int(rework.issue_key.stable_id())
                skipped.append(SkippedItem(
                    item_type="rework",
                    number=issue_num,
                    reason=decision.skip_reason,
                ))
            return actions, skipped

        if decision.should_launch:
            for rework in decision.reworks_to_launch[:capacity]:
                issue_num = int(rework.issue_key.stable_id())
                # Check for escalation
                escalation = self.rework_workflow.should_escalate(rework.rework_cycle)
                if escalation.should_escalate:
                    actions.append(EscalateToHumanAction(
                        issue_number=issue_num,
                        pr_number=issue_num,  # PR resolved by adapter at execution time
                        escalation_reason=escalation.reason or "max rework cycles reached",
                        rework_cycles=rework.rework_cycle,
                        needs_human_label=self.config.get_label_needs_human(),
                        needs_rework_label=self.config.get_label_needs_rework(),
                        max_rework_cycles=self.config.max_rework_cycles,
                        reason=f"escalating: cycle {rework.rework_cycle} >= max {escalation.max_cycles}",
                        expected=build_expected_for_mutation(),
                    ))
                else:
                    actions.append(LaunchSessionAction(
                        session_type="rework",
                        number=issue_num,
                        command="",  # Orchestrator will fill in
                        working_dir="",  # Orchestrator will fill in
                        reason=f"rework cycle {rework.rework_cycle} for issue #{issue_num}",
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
            report = self.dependency_evaluator.evaluate(
                issue.number, issue.body, source_milestone=issue.milestone
            )
            if not report.runnable:
                return f"Blocked by dependencies: {report.summary()}"

        return "Unknown reason"
