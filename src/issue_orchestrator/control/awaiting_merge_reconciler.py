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


logger = logging.getLogger(__name__)

AWAITING_MERGE_HISTORY_LIMIT = 50

ReconciliationOutcome = Literal["terminal", "still_pending", "skipped"]


@dataclass(frozen=True)
class AwaitingMergeReconciliationResult:
    """Summary of awaiting-merge reconciliation discovery work."""

    checked: int = 0
    discovered: int = 0
    still_pending: int = 0
    skipped: int = 0
    reconciliations: tuple[DiscoveredAwaitingMergeReconciliation, ...] = ()


@dataclass
class AwaitingMergeReconciler:
    """Discovers lifecycle cleanup facts for history-backed awaiting-merge cards."""

    repository_host: RepositoryHost
    clock: Callable[[], float] = time.time
    history_limit: int = AWAITING_MERGE_HISTORY_LIMIT

    def discover(self, state: OrchestratorState) -> AwaitingMergeReconciliationResult:
        """Discover completed history entries that should become terminal."""
        checked = 0
        discovered = 0
        still_pending = 0
        skipped = 0
        reconciliations: list[DiscoveredAwaitingMergeReconciliation] = []

        for entry in self._awaiting_merge_entries(state):
            checked += 1
            outcome, reconciliation = self._discover_entry(state, entry)
            if outcome == "terminal":
                discovered += 1
                if reconciliation is not None:
                    reconciliations.append(reconciliation)
            elif outcome == "still_pending":
                still_pending += 1
            else:
                skipped += 1

        return AwaitingMergeReconciliationResult(
            checked=checked,
            discovered=discovered,
            still_pending=still_pending,
            skipped=skipped,
            reconciliations=tuple(reconciliations),
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
    ) -> tuple[ReconciliationOutcome, DiscoveredAwaitingMergeReconciliation | None]:
        pr_number = pr_number_from_url(entry.pr_url or "")
        if pr_number is None:
            logger.warning(
                "Cannot reconcile awaiting-merge history for issue #%d: invalid PR URL %r",
                entry.issue_number,
                entry.pr_url,
            )
            return "skipped", None

        pr = self._get_pr(entry.issue_number, pr_number)
        if pr is not None:
            pr_state = _normalized_state(pr.state)
            if pr_state in TERMINAL_AWAITING_MERGE_HISTORY_STATUSES:
                return "terminal", _reconciliation_fact(
                    entry=entry,
                    pr_number=pr_number,
                    status=pr_state,
                    reason=_pr_terminal_reason(pr_state),
                    source="pull_request",
                )

        issue = self._get_issue(entry.issue_number)
        if issue is None:
            # An open PR still means "awaiting merge"; only bump issue freshness
            # after a confirmed issue refresh.
            if pr is not None:
                return "still_pending", None
            return "skipped", None

        record_issue_refreshes(state, {entry.issue_number}, self.clock())
        if _normalized_state(issue.state) == "closed":
            return "terminal", _reconciliation_fact(
                entry=entry,
                pr_number=pr_number,
                status="closed",
                reason="Issue closed; awaiting merge reconciled",
                source="issue",
            )

        if pr is None:
            return "skipped", None
        return "still_pending", None

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
            return None

    def _get_issue(self, issue_number: int) -> Issue | None:
        try:
            return self.repository_host.get_issue(issue_number)
        except RepositoryHostError as exc:
            logger.warning(
                "Failed to refresh awaiting-merge issue #%d: %s",
                issue_number,
                exc,
            )
            return None


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
