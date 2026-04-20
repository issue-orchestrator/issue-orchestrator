"""Session history ownership helpers."""

from __future__ import annotations

from collections.abc import MutableSequence
from dataclasses import dataclass
from typing import Literal, TypeAlias

from ..domain.models import (
    AwaitingMergeTerminalStatus,
    RECONCILABLE_HISTORY_STATUSES,
    SessionHistoryEntry,
    SessionHistoryStatus,
)


@dataclass(frozen=True)
class HistoryReconciliationMutation:
    """Details of an applied history reconciliation mutation."""

    issue_number: int
    pr_url: str
    previous_status: SessionHistoryStatus
    status: AwaitingMergeTerminalStatus
    status_reason: str


HistoryReconciliationNoopReason: TypeAlias = Literal["missing", "not_reconcilable"]


@dataclass(frozen=True)
class HistoryReconciliationNoop:
    """Details of a history reconciliation no-op."""

    issue_number: int
    pr_url: str
    reason: HistoryReconciliationNoopReason
    current_status: SessionHistoryStatus | None = None


HistoryReconciliationResult: TypeAlias = (
    HistoryReconciliationMutation | HistoryReconciliationNoop
)


@dataclass
class SessionHistoryOwner:
    """Owns controlled mutations of session history entries."""

    session_history: MutableSequence[SessionHistoryEntry]

    def reconcile_awaiting_merge(
        self,
        *,
        issue_number: int,
        pr_url: str,
        status: AwaitingMergeTerminalStatus,
        status_reason: str,
    ) -> HistoryReconciliationResult:
        """Mark the latest matching awaiting-merge history entry terminal."""
        entry = self._find_latest_matching_entry(issue_number, pr_url)
        if entry is None:
            return HistoryReconciliationNoop(
                issue_number=issue_number,
                pr_url=pr_url,
                reason="missing",
            )
        if entry.status not in RECONCILABLE_HISTORY_STATUSES:
            return HistoryReconciliationNoop(
                issue_number=issue_number,
                pr_url=pr_url,
                reason="not_reconcilable",
                current_status=entry.status,
            )

        previous_status = entry.status
        entry.status = status
        entry.status_reason = status_reason
        return HistoryReconciliationMutation(
            issue_number=issue_number,
            pr_url=pr_url,
            previous_status=previous_status,
            status=status,
            status_reason=status_reason,
        )

    def _find_latest_matching_entry(
        self,
        issue_number: int,
        pr_url: str,
    ) -> SessionHistoryEntry | None:
        # The newest matching entry is canonical; do not fall back to older
        # duplicate PR history once the latest matching row is terminal.
        for entry in reversed(self.session_history):
            if entry.issue_number != issue_number:
                continue
            if entry.pr_url != pr_url:
                continue
            return entry
        return None
