"""Reconcile history-backed awaiting-merge entries with repository state."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Literal
from urllib.parse import urlparse

from ..domain.models import (
    AwaitingMergeReconciliationSource,
    AwaitingMergeTerminalStatus,
    DiscoveredAwaitingMergeReconciliation,
    DiscoveredRework,
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

ReconciliationOutcome = Literal["terminal", "still_pending", "skipped"]
POST_PUBLISH_VALIDATION_REWORK_STATES = frozenset({"dirty", "behind", "unstable"})
POST_PUBLISH_VALIDATION_SOURCE = "post_publish_validation"
POST_PUBLISH_VALIDATION_COMMENT_MARKER = "<!-- io:post-publish-validation -->"


@dataclass(frozen=True)
class AwaitingMergeEntryDiscovery:
    """Discovery result for one awaiting-merge history entry."""

    outcome: ReconciliationOutcome
    reconciliation: DiscoveredAwaitingMergeReconciliation | None = None
    rework: DiscoveredRework | None = None


@dataclass(frozen=True)
class AwaitingMergeReconciliationResult:
    """Summary of awaiting-merge reconciliation discovery work."""

    checked: int = 0
    discovered: int = 0
    rework_discovered: int = 0
    still_pending: int = 0
    skipped: int = 0
    reconciliations: tuple[DiscoveredAwaitingMergeReconciliation, ...] = ()
    reworks: tuple[DiscoveredRework, ...] = ()


@dataclass
class AwaitingMergeReconciler:
    """Discovers lifecycle cleanup facts for history-backed awaiting-merge cards."""

    repository_host: RepositoryHost
    label_manager: LabelManager | None = None
    clock: Callable[[], float] = time.time
    history_limit: int = AWAITING_MERGE_HISTORY_LIMIT

    def discover(self, state: OrchestratorState) -> AwaitingMergeReconciliationResult:
        """Discover completed history entries that should become terminal."""
        checked = 0
        discovered = 0
        rework_discovered = 0
        still_pending = 0
        skipped = 0
        reconciliations: list[DiscoveredAwaitingMergeReconciliation] = []
        reworks: list[DiscoveredRework] = []

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
            else:
                skipped += 1
            if discovery.rework is not None:
                rework_discovered += 1
                reworks.append(discovery.rework)

        logger.debug(
            "Awaiting-merge scan complete: checked=%d terminal=%d still_pending=%d skipped=%d",
            checked,
            discovered,
            still_pending,
            skipped,
        )
        return AwaitingMergeReconciliationResult(
            checked=checked,
            discovered=discovered,
            rework_discovered=rework_discovered,
            still_pending=still_pending,
            skipped=skipped,
            reconciliations=tuple(reconciliations),
            reworks=tuple(reworks),
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
                return AwaitingMergeEntryDiscovery(
                    "terminal",
                    reconciliation=_reconciliation_fact(
                        entry=entry,
                        pr_number=pr_number,
                        status=pr_state,
                        reason=_pr_terminal_reason(pr_state),
                        source="pull_request",
                    ),
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
        return AwaitingMergeEntryDiscovery(
            "still_pending",
            rework=self._discover_post_publish_validation_rework(
                state=state,
                entry=entry,
                pr=pr,
                issue=issue,
                pr_number=pr_number,
            ),
        )

    def _discover_post_publish_validation_rework(
        self,
        *,
        state: OrchestratorState,
        entry: SessionHistoryEntry,
        pr: PRInfo,
        issue: Issue,
        pr_number: int,
    ) -> DiscoveredRework | None:
        if self.label_manager is None:
            return None
        if issue.agent_type is None:
            return None
        if self.label_manager.code_reviewed not in pr.labels:
            return None
        if self.label_manager.needs_rework in pr.labels:
            return None
        if any(session.issue.number == entry.issue_number for session in state.active_sessions):
            return None
        if any(
            pending.resolve_issue_number() == entry.issue_number
            for pending in state.pending_reworks
        ):
            return None

        mergeable_state = _normalized_state(pr.mergeable_state)
        if mergeable_state not in POST_PUBLISH_VALIDATION_REWORK_STATES:
            return None

        return DiscoveredRework(
            issue_number=entry.issue_number,
            pr_number=pr_number,
            branch_name=pr.branch,
            agent_type=issue.agent_type,
            rework_cycle=_next_rework_cycle(pr.labels, self.label_manager),
            source=POST_PUBLISH_VALIDATION_SOURCE,
            feedback=_build_post_publish_validation_feedback(pr, mergeable_state),
        )

    def _get_pr(self, issue_number: int, pr_number: int) -> PRInfo | None:
        try:
            return self.repository_host.get_pr(pr_number)
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


def _build_post_publish_validation_feedback(pr: PRInfo, mergeable_state: str) -> str:
    detail_map = {
        "dirty": "GitHub reports merge conflicts against the base branch.",
        "behind": "GitHub reports the branch is behind the base branch and must be updated before merge.",
        "unstable": "GitHub reports failing or unstable required validation on the merge result.",
    }
    lines = [
        "POST-PUBLISH VALIDATION FAILURE (address these issues):",
        "",
        f"PR #{pr.number} is no longer ready to merge after review approval.",
        f"- URL: {pr.url}",
        f"- Branch: {pr.branch}",
        f"- Mergeability: {mergeable_state}",
        f"- Detail: {detail_map.get(mergeable_state, 'GitHub reports the PR is no longer merge-ready.')}",
        "",
        "Update the branch, rerun the required validation, and leave the PR ready for merge again.",
    ]
    return "\n".join(lines)


def build_post_publish_validation_comment(feedback: str) -> str:
    return f"{POST_PUBLISH_VALIDATION_COMMENT_MARKER}\n{feedback}"
