"""FactGatherer - creates immutable snapshots for planning.

This module extracts fact-gathering logic from the orchestrator,
making it a pure read-only component that:
1. Reads current state (OrchestratorState)
2. Fetches external data via ports (RepositoryHost)
3. Returns immutable facts for the Planner

The FactGatherer makes NO decisions and plans no mutations - all state
transitions happen in the orchestrator based on Plan execution. Its only
outputs besides the snapshot are fire-and-forget observation sinks: trace
events (EventSink) and the triage board projection (TriageBoardPublisher,
#6781), both projections of what was observed, never policy.

Usage:
    gatherer = FactGatherer(
        config=config,
        repository_host=github_adapter,
    )
    snapshot = gatherer.create_snapshot(state, issues)
"""

import logging
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Optional, TYPE_CHECKING

from ..infra.config import Config
from ..events import EventName
from ..ports.repository_host import RepositoryHost, RepositoryHostError
from ..ports import EventSink,  make_trace_event
from .health_review_trigger import (
    classify_triage_anchor_issues,
    discover_open_triage_anchor_issues,
    health_review_due,
    health_review_interval_minutes,
)

if TYPE_CHECKING:
    from ..ports.issue import Issue
    from ..ports.triage_authority import TriageAuthorityStore
    from ..domain.models import (
        OrchestratorState,
        TriageFacts,
        CleanupFacts,
    )
    from ..domain.triage_session import (
        ApprovedTriageOp,
        StoredTriageOp,
        TriageCaseFileSummary,
    )
    from .planner_types import OrchestratorSnapshot
    from .triage_board import TriageBoardPublisher

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
    # Orchestrator-owned gated-proposal ledger (#6778). Optional so unrelated
    # tests need not wire it; without it the anchor scan classifies no
    # approved ops (gate-labeled proposals are still excluded from anchors).
    triage_authority: Optional["TriageAuthorityStore"] = None
    # Fire-and-forget projection sink for triage facts (#6781): like the
    # event sink, it observes gathered facts (retaining the latest case-file
    # projection + refreshing the triage board file) and makes no decisions.
    # Optional so unrelated tests need not wire it.
    board_publisher: Optional["TriageBoardPublisher"] = None

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
        now: float | None = None,
    ) -> Optional["TriageFacts"]:
        """Gather facts for the triage batch and health-review triggers.

        Three independent triggers can each produce facts (only the case where
        none is active yields None):
          * BATCH fields, gated by ``triage_review_threshold`` (via the watch
            label);
          * HEALTH-REVIEW fields, gated by
            ``triage.health_review.interval_minutes``;
          * PROPOSAL fields (approved-op execution + terminal-op cleanup
            candidates), armed by the triage agent's local op ledger and
            reconciled whenever it holds an op — INDEPENDENT of the batch
            review threshold (#6779 R12), so a manual-approval / default
            (threshold=0) proposal still advances and self-heals.

        GitHub API discipline shapes every read here: due-ness is pure interval
        math computed FIRST, so a health-only configuration that is not yet due
        makes ZERO GitHub calls (no anchor fact can affect planning until the
        review is due); the exhaustive triage-agent scan runs only when the
        batch trigger is armed OR the local op ledger has proposals to
        reconcile (an empty ledger has nothing to approve or clean up, so no
        scan is worth making). A due health review also uses that exhaustive scan
        so its snapshot includes open case files while still deduplicating its
        anchor. Observation only: milestone ASSEMBLY policy
        (strategy choice, explicit name -> number resolution) belongs to
        planning and the create-issue applier boundary (#6769 round 3) — no
        milestone API reads happen here.
        """
        from ..domain.models import TriageFacts

        watch_label = self._get_triage_watch_label()
        batch_armed = bool(watch_label)
        triage_agent_configured = bool(self.config.triage_review_agent)
        health_armed = health_review_interval_minutes(self.config) > 0

        # The act-level PROPOSAL machinery is armed by having a triage agent, so
        # it reconciles INDEPENDENT of the batch review threshold (#6779 R12):
        # approved gated proposals must execute and terminal/absent proposals
        # must be surfaced for cleanup even when threshold=0 (batch disabled).
        # The local op ledger (no GitHub call) says whether there is anything to
        # reconcile — an empty ledger produces no facts and no scan.
        ops = (
            dict(self.triage_authority.list_ops())
            if triage_agent_configured and self.triage_authority is not None
            else {}
        )
        if not batch_armed and not health_armed and not ops:
            return None

        due = health_review_due(
            self.config, state, time.time() if now is None else now
        )

        existing_triage_issue: Optional[int] = None
        existing_health_review_issue: Optional[int] = None
        approved_ops: tuple["ApprovedTriageOp", ...] = ()
        absent_op_candidates: tuple[int, ...] = ()
        case_files: tuple["TriageCaseFileSummary", ...] = ()
        # Distinguishes "scan ran and observed no case files" from "scan was
        # skipped this tick" so the board projection is only replaced when the
        # anchor scan actually observed the ledger (#6781 R2). A frugal tick
        # (health armed but not due, no batch, empty ledger) leaves this False
        # and its empty ``case_files`` must NOT wipe the retained projection.
        case_files_scanned = False
        if batch_armed or ops or due:
            # The ONE exhaustive open triage-agent scan classifies batch +
            # health anchors, open proposals, approved ops, and absent-ledger
            # cleanup candidates in a single reconcile (#6778/#6779 R2/R4/R12).
            # It runs when the batch trigger is armed OR the ledger has ops to
            # reconcile — decoupling proposal advancement from the batch
            # threshold. A due health review also needs this scan so its board
            # snapshot includes every open pattern case file (#6781).
            (
                batch_anchor,
                existing_health_review_issue,
                approved_ops,
                absent_op_candidates,
                case_files,
            ) = self._classify_triage_anchor_scan(ops)
            case_files_scanned = True
            # Batch anchor classification stays gated on batch_armed: a batch
            # anchor is meaningless while the batch trigger is off.
            if batch_armed:
                existing_triage_issue = batch_anchor
        prs = self._fetch_triage_prs(watch_label) if batch_armed else []
        all_labels, source_milestones = self._collect_pr_metadata(prs)

        facts = TriageFacts(
            pr_count=len(prs),
            threshold=self.config.triage_review_threshold,
            existing_triage_issue=existing_triage_issue,
            watch_label=watch_label or "",
            prs=tuple((pr.number, pr.title) for pr in prs),
            source_labels=frozenset(all_labels),
            source_milestones=tuple(source_milestones),
            health_review_due=due,
            existing_health_review_issue=existing_health_review_issue,
            approved_triage_ops=approved_ops,
            absent_proposal_op_candidates=absent_op_candidates,
            open_case_files=case_files,
            case_files_scanned=case_files_scanned,
        )
        if self.board_publisher is not None:
            self.board_publisher.publish(
                facts, last_health_review_at=state.last_health_review_at
            )
        return facts

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

    def _classify_triage_anchor_scan(
        self,
        ops: Mapping[int, "StoredTriageOp"],
    ) -> tuple[
        int | None,
        int | None,
        tuple["ApprovedTriageOp", ...],
        tuple[int, ...],
        tuple["TriageCaseFileSummary", ...],
    ]:
        """Classify the ONE shared, exhaustive open triage-agent scan.

        The scoped/exhaustive anchor-discovery owner backs both this path and
        startup recovery, so both apply ONE eligibility rule (#6763 finding 7)
        over the COMPLETE open set (#6779 R4). Gated proposal issues carry the
        triage agent label, so the SAME scan that finds batch/health anchors
        classifies them (#6778): gate-labeled issues are open proposals
        (excluded from anchor classification), and op-backed issues WITHOUT
        the gate label were approved by the operator. A backlog of proposals
        can never hide an older approved op or an anchor.

        ``ops`` is the caller-provided local authority-store ledger (the caller
        already read it to decide whether a scan is worthwhile — #6779 R12), so
        no extra GitHub call is made here beyond the single anchor scan.

        Fact gathering is READ-ONLY (#6779 R10): reconciliation only
        CLASSIFIES ledger rows absent from the scan as terminal-cleanup
        CANDIDATES; the numbers are returned as a fact for the planner to turn
        into a confirm-and-discard action, never mutated here.
        Observation-labeled issues are pattern case files (#6781), summarized
        for the board snapshot and excluded before anchor classification.
        """
        from .triage_case_files import split_triage_case_file_issues
        from .triage_proposals import reconcile_triage_proposals

        if not self.config.triage_review_agent:
            return None, None, (), (), ()
        existing = discover_open_triage_anchor_issues(
            self.repository_host, self.config
        )
        reconciled = reconcile_triage_proposals(existing, ops=ops)
        remaining, case_files = split_triage_case_file_issues(
            reconciled.anchor_candidate_issues
        )
        batch, health = classify_triage_anchor_issues(
            remaining, self.config.filtering.label
        )
        return (
            batch,
            health,
            reconciled.approved,
            reconciled.absent_op_issue_numbers,
            case_files,
        )

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
            held_issue_numbers=failure_investigation_hold_issue_numbers(
                state, self.config
            ),
        )


def failure_investigation_hold_issue_numbers(
    state: "OrchestratorState", config: Config
) -> frozenset[int]:
    """Issues whose failed-session run assets must be held from cleanup.

    Owner of the single lifecycle rule for #6771 round 3: a failed session
    records its ``ImmediateCleanup`` in the same pass that records the
    ``DiscoveredFailure``, but the failure investigation launches on a LATER
    tick — removing the worktree first deletes every artifact hint the
    investigation was queued to read. The rule is evaluated fresh from state
    at both consuming seams (``gather_cleanup_facts`` so the Planner skips
    held cleanups, and ``clear_discovered_facts`` so held entries survive the
    end-of-tick fact clear). Hold while the failure is still referenced by:

    - a failure discovered this tick (triage-on-failure will queue it),
    - a queued (pending) failure investigation, or
    - an active triage session investigating the issue.

    The hold releases by re-evaluation, with no dedicated release seam: once
    the investigation completes — or is dropped on exhaustion, or its queue
    action fails — none of the conditions match and the retained cleanup is
    planned normally on the next tick.
    """
    from ..domain.triage_session import TriageSessionFlavor
    from .triage_session_policy import is_triage_session

    if not (config.triage_review_on_failure and config.triage_review_agent):
        return frozenset()
    held = {failure.issue_number for failure in state.discovered_failures}
    held.update(
        item.issue_number
        for item in state.pending_triage_reviews
        if item.flavor is TriageSessionFlavor.FAILURE_INVESTIGATION
    )
    held.update(
        session.issue.number
        for session in state.active_sessions
        if is_triage_session(config.triage_review_agent, session.issue.agent_type)
    )
    return frozenset(held)


# Tick-scoped fact buffers: recorded by discovery/completion seams, consumed
# by one planning pass, cleared after the plan is applied.
_DISCOVERED_FACT_ATTRS: tuple[str, ...] = (
    "discovered_reviews",
    "discovered_retrospective_reviews",
    "discovered_awaiting_merge_reconciliations",
    "discovered_awaiting_merge_drifts",
    "discovered_awaiting_merge_escalations",
    "discovered_merge_queue_enqueues",
    "discovered_reworks",
    "discovered_escalations",
    "discovered_failures",
    "immediate_cleanups",
)


def clear_discovered_facts(state: "OrchestratorState", config: Config) -> None:
    """Clear tick-scoped fact buffers, retaining held immediate cleanups.

    Immediate cleanups referenced by a pending/active failure investigation
    are retained across the clear (#6771 round 3): the Planner skipped them
    this tick via ``CleanupFacts.held_issue_numbers``, and dropping them here
    would leak the worktree forever once the hold releases.
    """
    held = failure_investigation_hold_issue_numbers(state, config)
    retained = [c for c in state.immediate_cleanups if c.issue_number in held]
    for attr in _DISCOVERED_FACT_ATTRS:
        getattr(state, attr).clear()
    state.immediate_cleanups.extend(retained)
