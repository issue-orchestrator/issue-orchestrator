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
import re
import time
from typing import TYPE_CHECKING, Optional

from ..infra.config import Config
from ..infra.logging_config import issue_log
from ..ports.issue import Issue
from ..domain.models import (
    CompletionOutcome,
    TriageFacts,
    ObservedCompletion,
)

if TYPE_CHECKING:
    from .provider_resilience import ProviderResilienceManager
    from .label_manager import LabelManager
from .scheduler import Scheduler
from .dependency_evaluator import DependencyEvaluator
from .provider_availability import ProviderAvailabilityPolicy
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
    AddCommentAction,
    AddLabelAction,
    RemoveLabelAction,
    LaunchSessionAction,
    LaunchValidationRetryAction,
    QueueReviewAction,
    QueueReworkAction,
    QueueTriageAction,
    CreateTriageIssueAction,
    EscalateToHumanAction,
    CleanupSessionAction,
    ReconcileHistoryEntryAction,
    SessionType,
    SyncLabelsAction,
)
from .awaiting_merge_reconciler import (
    POST_PUBLISH_VALIDATION_SOURCE,
    build_post_publish_validation_comment,
)
from .reconciliation import build_expected_for_mutation
from .planner_types import OrchestratorSnapshot, Plan, PlanContext, SkippedItem

logger = logging.getLogger(__name__)


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
        provider_resilience: Optional["ProviderResilienceManager"] = None,
        label_manager: Optional["LabelManager"] = None,
    ):
        """Initialize planner with its dependencies.

        Args:
            config: Application configuration
            scheduler: Issue prioritization and availability logic
            dependency_evaluator: Optional dependency checking
            review_workflow: Optional review decision logic
            rework_workflow: Optional rework decision logic
            triage_workflow: Optional triage decision logic
            label_manager: Label registry for prefix-aware queries.
        """
        self.config = config
        self.scheduler = scheduler
        self.dependency_evaluator = self._align_dependency_evaluator(dependency_evaluator)
        self.review_workflow = review_workflow
        self.rework_workflow = rework_workflow
        self.triage_workflow = triage_workflow
        self.provider_resilience = provider_resilience
        self.provider_policy = ProviderAvailabilityPolicy(config, provider_resilience) if provider_resilience else None
        if label_manager is None:
            from .label_manager import LabelManager
            label_manager = LabelManager(config)
        self._lm = label_manager
        self._last_queue_decisions: dict[int, str] = {}
        self._last_queue_summary_logged_at: float = 0.0
        self._queue_summary_interval_seconds = 60.0

    def _align_dependency_evaluator(
        self,
        dependency_evaluator: Optional[DependencyEvaluator],
    ) -> Optional[DependencyEvaluator]:
        """Read scheduler dependency gating configuration and fail on drift."""
        scheduler_evaluator = self.scheduler.dependency_evaluator
        if dependency_evaluator is None:
            return scheduler_evaluator
        if scheduler_evaluator is None:
            raise ValueError("Scheduler dependency evaluator is required when Planner dependency evaluator is provided")
        if scheduler_evaluator is not dependency_evaluator:
            raise ValueError("Planner and Scheduler dependency evaluators must be the same instance")
        return dependency_evaluator

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

        plan_context = PlanContext(issue_labels_by_number={
            issue.number: tuple(issue.labels) for issue in snapshot.issues
        })

        # === PHASE 1: Queue population actions (don't consume capacity) ===

        # 1a. Clean up stale in-progress labels (no session running)
        stale_cleanup_actions = self._plan_stale_cleanup(snapshot)
        actions.extend(stale_cleanup_actions)

        # 1a-2. Clean up stale claims (io:claimed but claim expired)
        stale_claim_actions = self._plan_stale_claim_cleanup(snapshot)
        actions.extend(stale_claim_actions)

        # 1a-2b. Apply provider resilience labels (provider unavailable/available)
        provider_label_actions = self._plan_provider_resilience_labels(snapshot, plan_context)
        actions.extend(provider_label_actions)

        # 1a-3. Immediate label projection for observed completions (async processing)
        # This runs BEFORE discovered_reviews so labels are applied immediately
        completion_label_actions = self._plan_observed_completion_labels(snapshot, plan_context)
        actions.extend(completion_label_actions)

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

        # 1h. Reconcile recovered awaiting-merge history entries
        history_reconciliation_actions = self._plan_awaiting_merge_reconciliations(snapshot)
        actions.extend(history_reconciliation_actions)

        # === PHASE 2: Session launch actions (consume capacity) ===

        # Calculate available capacity
        capacity = self.config.max_concurrent_sessions - snapshot.active_count
        if capacity <= 0:
            logger.debug("Planner: no capacity available (active=%d, max=%d)",
                        snapshot.active_count, self.config.max_concurrent_sessions)
            # Still return queue actions even if no capacity for launches
            return Plan(actions=tuple(actions), skipped=tuple(skipped))

        # PRIORITY ORDER: Reviews > Reworks > Validation Retries > Triage > New Issues
        # This ensures completed work (PRs) gets reviewed before starting new work
        review_launch_count = 0
        rework_launch_count = 0
        validation_retry_launch_count = 0
        triage_launch_count = 0

        # 2. Plan review launches (highest priority)
        if capacity > 0 and self.review_workflow:
            review_actions, review_skipped = self._plan_reviews(snapshot, capacity, plan_context)
            actions.extend(review_actions)
            skipped.extend(review_skipped)
            capacity -= len(review_actions)
            review_launch_count = len(review_actions)

        # 3. Plan rework launches
        if capacity > 0 and self.rework_workflow:
            rework_actions, rework_skipped = self._plan_reworks(snapshot, capacity, plan_context)
            actions.extend(rework_actions)
            skipped.extend(rework_skipped)
            capacity -= len(rework_actions)
            rework_launch_count = len(rework_actions)

        # 4. Plan validation retry launches. These are continuations of
        # existing coding work and are not subject to max_issues_to_start.
        if capacity > 0:
            validation_retry_actions, validation_retry_skipped = self._plan_validation_retries(
                snapshot,
                capacity,
                plan_context,
            )
            actions.extend(validation_retry_actions)
            skipped.extend(validation_retry_skipped)
            capacity -= len(validation_retry_actions)
            validation_retry_launch_count = len(validation_retry_actions)

        # 5. Plan triage launches
        if capacity > 0 and self.triage_workflow:
            triage_actions, triage_skipped = self._plan_triage(snapshot, capacity, plan_context)
            actions.extend(triage_actions)
            skipped.extend(triage_skipped)
            capacity -= len(triage_actions)
            triage_launch_count = len(triage_actions)

        # 6. Plan issue launches with remaining capacity.
        #
        # Reviews/reworks/triage get priority (they consumed capacity above),
        # but any leftover capacity goes to new issues. We never starve issue
        # launches just because review/rework/triage actions were planned.
        if capacity > 0:
            pending_work_planned = (
                review_launch_count
                + rework_launch_count
                + validation_retry_launch_count
                + triage_launch_count
            )
            if pending_work_planned:
                logger.info(
                    "Planner: pending work consumed %d slot(s) "
                    "(reviews=%d, reworks=%d, validation_retries=%d, triage=%d), "
                    "%d slot(s) remain for issues",
                    pending_work_planned, review_launch_count,
                    rework_launch_count, validation_retry_launch_count,
                    triage_launch_count, capacity,
                )
            issue_actions, issue_skipped, _ = self._plan_issues(snapshot, capacity)
            actions.extend(issue_actions)
            skipped.extend(issue_skipped)

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
                ik = review.issue_key or str(review.issue_number)
                # Add pr-pending label to prevent issue re-pickup while awaiting merge
                actions.append(AddLabelAction(
                    issue_number=review.issue_number,
                    label=self._lm.pr_pending,
                    reason=f"session completed with PR #{review.pr_number} - awaiting merge",
                    expected=build_expected_for_mutation(),
                    issue_key=ik,
                ))
                # Skip review for dry-run PRs (fake PR numbers 90000-99999)
                is_dry_run_pr = 90000 <= review.pr_number <= 99999
                # Only queue review if code review agent is configured AND not dry-run
                if self.config.code_review_agent and not is_dry_run_pr:
                    actions.append(QueueReviewAction(
                        issue_number=review.issue_number,
                        pr_number=review.pr_number,
                        pr_url=review.pr_url,
                        branch_name=review.branch_name,
                        code_review_label=self.config.code_review_label or "",
                        agent_label=review.agent_label,
                        reason=f"session completed with PR #{review.pr_number}",
                        expected=build_expected_for_mutation(),
                        issue_key=ik,
                    ))
                    logger.debug("Planner: queuing review for PR #%d", review.pr_number)
                else:
                    logger.debug("Planner: no code_review_agent - skipping review queue for PR #%d", review.pr_number)
            else:
                logger.debug("Planner: PR #%d already queued, skipping", review.pr_number)

        return actions

    def _plan_awaiting_merge_reconciliations(
        self,
        snapshot: OrchestratorSnapshot,
    ) -> list[Action]:
        """Plan history status transitions discovered by awaiting-merge scans."""
        actions: list[Action] = []

        for reconciliation in snapshot.discovered_awaiting_merge_reconciliations:
            actions.append(ReconcileHistoryEntryAction(
                issue_number=reconciliation.issue_number,
                pr_number=reconciliation.pr_number,
                pr_url=reconciliation.pr_url,
                status=reconciliation.status,
                source=reconciliation.source,
                issue_key=reconciliation.issue_key or str(reconciliation.issue_number),
                reason=reconciliation.status_reason,
            ))

        for drift in snapshot.discovered_awaiting_merge_drifts:
            actions.append(SyncLabelsAction(
                issue_number=drift.issue_number,
                add_labels=(self._lm.blocked_pr_closed,),
                remove_labels=(self._lm.pr_pending,),
                issue_key=drift.issue_key or str(drift.issue_number),
                reason=drift.status_reason,
                expected=build_expected_for_mutation(
                    required={self._lm.pr_pending},
                ),
            ))

        return actions

    def _blocked_label_for_record(self, observed: ObservedCompletion) -> str:
        if observed.blocked_reason == "provider_unavailable":
            return self._lm.provider_unavailable
        return self._lm.blocked

    def _record_provider_skip(
        self,
        issue_number: int,
        item_type: str,
        item_number: int,
        provider: str,
        actions: list[Action],
        skipped: list[SkippedItem],
        plan_context: PlanContext,
    ) -> None:
        skipped.append(SkippedItem(
            item_type=item_type,
            number=item_number,
            reason=f"provider unavailable: {provider}",
        ))
        logger.info(issue_log(issue_number, "Skipped: reason=provider_unavailable provider=%s"), provider)
        if not self.provider_policy:
            return
        issue_labels = plan_context.issue_labels(issue_number)
        planned_labels = plan_context.planned_adds(issue_number)
        if self.provider_policy.should_add_blocked_label(issue_labels, planned_labels):
            actions.append(AddLabelAction(
                issue_number=issue_number,
                label=self.provider_policy.blocked_label(),
                reason=f"provider unavailable: {provider}",
                expected=build_expected_for_mutation(),
            ))
            plan_context.record_add(issue_number, self.provider_policy.blocked_label())

    def _plan_provider_resilience_labels(
        self,
        snapshot: OrchestratorSnapshot,
        plan_context: PlanContext,
    ) -> list[Action]:
        if not self.provider_policy:
            return []
        actions: list[Action] = []
        label = self.provider_policy.blocked_label()
        providers_by_issue = self.provider_policy.providers_for_snapshot(snapshot)
        for issue in snapshot.issues:
            providers = providers_by_issue.get(issue.number, set())
            if not providers:
                continue
            any_open = self.provider_policy.any_open(providers)
            issue_labels = plan_context.issue_labels(issue.number)
            planned_labels = plan_context.planned_adds(issue.number)
            if any_open and self.provider_policy.should_add_blocked_label(issue_labels, planned_labels):
                actions.append(AddLabelAction(
                    issue_number=issue.number,
                    label=label,
                    reason=f"provider unavailable: {', '.join(sorted(providers))}",
                    expected=build_expected_for_mutation(),
                    issue_key=issue.key.stable_id(),
                ))
                plan_context.record_add(issue.number, label)
            if (
                not any_open
                and self.provider_policy.should_remove_blocked_label(issue_labels, planned_labels)
                and plan_context.should_remove_label(issue.number, label)
            ):
                actions.append(RemoveLabelAction(
                    issue_number=issue.number,
                    label=label,
                    reason=f"provider available: {', '.join(sorted(providers))}",
                    expected=build_expected_for_mutation(),
                    issue_key=issue.key.stable_id(),
                ))
                plan_context.record_remove(issue.number, label)
        return actions

    def _plan_observed_completion_labels(
        self,
        snapshot: OrchestratorSnapshot,
        plan_context: PlanContext,
    ) -> list[Action]:
        """Plan immediate label updates for observed completions.

        This is the "immediate label projection" phase of async completion processing.
        When a session completes, we:
        1. Remove in-progress label immediately (don't wait for publish job)
        2. Add pr-pending label if outcome is COMPLETED (anticipating PR creation)
        3. Add blocked label if outcome is BLOCKED
        4. Add needs-human label if outcome is NEEDS_HUMAN

        The actual publish work (git push, PR creation) happens in background
        via the PublishJobExecutor.

        Returns:
            List of label actions to apply immediately
        """
        actions: list[Action] = []

        if not snapshot.observed_completions:
            return actions

        for observed in snapshot.observed_completions:
            issue_number = observed.issue_number
            ik = observed.issue_key_str

            # Always remove in-progress label when session completes
            actions.append(RemoveLabelAction(
                issue_number=issue_number,
                label=self._lm.in_progress,
                reason=f"session completed with outcome={observed.outcome}",
                expected=build_expected_for_mutation(),
                issue_key=ik,
            ))

            # Add outcome-specific label immediately
            if observed.outcome == CompletionOutcome.COMPLETED:
                # Session completed successfully - will create PR
                # Add pr-pending immediately (don't wait for publish job)
                if observed.needs_publish:
                    actions.append(AddLabelAction(
                        issue_number=issue_number,
                        label=self._lm.pr_pending,
                        reason="session completed - publish job pending",
                        expected=build_expected_for_mutation(),
                        issue_key=ik,
                    ))
                    logger.debug(
                        "Planner: projecting pr-pending label for issue #%d (publish job pending)",
                        issue_number,
                    )
            elif observed.outcome == CompletionOutcome.BLOCKED:
                # Session blocked - add blocked label
                blocked_label = self._blocked_label_for_record(observed)
                if plan_context.should_add_label(issue_number, blocked_label):
                    actions.append(AddLabelAction(
                        issue_number=issue_number,
                        label=blocked_label,
                        reason=f"session blocked: {observed.blocked_reason or 'unknown'}",
                        expected=build_expected_for_mutation(),
                        issue_key=ik,
                    ))
                    plan_context.record_add(issue_number, blocked_label)
                    logger.debug("Planner: adding blocked label for issue #%d", issue_number)
            elif observed.outcome == CompletionOutcome.NEEDS_HUMAN:
                # Session needs human intervention - use BLOCKED_NEEDS_HUMAN
                if plan_context.should_add_label(issue_number, self._lm.needs_human):
                    actions.append(AddLabelAction(
                        issue_number=issue_number,
                        label=self._lm.needs_human,
                        reason="session needs human intervention",
                        expected=build_expected_for_mutation(),
                        issue_key=ik,
                    ))
                    plan_context.record_add(issue_number, self._lm.needs_human)
                    logger.debug("Planner: adding blocked-needs-human label for issue #%d", issue_number)
            elif observed.outcome in (CompletionOutcome.REVIEW_APPROVED, CompletionOutcome.REVIEW_CHANGES_REQUESTED):
                # Review session completed - labels handled by review workflow
                logger.debug(
                    "Planner: review session completed for issue #%d, outcome=%s",
                    issue_number,
                    observed.outcome,
                )

        return actions

    def _plan_triage_issue_creation(self, snapshot: OrchestratorSnapshot) -> Optional[CreateTriageIssueAction]:
        """Plan triage issue creation if threshold is met."""
        if not snapshot.triage_facts:
            return None

        facts = snapshot.triage_facts
        if not self._should_create_triage_issue(facts):
            return None

        title, body = self._build_triage_issue_content(facts)
        labels = self._compute_triage_labels(facts)
        milestone = self._compute_triage_milestone(facts)

        logger.info("Planner: creating triage issue for %d PRs (labels=%s, milestone=%s)", facts.pr_count, labels, milestone)
        return CreateTriageIssueAction(
            title=title, body=body, labels=labels, pr_count=facts.pr_count,
            milestone=milestone, reason=f"threshold met: {facts.pr_count} >= {facts.threshold}",
        )

    def _should_create_triage_issue(self, facts: "TriageFacts") -> bool:
        """Check if triage issue should be created."""
        if facts.pr_count < facts.threshold:
            logger.debug("Planner: triage threshold not met (%d/%d)", facts.pr_count, facts.threshold)
            return False
        if facts.existing_triage_issue:
            logger.debug("Planner: triage issue #%d already exists", facts.existing_triage_issue)
            return False
        return True

    def _build_triage_issue_content(self, facts: "TriageFacts") -> tuple[str, str]:
        """Build title and body for triage issue."""
        pr_list = "\n".join(f"- PR #{pr[0]}: {pr[1]}" for pr in facts.prs)
        body = f"""## Triage Batch Review Triggered

{facts.pr_count} PRs have passed code review and are ready for triage review:

{pr_list}

Review these PRs for patterns, architectural concerns, and process improvements.
Flip labels from `{facts.watch_label}` to `{self.config.triage_reviewed_label}` after review.
"""
        title = f"Triage Batch Review: {facts.pr_count} PRs pending"
        priority_prefix = self._triage_priority_prefix()
        if priority_prefix and not re.search(r"^\[P\d-\d+\]", title):
            title = f"[{priority_prefix}-000] {title}"
        return title, body

    def _triage_priority_prefix(self) -> str | None:
        """Return [P?-nnn] prefix tier from triage.priority if configured."""
        priority = self.config.triage.priority
        if not priority:
            return None
        priority = priority.strip()
        if re.fullmatch(r"P\d", priority):
            return priority
        return None

    def _compute_triage_labels(self, facts: "TriageFacts") -> tuple[str, ...]:
        """Compute labels for triage issue."""
        label_list: list[str] = []
        if self.config.triage_review_agent:
            label_list.append(self.config.triage_review_agent)
        if self.config.filtering.label and self.config.filtering.label not in label_list:
            label_list.append(self.config.filtering.label)
        label_list.extend(self.config.triage.explicit_labels)
        for inherit_label in self.config.triage.inherit_labels:
            if inherit_label in facts.source_labels:
                label_list.append(inherit_label)
        return tuple(label_list)

    def _compute_triage_milestone(self, facts: "TriageFacts") -> int | None:
        """Compute milestone for triage issue."""
        triage_ms = self.config.triage.milestone_strategy

        if triage_ms.explicit:
            logger.debug("Planner: explicit milestone '%s' configured but name lookup not implemented", triage_ms.explicit)
            return None

        if triage_ms.inherit_from_issues and facts.source_milestones:
            sorted_milestones = sorted(facts.source_milestones, key=lambda m: m[0])
            if triage_ms.inherit_from_issues == "earliest":
                return sorted_milestones[0][0]
            return sorted_milestones[-1][0]

        return None

    def _plan_discovered_reworks(self, snapshot: OrchestratorSnapshot) -> list[Action]:
        """Plan queue actions for discovered reworks from scans.

        Returns:
            List of QueueReworkAction for reworks not already queued
        """
        actions: list[Action] = []

        if not snapshot.discovered_reworks:
            return actions

        queued_issue_ids = {
            r.resolve_issue_number()
            for r in snapshot.pending_reworks
            if r.resolve_issue_number() is not None
        }

        for rework in snapshot.discovered_reworks:
            if rework.issue_number not in queued_issue_ids:
                if (
                    rework.source == POST_PUBLISH_VALIDATION_SOURCE
                    and rework.pr_number > 0
                ):
                    actions.append(RemoveLabelAction(
                        issue_number=rework.pr_number,
                        label=self._lm.code_reviewed,
                        reason=(
                            "post-publish validation failed after review approval; "
                            "clearing code-reviewed"
                        ),
                    ))
                    actions.append(AddLabelAction(
                        issue_number=rework.pr_number,
                        label=self._lm.needs_rework,
                        reason=(
                            "post-publish validation failed after review approval; "
                            "marking PR for rework"
                        ),
                    ))
                    if rework.feedback:
                        actions.append(AddCommentAction(
                            number=rework.pr_number,
                            is_pr=True,
                            comment=build_post_publish_validation_comment(rework.feedback),
                            reason="post-publish validation failed after review approval",
                        ))
                # Remove pr-pending so scheduler considers issue available again
                actions.append(RemoveLabelAction(
                    issue_number=rework.issue_number,
                    label=self._lm.pr_pending,
                    reason=f"rework needed for PR #{rework.pr_number} (cycle {rework.rework_cycle})",
                ))
                actions.append(QueueReworkAction(
                    issue_number=rework.issue_number,
                    pr_number=rework.pr_number,
                    pr_url="",  # Not tracked in DiscoveredRework
                    branch_name=rework.branch_name,
                    rework_cycle=rework.rework_cycle,
                    source=rework.source,
                    feedback=rework.feedback,
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

        # Build issue-number → Issue lookup for stable key resolution
        issues_by_number = {i.number: i for i in snapshot.issues}

        for escalation in snapshot.discovered_escalations:
            # Resolve stable issue_key; fall back to str(issue_number)
            issue = issues_by_number.get(escalation.issue_number)
            issue_key = issue.key.stable_id() if issue else str(escalation.issue_number)

            # rework_cycle is the "next cycle" from the scanner (e.g., 3 means
            # label rework-cycle-2 was found).  Pass it directly — ActionApplier
            # subtracts 1 to derive the completed-cycle count for display.
            # This matches the normal-flow escalation in _plan_rework_launches.
            actions.append(EscalateToHumanAction(
                issue_number=escalation.issue_number,
                pr_number=escalation.pr_number,
                escalation_reason="max rework cycles exceeded",
                rework_cycles=escalation.rework_cycle,
                needs_human_label=self._lm.needs_human,
                needs_rework_label=self._lm.needs_rework,
                max_rework_cycles=self.config.max_rework_cycles,
                issue_key=issue_key,
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
        """Plan cleanup actions for completed sessions.

        Handles two types of cleanups:
        1. Deferred cleanups - wait for review label before cleaning
        2. Immediate cleanups - clean up right away (no review workflow)

        Returns:
            List of CleanupSessionAction for cleanups ready to process
        """
        actions: list[Action] = []

        if not snapshot.cleanup_facts:
            return actions

        facts = snapshot.cleanup_facts

        # 1. Deferred cleanups - check if PR has been reviewed
        for cleanup in facts.pending_cleanups:
            # cleanup is a tuple of (issue_number, pr_number, terminal_id, worktree_path)
            issue_number, pr_number, terminal_id, worktree_path = cleanup
            if pr_number in facts.reviewed_pr_numbers:
                actions.append(CleanupSessionAction(
                    issue_number=issue_number,
                    pr_number=pr_number,
                    terminal_id=terminal_id,
                    worktree_path=worktree_path,
                    close_tabs=facts.close_tabs,
                    remove_worktrees=facts.remove_worktrees,
                    reason=f"PR #{pr_number} has been reviewed",
                ))
                logger.info("Planner: deferred cleanup for issue #%d (PR #%d reviewed)",
                           issue_number, pr_number)

        # 2. Immediate cleanups - ready to execute now
        for cleanup in facts.immediate_cleanups:
            actions.append(CleanupSessionAction(
                issue_number=cleanup.issue_number,
                pr_number=0,  # No PR for immediate cleanups
                terminal_id=cleanup.terminal_id,
                worktree_path=cleanup.worktree_path,
                close_tabs=facts.close_tabs,
                remove_worktrees=facts.remove_worktrees,
                reason=f"session {cleanup.reason}",
            ))
            logger.info("Planner: immediate cleanup for issue #%d (%s)",
                       cleanup.issue_number, cleanup.reason)

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
                label=self._lm.in_progress,
                reason="stale - no running session",
                expected=build_expected_for_mutation(),
                issue_key=issue.key.stable_id(),
            ))
            logger.info("Planner: removing stale in-progress label from issue #%d",
                       issue.number)

        return actions

    def _plan_stale_claim_cleanup(self, snapshot: OrchestratorSnapshot) -> list[Action]:
        """Plan cleanup actions for issues with stale/expired claims.

        When an issue has the io:claimed label but the claim has expired
        (e.g., the orchestrator that held it crashed without releasing),
        we need to clean up:
        1. Remove the io:claimed label
        2. Add blocked:stale-claim label to flag for investigation

        The stale_claim_issues list is populated by the Orchestrator/Observer
        phase, which checks claims via ClaimManager.get_current_claim().

        Returns:
            List of label actions for stale claim cleanup
        """
        actions: list[Action] = []

        if not snapshot.stale_claim_issues:
            return actions

        for issue in snapshot.stale_claim_issues:
            # Remove the io:claimed label
            actions.append(RemoveLabelAction(
                issue_number=issue.number,
                label=self._lm.io_claimed,
                reason="stale claim expired",
                expected=build_expected_for_mutation(),
                issue_key=issue.key.stable_id(),
            ))
            # Add blocked:stale-claim label for visibility
            actions.append(AddLabelAction(
                issue_number=issue.number,
                label=self._lm.blocked_stale_claim,
                reason="stale claim detected - orchestrator may have crashed",
                expected=build_expected_for_mutation(),
                issue_key=issue.key.stable_id(),
            ))
            logger.info("Planner: cleaning up stale claim on issue #%d",
                       issue.number)

        return actions

    def _plan_issues(  # noqa: C901, PLR0912 — multi-phase issue scheduling
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

        # Get scheduler decisions with explicit availability reasons.
        scheduler_decisions = self.scheduler.evaluate_issues(
            list(snapshot.issues),
            check_dependencies=self.dependency_evaluator is not None,
            active_sessions=list(snapshot.active_sessions),
        )
        available = [d.issue for d in scheduler_decisions if d.available]
        dependency_blocked = [
            (d.issue, d.detail or "dependency blocked")
            for d in scheduler_decisions
            if d.reason == "dependency_blocked"
        ]
        decision_reason_by_issue = {d.issue.number: d.reason for d in scheduler_decisions}
        scheduler_filtered = sum(
            1 for decision in scheduler_decisions
            if not decision.available and decision.reason != "dependency_blocked"
        )

        # Record dependency-blocked items and add cross-milestone labels
        for issue, reason in dependency_blocked:
            logger.info(issue_log(issue.number, "Skipped: reason=blocked_by_dependency detail=%s"), reason)
            skipped.append(SkippedItem(
                item_type="issue",
                number=issue.number,
                reason=f"dependency: {reason}",
            ))
            # Add cross-milestone label if this is a milestone scope violation
            if "cross-milestone" in reason.lower():
                actions.append(AddLabelAction(
                    issue_number=issue.number,
                    label=self._lm.blocked_cross_milestone,
                    reason=f"dependency violates milestone scope: {reason}",
                    expected=build_expected_for_mutation(),
                    issue_key=issue.key.stable_id(),
                ))

        # Filter out providers with open circuit
        if self.provider_policy:
            filtered: list[Issue] = []
            for issue in available:
                provider = self.provider_policy.provider_for_issue(issue)
                if provider and self.provider_policy.is_open(provider):
                    skipped.append(SkippedItem(
                        item_type="issue",
                        number=issue.number,
                        reason=f"provider unavailable: {provider}",
                    ))
                    logger.info(issue_log(issue.number, "Skipped: reason=provider_unavailable provider=%s"), provider)
                    continue
                filtered.append(issue)
            available = filtered

        # Filter out issues already being worked on, just completed, or failed this cycle.
        # Include both discovered (new this tick) and pending (queued for launch) reviews/reworks
        # to prevent launching a code session for an issue that already has review/rework work.
        issues_with_reviews = {r.issue_number for r in snapshot.discovered_reviews}
        issues_with_reviews.update(r.issue_number for r in snapshot.pending_reviews)
        issues_with_reworks = {r.issue_number for r in snapshot.discovered_reworks}
        issues_with_reworks.update(
            n for r in snapshot.pending_reworks if (n := r.resolve_issue_number()) is not None
        )
        excluded_issues = (
            snapshot.active_issue_numbers |
            issues_with_reviews |
            issues_with_reworks |
            snapshot.failed_this_cycle |  # Skip issues that failed until cache refresh
            snapshot.session_history_issue_numbers  # Skip issues with completed sessions
        )
        not_active = [
            issue for issue in available
            if issue.number not in excluded_issues
        ]

        # Log per-issue exclusion reasons for diagnostics.
        skip_reason_by_issue: dict[int, str] = {}
        for issue in available:
            if issue.number in snapshot.active_issue_numbers:
                skipped.append(SkippedItem(item_type="issue", number=issue.number, reason="active session running"))
                logger.info(issue_log(issue.number, "Skipped: reason=active_session"))
                skip_reason_by_issue[issue.number] = "active_session"
            elif issue.number in issues_with_reviews:
                skipped.append(SkippedItem(item_type="issue", number=issue.number, reason="pending review"))
                logger.info(issue_log(issue.number, "Skipped: reason=pending_review"))
                skip_reason_by_issue[issue.number] = "pending_review"
            elif issue.number in issues_with_reworks:
                skipped.append(SkippedItem(item_type="issue", number=issue.number, reason="pending rework"))
                logger.info(issue_log(issue.number, "Skipped: reason=pending_rework"))
                skip_reason_by_issue[issue.number] = "pending_rework"
            elif issue.number in snapshot.failed_this_cycle:
                skipped.append(SkippedItem(item_type="issue", number=issue.number, reason="failed this cycle - waiting for cache refresh"))
                logger.info(issue_log(issue.number, "Skipped: reason=failed_this_cycle"))
                skip_reason_by_issue[issue.number] = "failed_this_cycle"
            elif issue.number in snapshot.session_history_issue_numbers:
                skipped.append(SkippedItem(item_type="issue", number=issue.number, reason="session completed this run"))
                logger.info(issue_log(issue.number, "Skipped: reason=session_history"))
                skip_reason_by_issue[issue.number] = "session_history"

        # Pick next batch based on priority
        to_launch = self.scheduler.pick_next_batch(
            available=not_active,
            current_count=snapshot.active_count,
            priority_overrides=list(snapshot.priority_queue),
        )

        # Create launch actions
        for issue in to_launch[:capacity]:
            priority_reason = self._get_priority_reason(issue)
            logger.info(
                issue_log(issue.number, "Selected for session: type=code priority=%s slots_available=%d"),
                priority_reason, capacity
            )
            actions.append(LaunchSessionAction(
                session_type=SessionType.ISSUE,
                number=issue.number,
                command="",  # Orchestrator will fill in
                working_dir="",  # Orchestrator will fill in
                reason=f"scheduled: priority={priority_reason}",
            ))

        # Pipeline funnel summary for diagnostics
        snapshot_numbers = sorted(i.number for i in snapshot.issues)
        available_numbers = sorted(i.number for i in available)
        eligible_numbers = sorted(i.number for i in not_active)
        launching_numbers = sorted(i.number for i in to_launch[:capacity])
        logger.info(
            "[PLAN] Issue pipeline: snapshot=%s → scheduler=%s (filtered=%d, dep_blocked=%d) "
            "→ eligible=%s → launching=%s (capacity=%d)",
            snapshot_numbers, available_numbers, scheduler_filtered,
            len(dependency_blocked), eligible_numbers, launching_numbers, capacity,
        )
        launching_set = set(launching_numbers)
        dep_blocked_map = {issue.number: reason for issue, reason in dependency_blocked}
        decision_by_issue: dict[int, str] = {}
        detail_by_issue: dict[int, str] = {}
        for issue in snapshot.issues:
            if issue.number in launching_set:
                decision_by_issue[issue.number] = "launch:scheduled"
                continue
            if issue.number in skip_reason_by_issue:
                decision_by_issue[issue.number] = f"skip:{skip_reason_by_issue[issue.number]}"
                continue
            if issue.number in dep_blocked_map:
                decision_by_issue[issue.number] = "skip:dependency_blocked"
                detail_by_issue[issue.number] = dep_blocked_map[issue.number]
                continue
            scheduler_reason = decision_reason_by_issue.get(issue.number, "unknown")
            decision_by_issue[issue.number] = f"skip:{scheduler_reason}"
        self._log_queue_decision_changes(decision_by_issue, detail_by_issue)

        return actions, skipped, len(actions)

    def _log_queue_decision_changes(
        self,
        decision_by_issue: dict[int, str],
        detail_by_issue: dict[int, str],
    ) -> None:
        """Emit queue decision traces only when they change, plus periodic summary."""
        for issue_number, decision in decision_by_issue.items():
            previous = self._last_queue_decisions.get(issue_number)
            if previous == decision:
                continue
            self._last_queue_decisions[issue_number] = decision
            if decision.startswith("launch:"):
                reason = decision.split(":", 1)[1]
                logger.info(
                    "trace-queue-decision issue=%d decision=launch reason=%s",
                    issue_number,
                    reason,
                )
                continue
            reason = decision.split(":", 1)[1]
            if reason == "dependency_blocked":
                logger.info(
                    "trace-queue-decision issue=%d decision=skip reason=dependency_blocked detail=%s",
                    issue_number,
                    detail_by_issue.get(issue_number, "dependency blocked"),
                )
            else:
                logger.info(
                    "trace-queue-decision issue=%d decision=skip reason=%s",
                    issue_number,
                    reason,
                )

        # Prune stale issues no longer in this snapshot.
        current_numbers = set(decision_by_issue.keys())
        for issue_number in list(self._last_queue_decisions.keys()):
            if issue_number not in current_numbers:
                del self._last_queue_decisions[issue_number]

        now = time.monotonic()
        if (now - self._last_queue_summary_logged_at) < self._queue_summary_interval_seconds:
            return
        self._last_queue_summary_logged_at = now

        launch_count = 0
        reason_counts: dict[str, int] = {}
        for decision in decision_by_issue.values():
            kind, reason = decision.split(":", 1)
            if kind == "launch":
                launch_count += 1
                continue
            reason_counts[reason] = reason_counts.get(reason, 0) + 1
        reason_summary = ", ".join(f"{reason}:{count}" for reason, count in sorted(reason_counts.items()))
        logger.info(
            "trace-queue-summary total=%d launch=%d skip=%d reasons=%s",
            len(decision_by_issue),
            launch_count,
            len(decision_by_issue) - launch_count,
            reason_summary or "none",
        )

    def _plan_reviews(
        self,
        snapshot: OrchestratorSnapshot,
        capacity: int,
        plan_context: PlanContext,
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
                logger.info(
                    issue_log(review.issue_number, "Skipped review: pr=#%d reason=%s"),
                    review.pr_number, decision.skip_reason
                )
                skipped.append(SkippedItem(
                    item_type="review",
                    number=review.pr_number,
                    reason=decision.skip_reason,
                ))
            return actions, skipped

        if decision.should_launch:
            for review in decision.reviews_to_launch[:capacity]:
                reviewer_label = self.config.get_reviewer_for_agent(review.agent_label) if review.agent_label else self.config.code_review_agent
                provider = self.provider_policy.provider_for_agent_label(reviewer_label) if self.provider_policy else None
                if provider and self.provider_policy and self.provider_policy.is_open(provider):
                    self._record_provider_skip(
                        issue_number=review.issue_number,
                        item_type="review",
                        item_number=review.pr_number,
                        provider=provider,
                        actions=actions,
                        skipped=skipped,
                        plan_context=plan_context,
                    )
                    continue
                logger.info(
                    issue_log(review.issue_number, "Selected for session: type=review pr=#%d slots_available=%d"),
                    review.pr_number, capacity
                )
                actions.append(LaunchSessionAction(
                    session_type=SessionType.REVIEW,
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
        plan_context: PlanContext,
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
                issue_num = rework.resolve_issue_number()
                if issue_num is None:
                    logger.warning("Planner: skipping rework with unresolved issue number: %s", rework.issue_key)
                    continue
                logger.info(
                    issue_log(issue_num, "Skipped rework: cycle=%d reason=%s"),
                    rework.rework_cycle, decision.skip_reason
                )
                skipped.append(SkippedItem(
                    item_type="rework",
                    number=issue_num,
                    reason=decision.skip_reason,
                ))
            return actions, skipped

        if decision.should_launch:
            for rework in decision.reworks_to_launch[:capacity]:
                issue_num = rework.resolve_issue_number()
                if issue_num is None:
                    logger.warning("Planner: skipping rework with unresolved issue number: %s", rework.issue_key)
                    continue
                provider = self.provider_policy.provider_for_agent_label(rework.agent_type) if self.provider_policy else None
                if provider and self.provider_policy and self.provider_policy.is_open(provider):
                    self._record_provider_skip(
                        issue_number=issue_num,
                        item_type="rework",
                        item_number=issue_num,
                        provider=provider,
                        actions=actions,
                        skipped=skipped,
                        plan_context=plan_context,
                    )
                    continue
                # Check for escalation
                escalation = self.rework_workflow.should_escalate(rework.rework_cycle)
                if escalation.should_escalate:
                    logger.info(
                        issue_log(issue_num, "Escalating to human: cycle=%d max=%d"),
                        rework.rework_cycle, escalation.max_cycles
                    )
                    actions.append(EscalateToHumanAction(
                        issue_number=issue_num,
                        pr_number=rework.pr_number or issue_num,
                        escalation_reason=escalation.reason or "max rework cycles reached",
                        rework_cycles=rework.rework_cycle,
                        needs_human_label=self._lm.needs_human,
                        needs_rework_label=self._lm.needs_rework,
                        max_rework_cycles=self.config.max_rework_cycles,
                        issue_key=rework.issue_key.stable_id(),
                        reason=f"escalating: cycle {rework.rework_cycle} > max {escalation.max_cycles}",
                        expected=build_expected_for_mutation(),
                    ))
                else:
                    logger.info(
                        issue_log(issue_num, "Selected for session: type=rework cycle=%d slots_available=%d"),
                        rework.rework_cycle, capacity
                    )
                    actions.append(LaunchSessionAction(
                        session_type=SessionType.REWORK,
                        number=issue_num,
                        command="",  # Orchestrator will fill in
                        working_dir="",  # Orchestrator will fill in
                        reason=f"rework cycle {rework.rework_cycle} for issue #{issue_num}",
                    ))

        return actions, skipped

    def _plan_validation_retries(
        self,
        snapshot: OrchestratorSnapshot,
        capacity: int,
        plan_context: PlanContext,
    ) -> tuple[list[Action], list[SkippedItem]]:
        """Plan launch actions for coding sessions that need validation retry."""
        actions: list[Action] = []
        skipped: list[SkippedItem] = []
        seen_issue_numbers: set[int] = set()

        for retry in snapshot.pending_validation_retries:
            if len(actions) >= capacity:
                break
            issue_number = retry.issue_number
            if issue_number in seen_issue_numbers:
                skipped.append(SkippedItem(
                    item_type="validation_retry",
                    number=issue_number,
                    reason="duplicate pending validation retry",
                ))
                continue
            seen_issue_numbers.add(issue_number)

            if issue_number in snapshot.active_issue_numbers:
                skipped.append(SkippedItem(
                    item_type="validation_retry",
                    number=issue_number,
                    reason="active session running",
                ))
                logger.info(issue_log(issue_number, "Skipped validation retry: reason=active_session"))
                continue

            provider = (
                self.provider_policy.provider_for_agent_label(retry.agent_label)
                if self.provider_policy and retry.agent_label
                else None
            )
            if provider and self.provider_policy and self.provider_policy.is_open(provider):
                self._record_provider_skip(
                    issue_number=issue_number,
                    item_type="validation_retry",
                    item_number=issue_number,
                    provider=provider,
                    actions=actions,
                    skipped=skipped,
                    plan_context=plan_context,
                )
                continue

            logger.info(
                issue_log(issue_number, "Selected for session: type=validation_retry retry_count=%d slots_available=%d"),
                retry.retry_count,
                capacity,
            )
            actions.append(LaunchValidationRetryAction(
                issue_number=issue_number,
                retry_count=retry.retry_count,
                reason=f"validation retry {retry.retry_count} for issue #{issue_number}",
            ))

        return actions, skipped

    def _plan_triage(
        self,
        snapshot: OrchestratorSnapshot,
        capacity: int,
        plan_context: PlanContext,
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
            provider = self.provider_policy.provider_for_agent_label(self.config.triage_review_agent) if self.provider_policy else None
            for triage in decision.triage_to_launch[:capacity]:
                if provider and self.provider_policy and self.provider_policy.is_open(provider):
                    self._record_provider_skip(
                        issue_number=triage.issue_number,
                        item_type="triage",
                        item_number=triage.issue_number,
                        provider=provider,
                        actions=actions,
                        skipped=skipped,
                        plan_context=plan_context,
                    )
                    continue
                actions.append(LaunchSessionAction(
                    session_type=SessionType.TRIAGE,
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
