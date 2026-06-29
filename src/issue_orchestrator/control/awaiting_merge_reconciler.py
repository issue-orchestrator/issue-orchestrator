"""Reconcile history-backed awaiting-merge entries with repository state."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Literal
from urllib.parse import urlparse

from ..domain.models import (
    AwaitingMergeReconciliationSource,
    AwaitingMergeTerminalStatus,
    DiscoveredAwaitingMergeDrift,
    DiscoveredAwaitingMergeEscalation,
    DiscoveredAwaitingMergeReconciliation,
    DiscoveredRework,
    PostPublishEscalationKind,
    RECONCILABLE_HISTORY_STATUSES,
    TERMINAL_AWAITING_MERGE_HISTORY_STATUSES,
)
from ..history import latest_history_entries_by_issue
from ..ports.repository_host import RepositoryHostError
from .queue_cache import record_issue_refreshes

if TYPE_CHECKING:
    from ..domain.models import OrchestratorState, SessionHistoryEntry
    from ..ports.issue import Issue
    from ..ports.pull_request_tracker import PRInfo
    from ..ports.repository_host import RepositoryHost
    from .label_manager import LabelManager


logger = logging.getLogger(__name__)

AWAITING_MERGE_HISTORY_LIMIT = 50
AWAITING_MERGE_LABEL_DRIFT_SCAN_INTERVAL_SECONDS = 300.0

ReconciliationOutcome = Literal["terminal", "still_pending", "skipped"]
POST_PUBLISH_VALIDATION_SOURCE = "post_publish_validation"
POST_PUBLISH_VALIDATION_COMMENT_MARKER = "<!-- io:post-publish-validation -->"


# Result of classifying GitHub merge readiness after reviewer approval.
# `mergeable_state` says merge readiness; `status_check_rollup` says
# check truth. Combining them disambiguates "checks still running"
# (transient — wait) from "a check actually failed" (real — rework).
PostApprovalAction = Literal[
    "READY",                # clean — proceed to merge
    "WAIT_FOR_CHECKS",      # unstable/blocked + checks PENDING/EXPECTED/unknown
    "REWORK_CONFLICT",      # dirty — merge conflict against base
    "REWORK_BEHIND",        # behind — branch out of date with base
    "REWORK_CHECK_FAILED",  # unstable/blocked + checks FAILURE/ERROR
    "BLOCKED_TERMINAL",     # blocked + checks SUCCESS — branch protection
    "UNKNOWN",              # has_hooks / draft / "" / unrecognized state
]


def classify_post_approval_state(pr: PRInfo) -> PostApprovalAction:
    """Decide what to do with a reviewer-approved PR based on GitHub state.

    See PostApprovalAction docstring for the full table. Pure function — no
    I/O, no clock — so callers can unit-test the dispatch matrix exhaustively.
    """
    state = _normalized_state(pr.mergeable_state)
    rollup = pr.status_check_rollup
    if state == "clean":
        return "READY"
    if state == "dirty":
        return "REWORK_CONFLICT"
    if state == "behind":
        return "REWORK_BEHIND"
    if state in ("unstable", "blocked"):
        if rollup in ("FAILURE", "ERROR"):
            return "REWORK_CHECK_FAILED"
        if rollup == "SUCCESS":
            # blocked + all-green → branch protection (CODEOWNERS, approvals,
            # required signatures) — code rework won't unstick this.
            if state == "blocked":
                return "BLOCKED_TERMINAL"
            # unstable + SUCCESS is unusual; GitHub usually resolves it to
            # `clean` on the next poll. Treat as transient.
            return "WAIT_FOR_CHECKS"
        # rollup in {PENDING, EXPECTED, None} → checks not yet conclusive
        return "WAIT_FOR_CHECKS"
    return "UNKNOWN"


# Actions that require sending the PR back to a coder agent.
_REWORK_ACTIONS: frozenset[PostApprovalAction] = frozenset(
    {"REWORK_CONFLICT", "REWORK_BEHIND", "REWORK_CHECK_FAILED"}
)


@dataclass(frozen=True)
class AwaitingMergeEntryDiscovery:
    """Discovery result for one awaiting-merge history entry."""

    outcome: ReconciliationOutcome
    reconciliation: DiscoveredAwaitingMergeReconciliation | None = None
    drift: DiscoveredAwaitingMergeDrift | None = None
    rework: DiscoveredRework | None = None
    escalation: DiscoveredAwaitingMergeEscalation | None = None


@dataclass(frozen=True)
class AwaitingMergeReconciliationResult:
    """Summary of awaiting-merge reconciliation discovery work."""

    checked: int = 0
    discovered: int = 0
    drift_discovered: int = 0
    rework_discovered: int = 0
    escalation_discovered: int = 0
    still_pending: int = 0
    skipped: int = 0
    reconciliations: tuple[DiscoveredAwaitingMergeReconciliation, ...] = ()
    drifts: tuple[DiscoveredAwaitingMergeDrift, ...] = ()
    reworks: tuple[DiscoveredRework, ...] = ()
    escalations: tuple[DiscoveredAwaitingMergeEscalation, ...] = ()


DEFAULT_POST_PUBLISH_CHECKS_PENDING_TIMEOUT_SECONDS = 1800.0


@dataclass
class AwaitingMergeReconciler:
    """Discovers lifecycle cleanup facts for history-backed awaiting-merge cards."""

    repository_host: RepositoryHost
    label_manager: LabelManager | None = None
    clock: Callable[[], float] = time.time
    history_limit: int = AWAITING_MERGE_HISTORY_LIMIT
    label_drift_scan_interval_seconds: float = (
        AWAITING_MERGE_LABEL_DRIFT_SCAN_INTERVAL_SECONDS
    )
    # Wall-clock budget for WAIT_FOR_CHECKS before escalating. Default
    # mirrors Config.post_publish_checks_pending_timeout_seconds; callers
    # in production wire the configured value through.
    post_publish_checks_pending_timeout_seconds: float = (
        DEFAULT_POST_PUBLISH_CHECKS_PENDING_TIMEOUT_SECONDS
    )

    def discover(self, state: OrchestratorState) -> AwaitingMergeReconciliationResult:
        """Discover completed history entries that should become terminal."""
        checked = 0
        discovered = 0
        drift_discovered = 0
        rework_discovered = 0
        escalation_discovered = 0
        still_pending = 0
        skipped = 0
        reconciliations: list[DiscoveredAwaitingMergeReconciliation] = []
        drifts: list[DiscoveredAwaitingMergeDrift] = []
        reworks: list[DiscoveredRework] = []
        escalations: list[DiscoveredAwaitingMergeEscalation] = []
        pending_issue_numbers: set[int] = set()

        candidates = self._awaiting_merge_entries(state)
        logger.debug(
            "Awaiting-merge scan: %d candidate entries (history_limit=%d)",
            len(candidates),
            self.history_limit,
        )
        for entry in candidates:
            checked += 1
            discovery = self._discover_entry(state, entry)
            if discovery.outcome == "terminal":
                discovered += 1
                if discovery.reconciliation is not None:
                    reconciliations.append(discovery.reconciliation)
            elif discovery.outcome == "still_pending":
                still_pending += 1
                pending_issue_numbers.add(entry.issue_number)
            else:
                skipped += 1
            if discovery.drift is not None:
                drift_discovered += 1
                drifts.append(discovery.drift)
            if discovery.rework is not None:
                rework_discovered += 1
                reworks.append(discovery.rework)
            if discovery.escalation is not None:
                escalation_discovered += 1
                escalations.append(discovery.escalation)

        label_drifts = self._discover_label_drifts(
            state,
            excluded_issue_numbers=pending_issue_numbers
            | {drift.issue_number for drift in drifts},
        )
        drift_discovered += len(label_drifts)
        drifts.extend(label_drifts)

        logger.debug(
            "Awaiting-merge scan complete: checked=%d terminal=%d drift=%d "
            "still_pending=%d skipped=%d escalations=%d",
            checked, discovered, drift_discovered,
            still_pending, skipped, escalation_discovered,
        )
        return AwaitingMergeReconciliationResult(
            checked=checked,
            discovered=discovered,
            drift_discovered=drift_discovered,
            rework_discovered=rework_discovered,
            escalation_discovered=escalation_discovered,
            still_pending=still_pending,
            skipped=skipped,
            reconciliations=tuple(reconciliations),
            drifts=tuple(drifts),
            reworks=tuple(reworks),
            escalations=tuple(escalations),
        )

    def _awaiting_merge_entries(
        self, state: OrchestratorState
    ) -> list[SessionHistoryEntry]:
        return [
            entry
            for entry in latest_history_entries_by_issue(
                state.session_history,
                limit=self.history_limit,
            )
            if entry.status in RECONCILABLE_HISTORY_STATUSES and bool(entry.pr_url)
        ]

    def _discover_entry(
        self,
        state: OrchestratorState,
        entry: SessionHistoryEntry,
    ) -> AwaitingMergeEntryDiscovery:
        pr_number = pr_number_from_url(entry.pr_url or "")
        if pr_number is None:
            logger.warning(
                "Cannot reconcile awaiting-merge history for issue #%d: invalid PR URL %r",
                entry.issue_number,
                entry.pr_url,
            )
            return AwaitingMergeEntryDiscovery("skipped")

        pr = self._get_pr(entry.issue_number, pr_number)
        if pr is not None:
            pr_state = _normalized_state(pr.state)
            logger.debug(
                "Awaiting-merge entry issue=#%d pr=#%d state=%r terminal=%s pr_url=%s",
                entry.issue_number,
                pr_number,
                pr_state,
                pr_state in TERMINAL_AWAITING_MERGE_HISTORY_STATUSES,
                entry.pr_url,
            )
            if pr_state in TERMINAL_AWAITING_MERGE_HISTORY_STATUSES:
                logger.info(
                    "Awaiting-merge terminal via PR: issue=#%d pr=#%d state=%s",
                    entry.issue_number,
                    pr_number,
                    pr_state,
                )
                # PR is terminal — drop any pending-checks bookkeeping so
                # the dict doesn't leak across PR lifecycles.
                state.awaiting_merge_checks_pending_since.pop(
                    entry.issue_number, None,
                )
                drift = None
                if pr_state == "closed":
                    drift = self._discover_terminal_pr_issue_drift(
                        state=state,
                        entry=entry,
                        pr=pr,
                        pr_number=pr_number,
                    )
                return AwaitingMergeEntryDiscovery(
                    "terminal",
                    reconciliation=_reconciliation_fact(
                        entry=entry,
                        pr_number=pr_number,
                        status=pr_state,
                        reason=_pr_terminal_reason(pr_state),
                        source="pull_request",
                    ),
                    drift=drift,
                )
        else:
            logger.debug(
                "Awaiting-merge PR fetch returned None: issue=#%d pr=#%d pr_url=%s",
                entry.issue_number,
                pr_number,
                entry.pr_url,
            )

        issue = self._get_issue(entry.issue_number)
        if issue is None:
            # An open PR still means "awaiting merge"; only bump issue freshness
            # after a confirmed issue refresh.
            if pr is not None:
                return AwaitingMergeEntryDiscovery("still_pending")
            return AwaitingMergeEntryDiscovery("skipped")

        record_issue_refreshes(state, {entry.issue_number}, self.clock())
        if _normalized_state(issue.state) == "closed":
            logger.info(
                "Awaiting-merge terminal via issue closure: issue=#%d pr=#%d",
                entry.issue_number,
                pr_number,
            )
            # Issue terminated — drop pending-checks bookkeeping.
            state.awaiting_merge_checks_pending_since.pop(
                entry.issue_number, None,
            )
            return AwaitingMergeEntryDiscovery(
                "terminal",
                reconciliation=_reconciliation_fact(
                    entry=entry,
                    pr_number=pr_number,
                    status="closed",
                    reason="Issue closed; awaiting merge reconciled",
                    source="issue",
                ),
            )

        if pr is None:
            return AwaitingMergeEntryDiscovery("skipped")
        rework, escalation = self._discover_post_publish_followup(
            state=state,
            entry=entry,
            pr=pr,
            issue=issue,
            pr_number=pr_number,
        )
        return AwaitingMergeEntryDiscovery(
            "still_pending",
            rework=rework,
            escalation=escalation,
        )

    def _discover_terminal_pr_issue_drift(
        self,
        *,
        state: OrchestratorState,
        entry: SessionHistoryEntry,
        pr: PRInfo,
        pr_number: int,
    ) -> DiscoveredAwaitingMergeDrift | None:
        if self.label_manager is None:
            return None
        try:
            issue = self._get_issue(entry.issue_number)
        except RepositoryHostError:
            logger.warning(
                "Unable to check issue state for closed PR drift: issue=#%d pr=#%d",
                entry.issue_number,
                pr_number,
            )
            return None
        if issue is None:
            return None

        record_issue_refreshes(state, {entry.issue_number}, self.clock())
        if _normalized_state(issue.state) == "closed":
            return None
        if not self.label_manager.is_pr_pending(issue.labels):
            return None

        return _drift_fact(
            issue_number=entry.issue_number,
            pr=pr,
            status_reason="PR closed; issue remains open",
        )

    def _discover_label_drifts(
        self,
        state: OrchestratorState,
        *,
        excluded_issue_numbers: set[int],
    ) -> list[DiscoveredAwaitingMergeDrift]:
        if self.label_manager is None:
            return []

        active_issue_numbers = {session.issue.number for session in state.active_sessions}
        now = self.clock()
        drifts: list[DiscoveredAwaitingMergeDrift] = []
        for issue in _unique_cached_issues(state):
            if not self._should_scan_label_drift_issue(
                state=state,
                issue=issue,
                active_issue_numbers=active_issue_numbers,
                excluded_issue_numbers=excluded_issue_numbers,
                now=now,
            ):
                continue

            drift = self._discover_label_drift_for_issue(
                state=state,
                issue=issue,
                scanned_at=now,
            )
            if drift is not None:
                drifts.append(drift)

        return drifts

    def _should_scan_label_drift_issue(
        self,
        *,
        state: OrchestratorState,
        issue: Issue,
        active_issue_numbers: set[int],
        excluded_issue_numbers: set[int],
        now: float,
    ) -> bool:
        if self.label_manager is None:
            return False
        if issue.number in excluded_issue_numbers or issue.number in active_issue_numbers:
            return False
        if _normalized_state(issue.state) == "closed":
            return False
        if not self.label_manager.is_pr_pending(issue.labels):
            return False
        return not _recent_label_drift_scan(
            state=state,
            issue_number=issue.number,
            now=now,
            interval_seconds=self.label_drift_scan_interval_seconds,
        )

    def _discover_label_drift_for_issue(
        self,
        *,
        state: OrchestratorState,
        issue: Issue,
        scanned_at: float,
    ) -> DiscoveredAwaitingMergeDrift | None:
        state.awaiting_merge_drift_scan_timestamps[issue.number] = scanned_at
        try:
            prs = self._get_prs_for_issue(issue.number)
        except RepositoryHostError:
            return None

        if any(_normalized_state(pr.state) == "open" for pr in prs):
            return None

        closed_prs = [
            pr for pr in prs if _normalized_state(pr.state) == "closed"
        ]
        if closed_prs:
            pr = max(closed_prs, key=lambda item: item.number)
            return _drift_fact(
                issue_number=issue.number,
                pr=pr,
                status_reason="PR closed; issue remains open",
            )
        if not prs:
            return DiscoveredAwaitingMergeDrift(
                issue_number=issue.number,
                pr_number=0,
                pr_url="",
                status_reason="PR missing; issue remains open",
            )
        return None

    def _discover_post_publish_followup(
        self,
        *,
        state: OrchestratorState,
        entry: SessionHistoryEntry,
        pr: PRInfo,
        issue: Issue,
        pr_number: int,
    ) -> tuple[DiscoveredRework | None, DiscoveredAwaitingMergeEscalation | None]:
        """Decide what (if anything) to do with an approved-but-not-merged PR.

        Returns ``(rework, escalation)`` — at most one is non-None on any
        given tick. Reads as a table of contents: clear stale bookkeeping
        if the PR is no longer in the post-approval state, gate, classify,
        clear bookkeeping if the classifier moved on, then dispatch.
        """
        if not self._post_publish_eligible(state, entry, pr):
            # Eligibility lost (label dropped, needs_rework added, an
            # active session/pending rework appeared) — clear any stale
            # WAIT_FOR_CHECKS timestamp so a future re-approval starts
            # a fresh wait budget instead of inheriting an old one. Without
            # this, a long gap (commit → label drop → days later → re-
            # approve) would compute `elapsed >> timeout` and escalate
            # immediately, defeating the timeout entirely.
            state.awaiting_merge_checks_pending_since.pop(entry.issue_number, None)
            return None, None

        action = classify_post_approval_state(pr)
        logger.debug(
            "Awaiting-merge classify: issue=#%d pr=#%d state=%s rollup=%s action=%s",
            entry.issue_number,
            pr_number,
            _normalized_state(pr.mergeable_state),
            pr.status_check_rollup,
            action,
        )
        if action != "WAIT_FOR_CHECKS":
            state.awaiting_merge_checks_pending_since.pop(entry.issue_number, None)

        if action in _REWORK_ACTIONS:
            return self._build_rework_discovery(
                pr=pr, action=action, entry=entry, issue=issue, pr_number=pr_number,
            ), None
        if action == "BLOCKED_TERMINAL":
            return None, self._build_branch_protection_escalation(
                pr=pr,
                issue_number=entry.issue_number,
                issue_key=issue.key.stable_id(),
                pr_number=pr_number,
            )
        if action == "WAIT_FOR_CHECKS":
            return None, self._maybe_escalate_pending_checks(
                state=state, pr=pr,
                issue_number=entry.issue_number,
                issue_key=issue.key.stable_id(),
                pr_number=pr_number,
            )
        # READY / UNKNOWN — nothing to do.
        return None, None

    def _post_publish_eligible(
        self,
        state: OrchestratorState,
        entry: SessionHistoryEntry,
        pr: PRInfo,
    ) -> bool:
        """Pre-filter: only consider PRs that are reviewer-approved and not
        already in flight via another rework path."""
        if self.label_manager is None:
            return False
        if self.label_manager.code_reviewed not in pr.labels:
            return False
        terminal_labels = (self.label_manager.needs_human, self.label_manager.needs_rework)
        if any(label in pr.labels for label in terminal_labels):
            return False
        if any(s.issue.number == entry.issue_number for s in state.active_sessions):
            return False
        if any(
            p.resolve_issue_number() == entry.issue_number
            for p in state.pending_reworks
        ):
            return False
        return True

    def _build_rework_discovery(
        self,
        *,
        pr: PRInfo,
        action: PostApprovalAction,
        entry: SessionHistoryEntry,
        issue: Issue,
        pr_number: int,
    ) -> DiscoveredRework:
        assert self.label_manager is not None
        assert issue.agent_type is not None
        return DiscoveredRework(
            issue_number=entry.issue_number,
            pr_number=pr_number,
            branch_name=pr.branch,
            agent_type=issue.agent_type,
            rework_cycle=_next_rework_cycle(pr.labels, self.label_manager),
            source=POST_PUBLISH_VALIDATION_SOURCE,
            feedback=_build_rework_feedback(pr, action),
            feedback_comment_already_posted=self._post_publish_comment_present(
                pr_number
            ),
        )

    def _post_publish_comment_present(self, pr_number: int) -> bool:
        """Return True if the PR already carries the post-publish marker comment.

        Read-only dedupe guard: if a prior tick posted the feedback comment but
        failed to apply the ``needs_rework`` label (leaving the PR eligible for
        re-discovery), the planner would otherwise stack a duplicate comment.
        Only reached on the rare rework-discovery path, so the extra read is
        bounded.
        """
        return any(
            POST_PUBLISH_VALIDATION_COMMENT_MARKER in _comment_body(comment)
            for comment in self._get_pr_comments(pr_number)
        )

    def _build_branch_protection_escalation(
        self,
        *,
        pr: PRInfo,
        issue_number: int,
        issue_key: str,
        pr_number: int,
    ) -> DiscoveredAwaitingMergeEscalation:
        # blocked + SUCCESS rollup: branch protection (CODEOWNERS,
        # approvals, signatures) blocks merge. Code rework can't help —
        # escalate immediately, no timeout.
        assert self.label_manager is not None
        return _build_escalation(
            pr=pr,
            issue_number=issue_number,
            issue_key=issue_key,
            pr_number=pr_number,
            label_manager=self.label_manager,
            kind="branch_protection_blocked",
            reason=(
                "Branch protection blocks merge despite all required "
                "checks passing — likely missing approvals, CODEOWNERS "
                "sign-off, or required signatures. Code rework cannot "
                "unstick this."
            ),
        )

    def _maybe_escalate_pending_checks(
        self,
        *,
        state: OrchestratorState,
        pr: PRInfo,
        issue_number: int,
        issue_key: str,
        pr_number: int,
    ) -> DiscoveredAwaitingMergeEscalation | None:
        """Run the WAIT_FOR_CHECKS timeout state machine for one PR.

        Returns an escalation only when the budget has been exceeded;
        otherwise returns None and (when first observed) records the
        timestamp on state so subsequent ticks can compute elapsed.
        """
        assert self.label_manager is not None
        now = self.clock()
        first_seen = state.awaiting_merge_checks_pending_since.get(issue_number)
        if first_seen is None:
            state.awaiting_merge_checks_pending_since[issue_number] = now
            return None
        elapsed = now - first_seen
        if elapsed < self.post_publish_checks_pending_timeout_seconds:
            return None
        minutes = max(1, int(elapsed // 60))
        timeout_minutes = int(
            self.post_publish_checks_pending_timeout_seconds // 60
        )
        return _build_escalation(
            pr=pr,
            issue_number=issue_number,
            issue_key=issue_key,
            pr_number=pr_number,
            label_manager=self.label_manager,
            kind="checks_pending_timeout",
            reason=(
                f"Required GitHub checks have been pending for "
                f"~{minutes} minute(s) since reviewer approval "
                f"(timeout: {timeout_minutes} minutes). The orchestrator "
                f"has stopped waiting and is handing the PR back for "
                f"human attention."
            ),
        )

    def _get_pr(self, issue_number: int, pr_number: int) -> PRInfo | None:
        # The post-publish classifier needs `status_check_rollup` to
        # distinguish "checks running" from "check failed", so this is
        # the one call site that pays the extra GraphQL round-trip.
        # Other lifecycle paths use `get_pr` (REST-only) and never
        # touch the rollup.
        try:
            return self.repository_host.get_pr_with_status_check_rollup(pr_number)
        except RepositoryHostError as exc:
            logger.warning(
                "Failed to refresh PR #%d for awaiting-merge issue #%d: %s",
                pr_number,
                issue_number,
                exc,
            )
            raise

    def _get_issue(self, issue_number: int) -> Issue | None:
        try:
            return self.repository_host.get_issue(issue_number)
        except RepositoryHostError as exc:
            logger.warning(
                "Failed to refresh awaiting-merge issue #%d: %s",
                issue_number,
                exc,
            )
            raise

    def _get_prs_for_issue(self, issue_number: int) -> list[PRInfo]:
        try:
            return self.repository_host.get_prs_for_issue(issue_number, state="all")
        except RepositoryHostError as exc:
            logger.warning(
                "Failed to scan PRs for awaiting-merge label drift on issue #%d: %s",
                issue_number,
                exc,
            )
            raise

    def _get_pr_comments(self, pr_number: int) -> list[dict[str, Any]]:
        try:
            return self.repository_host.get_issue_comments(pr_number)
        except RepositoryHostError as exc:
            logger.warning(
                "Failed to read comments for awaiting-merge PR #%d: %s",
                pr_number,
                exc,
            )
            raise


def pr_number_from_url(pr_url: str) -> int | None:
    """Extract a PR number from a GitHub-style pull request URL."""
    segments = [segment for segment in urlparse(pr_url).path.split("/") if segment]
    for index, segment in enumerate(segments[:-1]):
        if segment == "pull":
            try:
                return int(segments[index + 1])
            except ValueError:
                continue
    return None


def _reconciliation_fact(
    *,
    entry: SessionHistoryEntry,
    pr_number: int,
    status: AwaitingMergeTerminalStatus,
    reason: str,
    source: AwaitingMergeReconciliationSource,
) -> DiscoveredAwaitingMergeReconciliation:
    return DiscoveredAwaitingMergeReconciliation(
        issue_number=entry.issue_number,
        pr_number=pr_number,
        pr_url=entry.pr_url or "",
        status=status,
        status_reason=reason,
        source=source,
    )


def _drift_fact(
    *,
    issue_number: int,
    pr: PRInfo,
    status_reason: str,
) -> DiscoveredAwaitingMergeDrift:
    return DiscoveredAwaitingMergeDrift(
        issue_number=issue_number,
        pr_number=pr.number,
        pr_url=pr.url,
        status_reason=status_reason,
    )


def _unique_cached_issues(state: OrchestratorState) -> list[Issue]:
    issues_by_number: dict[int, Issue] = {}
    for issue in [*state.cached_scope_issues, *state.cached_queue_issues]:
        issues_by_number[issue.number] = issue
    return list(issues_by_number.values())


def _recent_label_drift_scan(
    *,
    state: OrchestratorState,
    issue_number: int,
    now: float,
    interval_seconds: float,
) -> bool:
    last_scanned_at = state.awaiting_merge_drift_scan_timestamps.get(issue_number, 0.0)
    return last_scanned_at > 0 and (now - last_scanned_at) < interval_seconds


def _pr_terminal_reason(status: AwaitingMergeTerminalStatus) -> str:
    if status == "merged":
        return "PR merged; awaiting merge reconciled"
    return "PR closed; awaiting merge reconciled"


def _normalized_state(state: str | None) -> str:
    return (state or "").strip().lower()


def _next_rework_cycle(labels: list[str], label_manager: LabelManager) -> int:
    cycle = label_manager.extract_rework_cycle(labels)
    if cycle is not None:
        return cycle + 1
    return 1


_REWORK_HEADERS: dict[PostApprovalAction, tuple[str, str, str]] = {
    "REWORK_CONFLICT": (
        "Merge conflict against base branch",
        "GitHub reports merge conflicts against the base branch.",
        "Rebase or merge the base branch and resolve the conflicts, "
        "then push so the PR is mergeable again.",
    ),
    "REWORK_BEHIND": (
        "Branch is behind base branch",
        "GitHub reports the branch is behind the base branch and "
        "branch protection requires it to be up-to-date before merge.",
        "Rebase (or merge) the base branch into this branch and push, "
        "so the PR is mergeable again.",
    ),
    "REWORK_CHECK_FAILED": (
        "Required check failed on this PR",
        "A required status check has FAILED or ERRORED on this PR's "
        "head commit. The reviewer already approved, but a CI/check "
        "regression is now blocking merge.",
        "Open the PR's checks tab to identify the failing check, "
        "reproduce locally, fix the underlying problem, and push "
        "so the checks turn green.",
    ),
}


def _build_rework_feedback(pr: PRInfo, action: PostApprovalAction) -> str:
    # Caller (`_discover_post_publish_followup`) only invokes this for
    # actions in `_REWORK_ACTIONS`, and `_REWORK_HEADERS` covers exactly
    # those. A KeyError here means the dispatch has drifted and we want
    # to crash loudly, not paper over it with a generic fallback.
    title, detail, guidance = _REWORK_HEADERS[action]
    state = _normalized_state(pr.mergeable_state) or "unknown"
    rollup = pr.status_check_rollup or "n/a"
    lines = [
        f"{title} (cycle handled by post-publish gate, not the reviewer):",
        "",
        f"PR #{pr.number} was approved by the reviewer but is no longer "
        "ready to merge.",
        f"- URL: {pr.url}",
        f"- Branch: {pr.branch}",
        f"- Mergeability: {state}",
        f"- Status checks: {rollup}",
        f"- Diagnosis: {detail}",
        "",
        guidance,
    ]
    return "\n".join(lines)


def build_post_publish_validation_comment(feedback: str) -> str:
    return f"{POST_PUBLISH_VALIDATION_COMMENT_MARKER}\n{feedback}"


def _comment_body(comment: dict[str, Any]) -> str:
    body = comment.get("body")
    return body if isinstance(body, str) else ""


def _build_escalation(
    *,
    pr: PRInfo,
    issue_number: int,
    issue_key: str,
    pr_number: int,
    label_manager: LabelManager,
    kind: PostPublishEscalationKind,
    reason: str,
) -> DiscoveredAwaitingMergeEscalation:
    return DiscoveredAwaitingMergeEscalation(
        issue_number=issue_number,
        pr_number=pr_number,
        pr_url=pr.url,
        issue_key=issue_key,
        rework_cycle=_next_rework_cycle(pr.labels, label_manager),
        kind=kind,
        reason=reason,
    )
