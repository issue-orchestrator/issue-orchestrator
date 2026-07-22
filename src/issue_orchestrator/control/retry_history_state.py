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

    def make_retryable(self, issue_number: int) -> HistoryIssueRemovalResult:
        """Clear every in-memory planner gate that maps to "this issue has
        already had a session this run" so the planner will consider the
        issue again on its next tick.

        Specifically: prune ``session_history`` entries (via
        :meth:`remove_issue_from_history`) and discard the issue from
        ``failed_this_cycle``. Both gate :meth:`QueueCache.evaluate_issue`
        and the planner's eligibility loop — leaving either set means the
        planner will keep skipping the issue even after retry-gating
        GitHub labels have been removed. Callers (typically the
        ``/api/issues/{n}/retry`` endpoint) must only invoke this after
        confirming the corresponding labels actually came off GitHub.
        """
        result = self.remove_issue_from_history(issue_number)
        self._state.failed_this_cycle.discard(issue_number)
        return result

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
        """Remove every issue-keyed in-memory record after a scratch reset.

        Grouped into four categories so the migration path to #6130's
        ``Attempt`` aggregate is explicit:

        - ``_clear_pending_workflow_queues`` and ``_clear_attempt_scoped_state``
          hold the bulk of attempt-scoped state. After #6130 introduces
          ``AttemptStore.supersede(issue_key)``, most of these collapse: the
          old ``Attempt``'s state becomes invisible by construction and no
          imperative clearance is needed.
        - ``_clear_discovered_facts`` is also attempt-scoped (each fact pins a
          specific PR/branch); same migration path.
        - ``_clear_progress_flags`` and ``_clear_queue_and_ui_hints`` are
          genuinely issue-scoped (they outlive any single attempt) and stay.

        The contract is pinned by
        ``test_clear_scratch_retry_state_contract_no_leaks_for_target`` — adding
        an issue-keyed field requires clearing it here or carving it out in
        the contract test, which forces the attempt-vs-issue scope question
        at add time.
        """
        superseded_pr_numbers = set(superseded_prs)
        review_count_before = len(self._state.pending_reviews)
        rework_count_before = len(self._state.pending_reworks)
        cleanup_count_before = len(self._state.pending_cleanups)

        self._clear_pending_workflow_queues(issue_number, superseded_pr_numbers)
        self._clear_attempt_scoped_state(issue_number)
        self._clear_discovered_facts(issue_number, superseded_pr_numbers)
        self._clear_progress_flags(issue_number)
        self._clear_queue_and_ui_hints(issue_number)
        for pr_number in superseded_pr_numbers:
            self._state.awaiting_merge_rollup_scan_timestamps.pop(pr_number, None)

        return PendingStateClearResult(
            review_count_before=review_count_before,
            review_count_after=len(self._state.pending_reviews),
            rework_count_before=rework_count_before,
            rework_count_after=len(self._state.pending_reworks),
            cleanup_count_before=cleanup_count_before,
            cleanup_count_after=len(self._state.pending_cleanups),
            superseded_prs=tuple(sorted(superseded_pr_numbers)),
        )

    def _clear_pending_workflow_queues(
        self,
        issue_number: int,
        superseded_pr_numbers: set[int],
    ) -> None:
        """Workflow queues (review/rework/cleanup/tech_lead) — attempt-scoped."""
        self._state.pending_reviews = [
            r for r in self._state.pending_reviews
            if r.issue_number != issue_number
            and r.pr_number not in superseded_pr_numbers
        ]
        self._state.pending_reworks = [
            r for r in self._state.pending_reworks
            if r.resolve_issue_number() != issue_number
            and r.pr_number not in superseded_pr_numbers
        ]
        self._state.pending_cleanups = [
            c for c in self._state.pending_cleanups
            if c.issue_number != issue_number
            and c.pr_number not in superseded_pr_numbers
        ]
        self._state.pending_tech_lead_reviews = [
            t for t in self._state.pending_tech_lead_reviews
            if t.issue_number != issue_number
        ]

    def _clear_attempt_scoped_state(self, issue_number: int) -> None:
        """Validation retries — purely per-attempt records."""
        self._state.pending_validation_retries = [
            r for r in self._state.pending_validation_retries
            if r.issue_number != issue_number
        ]

    def _clear_discovered_facts(
        self,
        issue_number: int,
        superseded_pr_numbers: set[int],
    ) -> None:
        """Discovered_* and immediate_cleanups — planner inputs, attempt-scoped."""
        self._state.discovered_reviews = [
            d for d in self._state.discovered_reviews
            if d.issue_number != issue_number
            and d.pr_number not in superseded_pr_numbers
        ]
        self._state.discovered_reworks = [
            d for d in self._state.discovered_reworks
            if d.issue_number != issue_number
            and d.pr_number not in superseded_pr_numbers
        ]
        self._state.discovered_escalations = [
            d for d in self._state.discovered_escalations
            if d.issue_number != issue_number
            and d.pr_number not in superseded_pr_numbers
        ]
        self._state.discovered_failures = [
            d for d in self._state.discovered_failures
            if d.issue_number != issue_number
        ]
        self._state.discovered_awaiting_merge_reconciliations = [
            d for d in self._state.discovered_awaiting_merge_reconciliations
            if d.issue_number != issue_number
            and d.pr_number not in superseded_pr_numbers
        ]
        self._state.discovered_awaiting_merge_drifts = [
            d for d in self._state.discovered_awaiting_merge_drifts
            if d.issue_number != issue_number
            and d.pr_number not in superseded_pr_numbers
        ]
        self._state.discovered_awaiting_merge_escalations = [
            d for d in self._state.discovered_awaiting_merge_escalations
            if d.issue_number != issue_number
            and d.pr_number not in superseded_pr_numbers
        ]
        self._state.discovered_merge_queue_enqueues = [
            d for d in self._state.discovered_merge_queue_enqueues
            if d.issue_number != issue_number
            and d.pr_number not in superseded_pr_numbers
        ]
        self._state.immediate_cleanups = [
            c for c in self._state.immediate_cleanups
            if c.issue_number != issue_number
        ]

    def _clear_progress_flags(self, issue_number: int) -> None:
        """Progress-blocking flags — genuinely issue-scoped, stay after #6130."""
        self._state.failed_this_cycle.discard(issue_number)
        self._state.stale_issue_ticks.pop(issue_number, None)
        self._state.dependency_problems.pop(issue_number, None)

    def _clear_queue_and_ui_hints(self, issue_number: int) -> None:
        """Queue/refresh/UI hints — issue-scoped, cross-attempt, stay after #6130."""
        self._state.issue_refresh_timestamps.pop(issue_number, None)
        self._state.issue_last_refreshed_at.pop(issue_number, None)
        self._state.awaiting_merge_drift_scan_timestamps.pop(issue_number, None)
        self._state.ui_visible_issue_numbers = [
            n for n in self._state.ui_visible_issue_numbers
            if n != issue_number
        ]
        self._state.priority_queue = [
            n for n in self._state.priority_queue
            if n != issue_number
        ]
        self._state.queue_pending_shrink_missing_issue_numbers = [
            n for n in self._state.queue_pending_shrink_missing_issue_numbers
            if n != issue_number
        ]
