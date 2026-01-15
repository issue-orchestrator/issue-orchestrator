"""FactGatherer - creates immutable snapshots for planning.

This module extracts fact-gathering logic from the orchestrator,
making it a pure read-only component that:
1. Reads current state (OrchestratorState)
2. Fetches external data via ports (RepositoryHost)
3. Returns immutable facts for the Planner

The FactGatherer has NO side effects - it only gathers information.
All mutations happen in the orchestrator based on Plan execution.

Usage:
    gatherer = FactGatherer(
        config=config,
        repository_host=github_adapter,
    )
    snapshot = gatherer.create_snapshot(state, issues)
"""

import logging
import re
from dataclasses import dataclass
from typing import Any, Optional, TYPE_CHECKING

from ..infra.config import Config
from ..events import EventName
from ..ports.repository_host import RepositoryHost
from ..ports import EventSink, TraceEvent

if TYPE_CHECKING:
    from ..ports.issue import Issue
    from ..domain.models import (
        OrchestratorState,
        TriageFacts,
        CleanupFacts,
    )
    from .planner import OrchestratorSnapshot

logger = logging.getLogger(__name__)


def _pr_labels(pr: Any) -> list[str]:
    labels = getattr(pr, "labels", None)
    if labels is None and isinstance(pr, dict):
        labels = pr.get("labels", [])
    return labels or []


@dataclass
class FactGatherer:
    """Gathers facts from state and external sources for planning.

    This is a read-only component that creates immutable snapshots.
    It does not modify any state.
    """

    config: Config
    repository_host: RepositoryHost
    events: Optional[EventSink] = None

    def fetch_issues(
        self,
        labels_for_agent: list[str],
        milestone: Optional[str] = None,
        required_stable_ids: set[str] | None = None,
    ) -> list["Issue"]:
        """Fetch all issues for configured agents from GitHub.

        Args:
            labels_for_agent: Labels that identify agent issues
            milestone: Optional milestone filter
            required_stable_ids: Optional set of stable IDs that must be discovered.
                If provided and missing after cached fetch, retry without cache.

        Returns:
            List of issues across all agent types
        """
        milestones = self.config.get_filter_milestones()
        if not milestones:
            milestones = [milestone]

        all_issues: list["Issue"] = []
        seen: set[int] = set()
        # Track which required IDs still need to be found across all agent types
        # Once an ID is found in any query, remove it from the required set
        still_needed = set(required_stable_ids) if required_stable_ids else None
        for agent_label in self.config.agents.keys():
            labels = list(labels_for_agent)
            labels.append(agent_label)
            for milestone_name in milestones:
                issues = self.repository_host.list_issues(
                    labels=labels,
                    milestone=milestone_name,
                    limit=self.config.filtering.fetch_limit,
                    required_stable_ids=still_needed,
                )
                for issue in issues:
                    if issue.number in seen:
                        continue
                    seen.add(issue.number)
                    all_issues.append(issue)
                    # Remove found IDs from the still_needed set
                    if still_needed and issue.key.stable_id() in still_needed:
                        still_needed.discard(issue.key.stable_id())
                if self.events:
                    self.events.publish(TraceEvent(EventName.ISSUES_FETCHED, {
                        "agent": agent_label,
                        "labels": labels,
                        "milestone": milestone_name,
                        "count": len(issues),
                        "issue_numbers": [i.number for i in issues],
                    }))
                    for issue in issues:
                        self.events.publish(TraceEvent(
                            EventName.ISSUE_LABELS_CHANGED,
                            {
                                "issue_number": issue.number,
                                "issue_key": issue.key.stable_id(),
                                "labels": list(issue.labels),
                                "state": issue.state,
                            },
                        ))

        # Apply exclusion filter (runs after GitHub API filtering)
        issue_filter = self.config.get_issue_filter()
        if not issue_filter.is_empty():
            before_count = len(all_issues)
            all_issues = issue_filter.apply(all_issues)
            if before_count != len(all_issues):
                logger.debug(
                    "Excluded %d issues via filter %s",
                    before_count - len(all_issues),
                    issue_filter,
                )

        return all_issues

    def create_snapshot(
        self,
        state: "OrchestratorState",
        issues: list["Issue"],
        stale_in_progress_issues: list["Issue"] | None = None,
    ) -> "OrchestratorSnapshot":
        """Create an immutable snapshot for planning.

        Args:
            state: Current orchestrator state
            issues: Current list of issues from GitHub
            stale_in_progress_issues: Issues with in-progress label but no running session

        Returns:
            Immutable snapshot of orchestrator state for Planner
        """
        from .planner import OrchestratorSnapshot

        return OrchestratorSnapshot(
            issues=tuple(issues),
            active_sessions=tuple(state.active_sessions),
            pending_reviews=tuple(state.pending_reviews),
            pending_reworks=tuple(state.pending_reworks),
            pending_triage=tuple(state.pending_triage_reviews),
            paused=state.paused,
            priority_queue=tuple(state.priority_queue),
            issues_started_count=state.issues_started_count,
            max_issues_to_start=self.config.filtering.max_to_start if self.config.filtering.max_to_start > 0 else None,
            discovered_reviews=tuple(state.discovered_reviews),
            discovered_reworks=tuple(state.discovered_reworks),
            discovered_escalations=tuple(state.discovered_escalations),
            discovered_failures=tuple(state.discovered_failures),
            triage_facts=self.gather_triage_facts(state),
            cleanup_facts=self.gather_cleanup_facts(state),
            stale_in_progress_issues=tuple(stale_in_progress_issues or []),
            failed_this_cycle=frozenset(state.failed_this_cycle),
            session_history_issue_numbers=frozenset(e.issue_number for e in state.session_history),
        )

    def gather_triage_facts(
        self,
        state: "OrchestratorState",
    ) -> Optional["TriageFacts"]:
        """Gather facts for triage review trigger decision.

        Returns immutable facts for the Planner to decide whether to create
        a triage issue. Does NOT create the issue - that's the Planner's job.

        Args:
            state: Current orchestrator state (for future use)

        Returns:
            TriageFacts if triage is configured, else None
        """
        from ..domain.models import TriageFacts

        # Check if triage review is configured
        if not self.config.triage_review_agent:
            return None
        if self.config.triage_review_threshold <= 0:
            return None

        # Label to watch: either explicit triage_review_label or code_reviewed_label
        watch_label = self.config.triage_review_label or self.config.code_reviewed_label
        if not watch_label:
            return None

        # Count PRs ready for triage review (include closed to avoid missing fast-closed PRs)
        prs = self.repository_host.get_prs_with_label(watch_label, state="all")
        if self.config.filtering.label:
            prs = [
                pr
                for pr in prs
                if self.config.filtering.label in _pr_labels(pr)
            ]
        pr_count = len(prs)
        threshold = self.config.triage_review_threshold

        # Check if a triage review issue already exists
        existing_triage_issue: Optional[int] = None
        existing = self.repository_host.list_issues(
            labels=[self.config.triage_review_agent],
            limit=10,
        )
        for issue in existing:
            if "Batch Review" in issue.title or "Triage Review" in issue.title:
                existing_triage_issue = issue.number
                break

        # Build PR info tuples for body generation
        pr_tuples = tuple((pr.number, pr.title) for pr in prs)

        # Collect all labels from source PRs for label inheritance
        all_labels: set[str] = set()
        for pr in prs:
            all_labels.update(_pr_labels(pr))

        # Collect milestones from linked issues
        # PRs don't have milestones directly - they're on the linked issues
        # Extract issue numbers from PR bodies/titles and look up milestones
        source_milestones: list[tuple[int, str]] = []
        for pr in prs:
            # Try to extract linked issue number from PR body or title
            # Common patterns: "Fixes #123", "Closes #123", "#123", "issue-123"
            matches = re.findall(r'#(\d+)', (pr.body or "") + " " + pr.title)
            for match in matches:
                issue_num = int(match)
                try:
                    issue = self.repository_host.get_issue(issue_num)
                    if issue and issue.milestone and issue.milestone_number:
                        milestone_tuple = (issue.milestone_number, issue.milestone)
                        if milestone_tuple not in source_milestones:
                            source_milestones.append(milestone_tuple)
                except Exception:
                    pass  # Ignore lookup errors

        return TriageFacts(
            pr_count=pr_count,
            threshold=threshold,
            existing_triage_issue=existing_triage_issue,
            watch_label=watch_label,
            prs=pr_tuples,
            source_labels=frozenset(all_labels),
            source_milestones=tuple(source_milestones),
        )

    def gather_cleanup_facts(
        self,
        state: "OrchestratorState",
    ) -> Optional["CleanupFacts"]:
        """Gather facts for cleanup decision.

        Returns immutable facts for the Planner to decide which cleanups to process.
        Does NOT perform cleanup - that's the Planner's job.

        Handles two types of cleanups:
        1. Deferred cleanups (pending_cleanups) - waiting for review label
        2. Immediate cleanups (immediate_cleanups) - ready to execute now

        Args:
            state: Current orchestrator state with pending_cleanups and immediate_cleanups

        Returns:
            CleanupFacts if there are any cleanups to process, else None
        """
        from ..domain.models import CleanupFacts

        # Check if there's anything to clean up
        has_pending = bool(state.pending_cleanups)
        has_immediate = bool(state.immediate_cleanups)

        if not has_pending and not has_immediate:
            return None

        # Determine cleanup settings based on workflow
        if self.config.triage_review_agent:
            cleanup_label = self.config.triage_reviewed_label
            close_tabs = self.config.cleanup.with_triage.close_ai_session_tabs
            remove_wt = self.config.cleanup.with_triage.remove_worktrees
        elif self.config.code_review_agent:
            cleanup_label = self.config.code_reviewed_label
            close_tabs = self.config.cleanup.without_triage.close_ai_session_tabs
            remove_wt = self.config.cleanup.without_triage.remove_worktrees
        else:
            # No review workflow - use defaults for immediate cleanups
            cleanup_label = None
            close_tabs = self.config.cleanup.without_triage.close_ai_session_tabs
            remove_wt = self.config.cleanup.without_triage.remove_worktrees

        # Get reviewed PRs for deferred cleanups (only if we have pending cleanups)
        reviewed_pr_numbers: frozenset[int] = frozenset()
        if has_pending and cleanup_label:
            try:
                reviewed_prs = self.repository_host.get_prs_with_label(cleanup_label)
                reviewed_pr_numbers = frozenset(pr.number for pr in reviewed_prs)
            except Exception as e:
                logger.warning(f"[CLEANUP] Failed to fetch PRs with label {cleanup_label}: {e}")

        # Build immutable tuples of pending cleanup info
        pending_tuples = tuple(
            (c.issue_number, c.pr_number, c.terminal_session_name, str(c.worktree_path))
            for c in state.pending_cleanups
        )

        # Build immutable tuple of immediate cleanups
        immediate_tuples = tuple(state.immediate_cleanups)

        return CleanupFacts(
            pending_cleanups=pending_tuples,
            reviewed_pr_numbers=reviewed_pr_numbers,
            close_tabs=close_tabs,
            remove_worktrees=remove_wt,
            immediate_cleanups=immediate_tuples,
        )
