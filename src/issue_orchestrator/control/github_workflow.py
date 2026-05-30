"""GitHubWorkflow - GitHub-specific workflow operations.

This module contains workflow methods extracted from the Orchestrator:
- Issue fetching and filtering
- PR scanning for reviews/reworks
- Label operations
- Dependency problem tracking
"""

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional, Callable

if TYPE_CHECKING:
    from ..ports.issue import Issue
    from ..domain.models import OrchestratorState, DependencyProblem, Session, PendingCleanup
    from ..domain.state_machines.issue_machine import IssueStateMachine
    from .fact_gatherer import FactGatherer
    from .pr_scanner import PRScanner
    from .label_sync import LabelSync
    from .cleanup_manager import CleanupManager
    from .state_machine_manager import StateMachineManager
    from .label_manager import LabelManager

from ..infra.config import Config
from ..events import EventName, EventContext
from ..domain.models import (
    DiscoveredReview,
    DiscoveredRework,
    DiscoveredEscalation,
    DependencyProblem,
)
from ..ports import EventSink, make_trace_event, RepositoryHost
from .awaiting_merge_reconciler import AwaitingMergeReconciler
from .retrospective_review import discover_retrospective_review_issues
from .review_scope import ReviewScopeChecker

logger = logging.getLogger(__name__)


@dataclass
class GitHubWorkflow:
    """Handles GitHub-specific workflow operations."""

    config: Config
    events: EventSink
    repository_host: RepositoryHost
    fact_gatherer: "FactGatherer"
    pr_scanner: "PRScanner"
    label_sync: Optional["LabelSync"]
    event_context: EventContext
    label_manager: "LabelManager | None" = None

    def fetch_all_issues(
        self,
        milestone_filter: str | None,
        required_stable_ids: set[str] | None = None,
    ) -> list["Issue"]:
        """Fetch all issues from GitHub - delegates to FactGatherer.

        Args:
            milestone_filter: Optional milestone to filter by.
            required_stable_ids: Optional set of stable IDs that must be discovered.
                If provided and missing after cached fetch, retry without cache.
        """
        base_labels = [self.config.filtering.label] if self.config.filtering.label else []
        return self.fact_gatherer.fetch_issues(
            base_labels, milestone_filter, required_stable_ids=required_stable_ids
        )

    def fetch_discovery_issues(
        self,
        milestone_filter: str | None,
        fetch_limit: int,
    ) -> list["Issue"]:
        """Fetch a bounded issue set for incremental discovery."""
        base_labels = [self.config.filtering.label] if self.config.filtering.label else []
        return self.fact_gatherer.fetch_issues(
            base_labels,
            milestone_filter,
            fetch_limit=fetch_limit,
        )

    def fetch_delta_issues(
        self,
        *,
        since: str,
        fetch_limit: int,
    ) -> tuple[list["Issue"], str | None]:
        """Fetch repo-wide issue deltas since watermark."""
        return self.repository_host.list_issues_delta(since=since, limit=fetch_limit)

    def issue_in_scope(self, issue: "Issue") -> bool:
        """Return True if issue is in current orchestrator queue scope."""
        labels = set(issue.labels)
        if self.config.filtering.label and self.config.filtering.label not in labels:
            return False
        if not any(agent_label in labels for agent_label in self.config.agents.keys()):
            return False

        milestones = self.config.get_filter_milestones()
        if milestones:
            if issue.milestone is None or issue.milestone not in milestones:
                return False

        issue_filter = self.config.get_issue_filter()
        if not issue_filter.is_empty() and not issue_filter.apply([issue]):
            return False

        return True

    def refresh_issues(self, issue_numbers: list[int]) -> list["Issue"]:
        """Refresh a bounded set of issues by number."""
        refreshed: list["Issue"] = []
        for issue_number in issue_numbers:
            issue = self.refresh_issue(issue_number)
            if issue is not None:
                refreshed.append(issue)
        return refreshed

    def scan_needs_code_review_prs(
        self,
        state: "OrchestratorState",
        issue_branches: dict[int, str] | None = None,
    ) -> None:
        """Scan for PRs that need code review and add to discovered_reviews."""
        for r in self.pr_scanner.scan_for_reviews(
            state.pending_reviews,
            [s.terminal_id for s in state.active_sessions],
            issue_branches=issue_branches,
        ):
            state.discovered_reviews.append(
                DiscoveredReview(r.issue_number, r.pr_number, r.pr_url, r.branch_name)
            )

    def scan_needs_rework_prs(
        self,
        state: "OrchestratorState",
        issue_branches: dict[int, str] | None = None,
    ) -> None:
        """Scan for PRs that need rework and add to discovered_reworks/escalations."""
        reworks, escalations = self.pr_scanner.scan_for_reworks(
            state.pending_reworks,
            [s.issue.number for s in state.active_sessions],
            issue_branches=issue_branches,
        )
        for pr, issue, cycle in escalations:
            state.discovered_escalations.append(DiscoveredEscalation(issue, pr, cycle))
        for r in reworks:
            issue_number = r.resolve_issue_number()
            if issue_number is None:
                logger.warning(
                    "[SCANNER] Rework issue key missing issue number: %s",
                    r.issue_key,
                )
                continue
            state.discovered_reworks.append(
                DiscoveredRework(
                    issue_number,
                    r.pr_number or 0,
                    "",
                    r.agent_type,
                    r.rework_cycle,
                )
            )

    def scan_pending_pr_work(self, state: "OrchestratorState") -> None:
        """Scan review and rework queues using one issue-branch fetch per tick."""
        issue_branches = self.pr_scanner.load_issue_branches()
        self.scan_needs_code_review_prs(state, issue_branches=issue_branches)
        self.scan_needs_rework_prs(state, issue_branches=issue_branches)
        self.scan_retrospective_review_issues(state)
        result = AwaitingMergeReconciler(
            self.repository_host,
            label_manager=self.label_manager,
            post_publish_checks_pending_timeout_seconds=(
                self.config.post_publish_checks_pending_timeout_seconds
            ),
        ).discover(state)
        state.discovered_awaiting_merge_reconciliations.extend(result.reconciliations)
        state.discovered_awaiting_merge_drifts.extend(result.drifts)
        state.discovered_reworks.extend(result.reworks)
        state.discovered_awaiting_merge_escalations.extend(result.escalations)
        if result.discovered:
            logger.info(
                "Discovered %d awaiting-merge history reconciliations",
                result.discovered,
            )
        if result.drift_discovered:
            logger.info(
                "Discovered %d awaiting-merge issue/PR drift(s)",
                result.drift_discovered,
            )
        if result.rework_discovered:
            logger.info(
                "Discovered %d post-publish validation rework(s)",
                result.rework_discovered,
            )
        if result.escalation_discovered:
            logger.info(
                "Discovered %d post-publish escalation(s)",
                result.escalation_discovered,
            )

    def scan_retrospective_review_issues(self, state: "OrchestratorState") -> None:
        """Discover trigger-labeled issues for review-first existing-work audits."""
        state.discovered_retrospective_reviews.extend(
            discover_retrospective_review_issues(
                repository_host=self.repository_host,
                config=self.config,
                already_issue_numbers=state.retrospective_review_in_flight_issue_numbers(),
            )
        )

    def update_dependency_problems(
        self,
        state: "OrchestratorState",
        dep_blocked: list[tuple["Issue", str]],
    ) -> None:
        """Update state with dependency problems and emit events."""
        new = {
            i.number: DependencyProblem(i.number, i.title, [], r)
            for i, r in dep_blocked
        }
        blocked = set(new) - set(state.dependency_problems)
        unblocked = set(state.dependency_problems) - set(new)

        for n in blocked:
            self.events.publish(make_trace_event(
                EventName.DEPENDENCY_BLOCKED,
                self.event_context.enrich({
                    "issue_number": n,
                    "summary": new[n].summary,
                }),
            ))
        for n in unblocked:
            self.events.publish(make_trace_event(
                EventName.DEPENDENCY_UNBLOCKED,
                self.event_context.enrich({"issue_number": n}),
            ))

        state.dependency_problems = new

    def reconcile_orphaned_pr_labels(self, orchestrator_pr_marker: str) -> int:
        """Reconcile orphaned PR labels at startup.

        Returns the number of labels reconciled.
        """
        if not self.config.code_review_label or not self.config.repo or not self.label_sync:
            return 0
        scope_checker = ReviewScopeChecker(
            self.config,
            self.repository_host,
            log_prefix="LABEL_SYNC",
            require_open_issue=True,
        )
        return self.label_sync.reconcile_orphaned_pr_labels(
            self.config.code_review_label,
            self.config.code_reviewed_label,
            orchestrator_pr_marker,
            is_pr_in_scope=scope_checker.is_pr_in_scope,
        )

    def refresh_issue(self, issue_number: int) -> Optional["Issue"]:
        """Refresh a single issue from GitHub.

        Returns the refreshed issue, or None if the backing store reports the
        issue is absent. Access failures propagate.
        """
        return self.repository_host.get_issue(issue_number)

    def build_labels(self, *labels: str) -> list[str]:
        """Build a label list including the filter label if configured."""
        return list(labels) + ([self.config.filtering.label] if self.config.filtering.label else [])

    def get_milestone_filter(self) -> str | None:
        """Get the configured milestone filter."""
        return self.config.filtering.milestone

    def process_deferred_cleanups(
        self,
        pending_cleanups: list["PendingCleanup"],
        cleanup_manager: "CleanupManager",
    ) -> list["PendingCleanup"]:
        """Process deferred cleanups - moved per method table.

        Args:
            pending_cleanups: List of pending cleanups from state
            cleanup_manager: For processing the cleanups

        Returns:
            Updated list of pending cleanups
        """
        return cleanup_manager.process_deferred_cleanups(pending_cleanups)


def get_issue_machine(issue: "Issue", state_machines: "StateMachineManager") -> Optional["IssueStateMachine"]:
    """Get issue state machine - moved per method table."""
    return state_machines.get_issue_machine(issue)


def launch_issue_by_number(
    n: int,
    cached_queue_issues: list["Issue"],
    launch_session_fn: Callable[["Issue"], Optional["Session"]],
    increment_count_fn: Callable[[], None],
) -> Optional["Session"]:
    """Launch issue session by number - moved per method table.

    Args:
        n: Issue number
        cached_queue_issues: List of cached issues
        launch_session_fn: Function to launch session
        increment_count_fn: Function to increment issues_started_count

    Returns:
        The launched session or None
    """
    issue = next((i for i in cached_queue_issues if i.number == n), None)
    if not issue:
        return None
    s = launch_session_fn(issue)
    if s:
        increment_count_fn()
    return s
