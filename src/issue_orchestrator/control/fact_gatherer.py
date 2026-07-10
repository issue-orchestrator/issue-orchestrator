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
from ..ports.repository_host import RepositoryHost, RepositoryHostError
from ..ports import EventSink,  make_trace_event

if TYPE_CHECKING:
    from ..ports.issue import Issue
    from ..domain.models import (
        OrchestratorState,
        TriageFacts,
        CleanupFacts,
    )
    from .planner_types import OrchestratorSnapshot

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
        fetch_limit: int | None = None,
    ) -> list["Issue"]:
        """Fetch all issues for configured agents from GitHub."""
        milestones = self.config.get_filter_milestones() or [milestone]
        limit = fetch_limit if fetch_limit is not None else self.config.filtering.fetch_limit
        all_issues, seen, still_needed = [], set(), set(required_stable_ids) if required_stable_ids else None

        for agent_label in self.config.agents.keys():
            labels = list(labels_for_agent) + [agent_label]
            for milestone_name in milestones:
                issues = self.repository_host.list_issues(
                    labels=labels, milestone=milestone_name,
                    limit=limit, required_stable_ids=still_needed,
                )
                self._process_fetched_issues(issues, all_issues, seen, still_needed, agent_label, labels, milestone_name)

        return self._apply_issue_filter(all_issues)

    def _process_fetched_issues(
        self,
        issues: list["Issue"],
        all_issues: list["Issue"],
        seen: set[int],
        still_needed: set[str] | None,
        agent_label: str,
        labels: list[str],
        milestone_name: str | None,
    ) -> None:
        """Process fetched issues and emit events."""
        for issue in issues:
            if issue.number in seen:
                continue
            seen.add(issue.number)
            all_issues.append(issue)
            if still_needed and issue.key.stable_id() in still_needed:
                still_needed.discard(issue.key.stable_id())

        if self.events is not None:
            self._emit_issues_fetched_events(issues, agent_label, labels, milestone_name)

    def _emit_issues_fetched_events(self, issues: list["Issue"], agent_label: str, labels: list[str], milestone_name: str | None) -> None:
        """Emit events for fetched issues."""
        self.events.publish(make_trace_event(EventName.ISSUES_FETCHED, {
            "agent": agent_label, "labels": labels, "milestone": milestone_name,
            "count": len(issues), "issue_numbers": [i.number for i in issues],
        }))

    def _apply_issue_filter(self, all_issues: list["Issue"]) -> list["Issue"]:
        """Apply exclusion filter to issues."""
        issue_filter = self.config.get_issue_filter()
        if issue_filter.is_empty():
            return all_issues
        before_count = len(all_issues)
        filtered = issue_filter.apply(all_issues)
        if before_count != len(filtered):
            logger.debug("Excluded %d issues via filter %s", before_count - len(filtered), issue_filter)
        return filtered

    def create_snapshot(
        self,
        state: "OrchestratorState",
        issues: list["Issue"],
        stale_in_progress_issues: list["Issue"] | None = None,
        stale_claim_issues: list["Issue"] | None = None,
    ) -> "OrchestratorSnapshot":
        """Create an immutable snapshot for planning.

        Args:
            state: Current orchestrator state
            issues: Current list of issues from GitHub
            stale_in_progress_issues: Issues with in-progress label but no running session
            stale_claim_issues: Issues with io:claimed label but expired/invalid claim

        Returns:
            Immutable snapshot of orchestrator state for Planner
        """
        from .planner_types import OrchestratorSnapshot

        return OrchestratorSnapshot(
            issues=tuple(issues),
            active_sessions=tuple(state.active_sessions),
            pending_reviews=tuple(state.pending_reviews),
            pending_retrospective_reviews=tuple(state.pending_retrospective_reviews),
            pending_reworks=tuple(state.pending_reworks),
            pending_triage=tuple(state.pending_triage_reviews),
            pending_validation_retries=tuple(state.pending_validation_retries),
            paused=state.paused,
            priority_queue=tuple(state.priority_queue),
            issues_started_count=state.issues_started_count,
            max_issues_to_start=self.config.filtering.max_to_start if self.config.filtering.max_to_start > 0 else None,
            discovered_reviews=tuple(state.discovered_reviews),
            discovered_retrospective_reviews=tuple(
                state.discovered_retrospective_reviews
            ),
            discovered_awaiting_merge_reconciliations=tuple(
                state.discovered_awaiting_merge_reconciliations
            ),
            discovered_awaiting_merge_drifts=tuple(
                state.discovered_awaiting_merge_drifts
            ),
            discovered_reworks=tuple(state.discovered_reworks),
            discovered_escalations=tuple(state.discovered_escalations),
            discovered_awaiting_merge_escalations=tuple(
                state.discovered_awaiting_merge_escalations
            ),
            discovered_merge_queue_enqueues=tuple(
                state.discovered_merge_queue_enqueues
            ),
            discovered_failures=tuple(state.discovered_failures),
            triage_facts=self.gather_triage_facts(state),
            cleanup_facts=self.gather_cleanup_facts(state),
            stale_in_progress_issues=tuple(stale_in_progress_issues or []),
            stale_claim_issues=tuple(stale_claim_issues or []),
            failed_this_cycle=frozenset(state.failed_this_cycle),
            session_history_issue_numbers=frozenset(e.issue_number for e in state.session_history),
        )

    def gather_triage_facts(
        self,
        state: "OrchestratorState",
    ) -> Optional["TriageFacts"]:
        """Gather facts for triage review trigger decision."""
        from ..domain.models import TriageFacts

        watch_label = self._get_triage_watch_label()
        if not watch_label:
            return None

        prs = self._fetch_triage_prs(watch_label)
        existing_triage_issue = self._find_existing_triage_issue()
        all_labels, source_milestones = self._collect_pr_metadata(prs)

        return TriageFacts(
            pr_count=len(prs),
            threshold=self.config.triage_review_threshold,
            existing_triage_issue=existing_triage_issue,
            watch_label=watch_label,
            prs=tuple((pr.number, pr.title) for pr in prs),
            source_labels=frozenset(all_labels),
            source_milestones=tuple(source_milestones),
        )

    def _get_triage_watch_label(self) -> str | None:
        """Get the label to watch for triage review (None = trigger disabled)."""
        if not self.config.triage_review_agent or self.config.triage_review_threshold <= 0:
            return None
        return self.config.triage_watch_label

    def _fetch_triage_prs(self, watch_label: str) -> list[Any]:
        """Fetch PRs that are current triage batch candidates.

        Eligibility comes from the shared :class:`TriageCandidatePolicy` — the
        same predicate the manifest builder applies — so terminally-triaged
        PRs never count toward the threshold that the manifest then filters
        out (#6768 round 5: that divergence created empty-batch loops).
        """
        from .triage_manifest_builder import TriageCandidatePolicy

        policy = TriageCandidatePolicy.from_config(self.config)
        prs = self.repository_host.get_prs_with_label(watch_label, state="all")
        return [pr for pr in prs if policy.is_candidate(_pr_labels(pr))]

    def _find_existing_triage_issue(self) -> int | None:
        """Find existing triage review issue if any."""
        triage_agent = self.config.triage_review_agent
        if not triage_agent:
            return None
        existing = self.repository_host.list_issues(
            labels=[triage_agent],
            state="open",
            limit=10,
        )
        filter_label = self.config.filtering.label
        for issue in existing:
            if filter_label and filter_label not in issue.labels:
                continue
            if "Batch Review" in issue.title or "Triage Review" in issue.title:
                return issue.number
        return None

    def _collect_pr_metadata(self, prs: list[Any]) -> tuple[set[str], list[tuple[int, str]]]:
        """Collect labels and milestones from PRs and their linked issues."""
        all_labels: set[str] = set()
        source_milestones: list[tuple[int, str]] = []

        for pr in prs:
            all_labels.update(_pr_labels(pr))
            self._collect_linked_issue_metadata(pr, all_labels, source_milestones)

        return all_labels, source_milestones

    def _collect_linked_issue_metadata(
        self,
        pr: object,
        all_labels: set[str],
        source_milestones: list[tuple[int, str]],
    ) -> None:
        """Collect metadata from issues linked to a PR."""
        matches = re.findall(r'#(\d+)', (getattr(pr, 'body', '') or "") + " " + pr.title)
        for match in matches:
            issue_num = int(match)
            issue = self.repository_host.get_issue(issue_num)
            if not issue:
                continue
            all_labels.update(issue.labels)
            if issue.milestone and issue.milestone_number:
                milestone_tuple = (issue.milestone_number, issue.milestone)
                if milestone_tuple not in source_milestones:
                    source_milestones.append(milestone_tuple)

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
            except RepositoryHostError:
                raise
            except Exception as e:
                logger.warning(f"[CLEANUP] Failed to fetch PRs with label {cleanup_label}: {e}")

        # Build immutable tuples of pending cleanup info
        pending_tuples = tuple(
            (c.issue_number, c.pr_number, c.terminal_id, str(c.worktree_path))
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
