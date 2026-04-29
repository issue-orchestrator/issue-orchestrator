"""Owner abstraction for web retry/history state mutations."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from ..domain.models import OrchestratorState


@dataclass(frozen=True)
class HistoryClearResult:
    """Outcome for clearing visible history state."""

    cleared_history_entries: int
    cleared_completed_today: int


@dataclass(frozen=True)
class HistoryIssueRemovalResult:
    """Outcome for removing one issue from retry-blocking history state."""

    issue_number: int
    removed_history_entries: int
    removed_completed_today: bool


@dataclass(frozen=True)
class PendingStateClearResult:
    """Outcome for removing stale queued review/rework/cleanup state."""

    review_count_before: int
    review_count_after: int
    rework_count_before: int
    rework_count_after: int
    cleanup_count_before: int
    cleanup_count_after: int
    superseded_prs: tuple[int, ...]


class RetryHistoryState:
    """Owns retry/history mutations that make issues launchable again."""

    def __init__(self, state: OrchestratorState) -> None:
        self._state = state

    def clear_history(self) -> HistoryClearResult:
        """Clear session-history and completed-today state together."""
        cleared_history_entries = len(self._state.session_history)
        cleared_completed_today = len(self._state.completed_today)
        self._state.session_history = []
        self._state.completed_today = []
        return HistoryClearResult(
            cleared_history_entries=cleared_history_entries,
            cleared_completed_today=cleared_completed_today,
        )

    def remove_issue_from_history(self, issue_number: int) -> HistoryIssueRemovalResult:
        """Remove an issue from history gates so planner can consider it again."""
        original_history_count = len(self._state.session_history)
        self._state.session_history = [
            entry for entry in self._state.session_history
            if entry.issue_number != issue_number
        ]

        removed_completed_today = issue_number in self._state.completed_today
        if removed_completed_today:
            self._state.completed_today.remove(issue_number)

        return HistoryIssueRemovalResult(
            issue_number=issue_number,
            removed_history_entries=(
                original_history_count - len(self._state.session_history)
            ),
            removed_completed_today=removed_completed_today,
        )

    def remove_issues_from_history(self, issue_numbers: Iterable[int]) -> list[int]:
        """Remove multiple issues from history gates."""
        retried: list[int] = []
        for issue_number in issue_numbers:
            self.remove_issue_from_history(issue_number)
            retried.append(issue_number)
        return retried

    def deprioritize_issues(self, issue_numbers: Iterable[int]) -> list[int]:
        """Remove issue numbers from the manual priority queue."""
        removed: list[int] = []
        for issue_number in issue_numbers:
            if issue_number in self._state.priority_queue:
                self._state.priority_queue.remove(issue_number)
                removed.append(issue_number)
        return removed

    def prioritize_issue_front(self, issue_number: int) -> bool:
        """Place an issue at the front of the manual priority queue if absent."""
        if issue_number in self._state.priority_queue:
            return False
        self._state.priority_queue.insert(0, issue_number)
        return True

    def clear_scratch_retry_pending_state(
        self,
        issue_number: int,
        superseded_prs: Iterable[int],
    ) -> PendingStateClearResult:
        """Remove stale pending review/rework/cleanup items after a scratch reset."""
        superseded_pr_numbers = set(superseded_prs)
        review_count_before = len(self._state.pending_reviews)
        rework_count_before = len(self._state.pending_reworks)
        cleanup_count_before = len(self._state.pending_cleanups)

        self._state.pending_reviews = [
            review
            for review in self._state.pending_reviews
            if review.issue_number != issue_number
            and review.pr_number not in superseded_pr_numbers
        ]
        self._state.pending_reworks = [
            rework
            for rework in self._state.pending_reworks
            if rework.resolve_issue_number() != issue_number
            and rework.pr_number not in superseded_pr_numbers
        ]
        self._state.pending_cleanups = [
            cleanup
            for cleanup in self._state.pending_cleanups
            if cleanup.issue_number != issue_number
            and cleanup.pr_number not in superseded_pr_numbers
        ]

        return PendingStateClearResult(
            review_count_before=review_count_before,
            review_count_after=len(self._state.pending_reviews),
            rework_count_before=rework_count_before,
            rework_count_after=len(self._state.pending_reworks),
            cleanup_count_before=cleanup_count_before,
            cleanup_count_after=len(self._state.pending_cleanups),
            superseded_prs=tuple(sorted(superseded_pr_numbers)),
        )
