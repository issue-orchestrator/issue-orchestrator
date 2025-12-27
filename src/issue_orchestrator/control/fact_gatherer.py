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
from dataclasses import dataclass
from typing import Optional, TYPE_CHECKING

from ..config import Config
from ..events import EventName
from ..ports.repository_host import RepositoryHost
from ..ports import EventSink, TraceEvent

if TYPE_CHECKING:
    from ..models import (
        Issue,
        OrchestratorState,
        TriageFacts,
        CleanupFacts,
    )
    from .planner import OrchestratorSnapshot

logger = logging.getLogger(__name__)


@dataclass
class FactGatherer:
    """Gathers facts from state and external sources for planning.

    This is a read-only component that creates immutable snapshots.
    It does not modify any state.
    """

    config: Config
    repository_host: RepositoryHost
    events: Optional[EventSink] = None

    def fetch_issues(self, labels_for_agent: list[str], milestone: Optional[str] = None) -> list["Issue"]:
        """Fetch all issues for configured agents from GitHub.

        Args:
            labels_for_agent: Labels that identify agent issues
            milestone: Optional milestone filter

        Returns:
            List of issues across all agent types
        """
        from ..models import Issue
        all_issues: list[Issue] = []
        for agent_label in self.config.agents.keys():
            labels = list(labels_for_agent)
            labels.append(agent_label)
            issues = self.repository_host.list_issues(
                labels=labels,
                milestone=milestone,
                limit=self.config.issue_fetch_limit,
            )
            all_issues.extend(issues)
            if self.events:
                self.events.publish(TraceEvent(EventName.ISSUES_FETCHED, {
                    "agent": agent_label,
                    "labels": labels,
                    "milestone": milestone,
                    "count": len(issues),
                    "issue_numbers": [i.number for i in issues],
                }))
        return all_issues

    def create_snapshot(
        self,
        state: "OrchestratorState",
        issues: list["Issue"],
    ) -> "OrchestratorSnapshot":
        """Create an immutable snapshot for planning.

        Args:
            state: Current orchestrator state
            issues: Current list of issues from GitHub

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
            max_issues_to_start=self.config.max_issues_to_start if self.config.max_issues_to_start > 0 else None,
            discovered_reviews=tuple(state.discovered_reviews),
            discovered_reworks=tuple(state.discovered_reworks),
            discovered_escalations=tuple(state.discovered_escalations),
            discovered_failures=tuple(state.discovered_failures),
            triage_facts=self.gather_triage_facts(state),
            cleanup_facts=self.gather_cleanup_facts(state),
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
        from ..models import TriageFacts

        # Check if triage review is configured
        if not self.config.triage_review_agent:
            return None
        if self.config.triage_review_threshold <= 0:
            return None

        # Label to watch: either explicit triage_review_label or code_reviewed_label
        watch_label = self.config.triage_review_label or self.config.code_reviewed_label
        if not watch_label:
            return None

        # Count PRs ready for triage review
        prs = self.repository_host.get_prs_with_label(watch_label)
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

        return TriageFacts(
            pr_count=pr_count,
            threshold=threshold,
            existing_triage_issue=existing_triage_issue,
            watch_label=watch_label,
            prs=pr_tuples,
        )

    def gather_cleanup_facts(
        self,
        state: "OrchestratorState",
    ) -> Optional["CleanupFacts"]:
        """Gather facts for cleanup decision.

        Returns immutable facts for the Planner to decide which cleanups to process.
        Does NOT perform cleanup - that's the Planner's job.

        Args:
            state: Current orchestrator state with pending_cleanups

        Returns:
            CleanupFacts if there are pending cleanups, else None
        """
        from ..models import CleanupFacts

        if not state.pending_cleanups:
            return None

        # Determine which label indicates review is complete
        if self.config.triage_review_agent:
            cleanup_label = self.config.triage_reviewed_label
            close_tabs = self.config.cleanup.with_triage.close_ai_session_tabs
            remove_wt = self.config.cleanup.with_triage.remove_worktrees
        elif self.config.code_review_agent:
            cleanup_label = self.config.code_reviewed_label
            close_tabs = self.config.cleanup.without_triage.close_ai_session_tabs
            remove_wt = self.config.cleanup.without_triage.remove_worktrees
        else:
            # No review workflow configured
            return None

        if not cleanup_label:
            return None

        # Get all PRs with the cleanup label
        try:
            reviewed_prs = self.repository_host.get_prs_with_label(cleanup_label)
            reviewed_pr_numbers = frozenset(pr.number for pr in reviewed_prs)
        except Exception as e:
            logger.warning(f"[CLEANUP] Failed to fetch PRs with label {cleanup_label}: {e}")
            return None

        # Build immutable tuples of pending cleanup info
        pending_tuples = tuple(
            (c.issue_number, c.pr_number, c.terminal_session_name, str(c.worktree_path))
            for c in state.pending_cleanups
        )

        return CleanupFacts(
            pending_cleanups=pending_tuples,
            reviewed_pr_numbers=reviewed_pr_numbers,
            close_tabs=close_tabs,
            remove_worktrees=remove_wt,
        )
