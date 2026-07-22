"""Owner abstraction for web retry/history state mutations."""

from __future__ import annotations

import logging
from collections.abc import Callable, Collection, Iterable
from dataclasses import dataclass
from typing import Literal

from ..domain.models import OrchestratorState

logger = logging.getLogger(__name__)

# Why a tech-lead expedite request did (or did not) reach the worker lane. The
# owner returns this typed outcome instead of a bare bool so callers surface the
# reason (#6870) — a cap skip must be logged, never silently dropped.
ExpediteReason = Literal["expedited", "already_queued", "cap_reached", "disabled"]


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


@dataclass(frozen=True)
class ExpediteOutcome:
    """Result of one expedite-lane request (#6870), returned by the owner."""

    issue_number: int
    expedited: bool
    reason: ExpediteReason
    outstanding: int  # tech-lead-expedited issues in priority_queue after this call
    max_expedited: int


@dataclass(frozen=True)
class ExpediteEligibility:
    """Which pending expedite follow-ups are promotable this tick (#6870).

    ``eligible`` — issue numbers currently un-gated AND runnable (the gate label
    removed, no other blocking label). ``in_scope`` — issue numbers still open
    and in the configured scope; a pending issue absent from this set has been
    closed/rejected and is pruned rather than kept waiting forever.
    """

    eligible: frozenset[int]
    in_scope: frozenset[int]


@dataclass(frozen=True)
class ExpediteLane:
    """Applier/tick-facing expedite entrypoint bound to live state + the cap.

    The single seam the action applier and the planning cycle use to move a
    tech-lead follow-up onto the worker lane. Every write is routed through the
    :class:`RetryHistoryState` owner (never a direct ``priority_queue`` mutation
    from the applier/controller), and the configured cap travels with the lane
    so the check-and-insert stays atomic in the owner.
    """

    owner_factory: Callable[[], "RetryHistoryState"]
    eligibility_provider: Callable[[], ExpediteEligibility]
    max_expedited: int

    def expedite_now(self, issue_number: int) -> ExpediteOutcome:
        """Execute-authority path: jump the lane immediately at creation."""
        return self.owner_factory().expedite_issue_front(
            issue_number, max_expedited=self.max_expedited
        )

    def defer_until_ungated(self, issue_number: int) -> None:
        """Propose-authority path: remember a gated follow-up for later promotion."""
        self.owner_factory().record_expedite_pending(issue_number)

    def release(self, issue_number: int) -> bool:
        """Lifecycle hook: free an expedited issue's slot once it is worked."""
        return self.owner_factory().release_expedited(issue_number)

    def promote_ungated(self) -> list[ExpediteOutcome]:
        """Per-tick: promote every pending follow-up whose gate has been removed.

        Short-circuits when nothing is pending (the common case) so the
        eligibility scan over the cached queues is skipped entirely.
        """
        owner = self.owner_factory()
        if not owner.has_expedite_pending():
            return []
        eligibility = self.eligibility_provider()
        return owner.promote_expedite_pending(
            eligibility.eligible,
            eligibility.in_scope,
            max_expedited=self.max_expedited,
        )


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
        # A dequeued issue is no longer an outstanding tech-lead expedite (#6870).
        self._prune_expedited_ledger()
        return removed

    def prioritize_issue_front(self, issue_number: int) -> bool:
        """Place an issue at the front of the manual priority queue if absent."""
        if issue_number in self._state.priority_queue:
            return False
        self._state.priority_queue.insert(0, issue_number)
        return True

    def expedite_issue_front(
        self, issue_number: int, *, max_expedited: int
    ) -> ExpediteOutcome:
        """Front-queue an issue for the tech lead, bounded by the expedite cap (#6870).

        Unlike :meth:`prioritize_issue_front` (the unbounded operator/retry
        path), this enforces ``tech_lead.max_expedited`` atomically: the cap
        counts only OUTSTANDING tech-lead-expedited issues — those this owner
        placed that are still in ``priority_queue`` — so a noisy tech lead can
        never starve the worker lane, and operator/retry priorities never count
        against it. Stale ledger entries (an expedited issue since worked or
        dequeued) are pruned first so the count reflects reality; the check and
        the insert happen together so the bound cannot be raced.

        ``max_expedited <= 0`` disables the lane (``disabled``); an
        already-queued issue consumes no slot (``already_queued``); at the cap
        the request is skipped and LOGGED, never silently truncated
        (``cap_reached``).
        """
        self._prune_expedited_ledger()
        outstanding = len(self._state.tech_lead_expedited)
        if max_expedited <= 0:
            return ExpediteOutcome(
                issue_number, False, "disabled", outstanding, max_expedited
            )
        if issue_number in self._state.priority_queue:
            return ExpediteOutcome(
                issue_number, False, "already_queued", outstanding, max_expedited
            )
        if outstanding >= max_expedited:
            logger.info(
                "[EXPEDITE] Not expediting issue #%d: %d/%d expedite slot(s)"
                " already outstanding (tech_lead.max_expedited); it will be"
                " worked at normal priority",
                issue_number,
                outstanding,
                max_expedited,
            )
            return ExpediteOutcome(
                issue_number, False, "cap_reached", outstanding, max_expedited
            )
        self._state.priority_queue.insert(0, issue_number)
        self._state.tech_lead_expedited.append(issue_number)
        return ExpediteOutcome(
            issue_number, True, "expedited", outstanding + 1, max_expedited
        )

    def has_expedite_pending(self) -> bool:
        """True iff any gated expedite follow-up is awaiting promotion (#6870)."""
        return bool(self._state.tech_lead_expedite_pending)

    def record_expedite_pending(self, issue_number: int) -> None:
        """Remember a gated (propose-authority) expedite follow-up (#6870).

        The issue is created behind the ``proposed-tech-lead`` gate, so it must
        NOT jump the lane yet. :meth:`promote_expedite_pending` moves it to the
        front once an operator removes the gate.
        """
        if issue_number not in self._state.tech_lead_expedite_pending:
            self._state.tech_lead_expedite_pending.append(issue_number)

    def promote_expedite_pending(
        self,
        eligible: Collection[int],
        in_scope: Collection[int],
        *,
        max_expedited: int,
    ) -> list[ExpediteOutcome]:
        """Promote gated expedite follow-ups whose gate has been removed (#6870).

        The single point where a propose-authority expedite intent turns into a
        real front-queue write — exactly when the issue first becomes eligible
        for work, inheriting the ADR-0031 create_issue gate. A pending issue in
        ``eligible`` (un-gated + runnable) is expedited under the cap; one still
        ``in_scope`` but not yet eligible (still gated) stays pending; one absent
        from scope (closed/rejected) is dropped so the pending set never leaks. A
        cap-blocked promotion stays pending to retry once a slot frees.
        """
        eligible_set = set(eligible)
        in_scope_set = set(in_scope)
        outcomes: list[ExpediteOutcome] = []
        remaining: list[int] = []
        for issue_number in self._state.tech_lead_expedite_pending:
            if issue_number in eligible_set:
                outcome = self.expedite_issue_front(
                    issue_number, max_expedited=max_expedited
                )
                outcomes.append(outcome)
                if outcome.reason == "cap_reached":
                    remaining.append(issue_number)
            elif issue_number in in_scope_set:
                remaining.append(issue_number)
        self._state.tech_lead_expedite_pending = remaining
        return outcomes

    def release_expedited(self, issue_number: int) -> bool:
        """Free an expedited issue's lane slot once it is being worked (#6870).

        The cap counts OUTSTANDING expedited issues; without a release those
        slots would never come back (nothing else removes a worked issue from
        ``priority_queue``), so after ``max_expedited`` expedites every later
        request would return ``cap_reached`` forever. Hooked into the
        session-launch lifecycle: once an expedited issue is picked up as an
        active session it has already jumped the lane, so this drops it from
        BOTH the expedite ledger (freeing the slot) and ``priority_queue``.

        Scoped to tech-lead-expedited issues: an operator/retry ``priority_queue``
        entry is never touched — the two sets are disjoint by construction
        (``expedite_issue_front`` skips an already-queued issue). No-op for a
        non-expedited issue.
        """
        if issue_number not in self._state.tech_lead_expedited:
            return False
        self._state.tech_lead_expedited = [
            n for n in self._state.tech_lead_expedited if n != issue_number
        ]
        self._state.priority_queue = [
            n for n in self._state.priority_queue if n != issue_number
        ]
        return True

    def _prune_expedited_ledger(self) -> None:
        """Drop expedite-ledger entries no longer in ``priority_queue`` (#6870)."""
        self._state.tech_lead_expedited = [
            n for n in self._state.tech_lead_expedited
            if n in self._state.priority_queue
        ]

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
        # Expedite bookkeeping is issue-scoped queue state (#6870): a scratch
        # reset drops the issue from both the outstanding ledger and the
        # awaiting-un-gate pending set alongside priority_queue.
        self._state.tech_lead_expedited = [
            n for n in self._state.tech_lead_expedited if n != issue_number
        ]
        self._state.tech_lead_expedite_pending = [
            n for n in self._state.tech_lead_expedite_pending if n != issue_number
        ]
        self._state.queue_pending_shrink_missing_issue_numbers = [
            n for n in self._state.queue_pending_shrink_missing_issue_numbers
            if n != issue_number
        ]
