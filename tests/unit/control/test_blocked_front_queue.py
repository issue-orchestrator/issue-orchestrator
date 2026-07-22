"""Tests for the blocked->front restore policy (#6873).

Covers both review boundaries: real-scheduler decisions INTO the policy, and
policy-owned queue entries THROUGH launch cleanup (the always-run, non-tech-lead
launch seam). The `_fetch_and_update_queue` wiring regression lives in
`test_orchestrator_support.py` instead.
"""

from __future__ import annotations

from unittest.mock import Mock

from issue_orchestrator.control.blocked_front_queue import (
    front_queue_newly_unblocked,
    release_blocked_front_on_launch,
)
from issue_orchestrator.control.label_manager import LabelManager
from issue_orchestrator.control.scheduler import (
    AvailabilityReason,
    IssueAvailabilityDecision,
    Scheduler,
)
from issue_orchestrator.domain.models import Issue, OrchestratorState
from issue_orchestrator.infra.config import Config


def _issue(number: int, labels: list[str] | None = None) -> Issue:
    return Issue(number=number, title=f"Issue {number}", labels=labels or [], state="open")


def _dec(
    number: int, available: bool, reason: AvailabilityReason
) -> IssueAvailabilityDecision:
    return IssueAvailabilityDecision(issue=_issue(number), available=available, reason=reason)


class TestFrontQueueNewlyUnblocked:
    """Issues leaving a blocked state jump to the front, keyed on typed predicates."""

    def test_unblocked_issue_moves_to_front(self):
        state = OrchestratorState()
        state.previously_blocked_issue_numbers = {5}
        front_queue_newly_unblocked(state, [_dec(5, True, AvailabilityReason.AVAILABLE)])
        assert state.priority_queue == [5]
        assert state.blocked_front_prioritized == [5]
        assert state.previously_blocked_issue_numbers == set()

    def test_still_blocked_is_not_queued(self):
        state = OrchestratorState()
        state.previously_blocked_issue_numbers = {5}
        front_queue_newly_unblocked(state, [_dec(5, False, AvailabilityReason.BLOCKED_LABEL)])
        assert state.priority_queue == []
        assert state.blocked_front_prioritized == []
        assert state.previously_blocked_issue_numbers == {5}

    def test_never_blocked_is_not_queued(self):
        state = OrchestratorState()
        front_queue_newly_unblocked(state, [_dec(9, True, AvailabilityReason.AVAILABLE)])
        assert state.priority_queue == []
        assert state.previously_blocked_issue_numbers == set()

    def test_both_label_and_dependency_routes_reach_the_front(self):
        state = OrchestratorState()
        state.previously_blocked_issue_numbers = {5, 6}
        front_queue_newly_unblocked(
            state,
            [
                _dec(5, True, AvailabilityReason.AVAILABLE),
                _dec(6, True, AvailabilityReason.AVAILABLE),
            ],
        )
        assert set(state.priority_queue) == {5, 6}
        assert set(state.blocked_front_prioritized) == {5, 6}
        assert state.previously_blocked_issue_numbers == set()

    def test_newly_blocked_issues_recorded_as_baseline(self):
        state = OrchestratorState()
        front_queue_newly_unblocked(
            state,
            [
                _dec(7, False, AvailabilityReason.DEPENDENCY_BLOCKED),
                _dec(8, False, AvailabilityReason.BLOCKED_LABEL),
            ],
        )
        assert state.priority_queue == []
        assert state.previously_blocked_issue_numbers == {7, 8}

    def test_idempotent_across_scans(self):
        state = OrchestratorState()
        state.previously_blocked_issue_numbers = {5}
        decisions = [_dec(5, True, AvailabilityReason.AVAILABLE)]
        front_queue_newly_unblocked(state, decisions)
        front_queue_newly_unblocked(state, decisions)  # still available -> stays, not re-added
        assert state.priority_queue == [5]
        assert state.blocked_front_prioritized == [5]

    # --- reconciliation lifecycle ---

    def test_reblock_releases_the_owned_entry(self):
        state = OrchestratorState()
        state.previously_blocked_issue_numbers = {5}
        front_queue_newly_unblocked(state, [_dec(5, True, AvailabilityReason.AVAILABLE)])
        assert state.priority_queue == [5] and state.blocked_front_prioritized == [5]
        front_queue_newly_unblocked(state, [_dec(5, False, AvailabilityReason.BLOCKED_LABEL)])
        assert state.priority_queue == []
        assert state.blocked_front_prioritized == []
        assert state.previously_blocked_issue_numbers == {5}

    def test_picked_up_entry_is_reconciled_out(self):
        state = OrchestratorState()
        state.previously_blocked_issue_numbers = {5}
        front_queue_newly_unblocked(state, [_dec(5, True, AvailabilityReason.AVAILABLE)])
        front_queue_newly_unblocked(
            state, [_dec(5, False, AvailabilityReason.IN_PROGRESS_ACTIVE_SESSION)]
        )
        assert state.priority_queue == []
        assert state.blocked_front_prioritized == []

    def test_operator_priority_is_not_claimed_or_released(self):
        state = OrchestratorState()
        state.priority_queue = [5]  # operator-owned, no ledger entry
        state.previously_blocked_issue_numbers = {5}
        front_queue_newly_unblocked(state, [_dec(5, True, AvailabilityReason.AVAILABLE)])
        assert state.priority_queue == [5]
        assert state.blocked_front_prioritized == []
        front_queue_newly_unblocked(state, [_dec(5, False, AvailabilityReason.BLOCKED_LABEL)])
        assert state.priority_queue == [5]  # operator entry survives

    # --- real scheduler boundary (R1) ---

    def test_real_scheduler_in_progress_active_is_not_front_queued(self):
        config = Config()
        scheduler = Scheduler(config=config)
        issue = _issue(5, [LabelManager(config).in_progress])
        session = Mock()
        session.issue = Mock(number=5)

        state = OrchestratorState()
        state.previously_blocked_issue_numbers = {5}
        decisions = scheduler.evaluate_issues([issue], active_sessions=[session])
        front_queue_newly_unblocked(state, decisions)
        assert state.priority_queue == []
        assert state.blocked_front_prioritized == []

    def test_real_scheduler_without_active_sessions_would_misqueue(self):
        config = Config()
        scheduler = Scheduler(config=config)
        issue = _issue(5, [LabelManager(config).in_progress])

        state = OrchestratorState()
        state.previously_blocked_issue_numbers = {5}
        decisions = scheduler.evaluate_issues([issue])  # no active_sessions == the bug
        front_queue_newly_unblocked(state, decisions)
        assert state.priority_queue == [5]


class TestReleaseBlockedFrontOnLaunch:
    """#6873 R4: launch cleanup runs through the always-run, non-tech-lead seam."""

    def test_successful_launch_clears_owned_entry_without_expedite_lane(self):
        state = OrchestratorState()
        state.previously_blocked_issue_numbers = {5}
        front_queue_newly_unblocked(state, [_dec(5, True, AvailabilityReason.AVAILABLE)])
        assert state.priority_queue == [5] and state.blocked_front_prioritized == [5]
        # No ExpediteLane involved — the launch seam frees it directly.
        release_blocked_front_on_launch(state, 5, launched=True)
        assert state.priority_queue == []
        assert state.blocked_front_prioritized == []

    def test_failed_launch_retains_the_entry(self):
        state = OrchestratorState()
        state.previously_blocked_issue_numbers = {5}
        front_queue_newly_unblocked(state, [_dec(5, True, AvailabilityReason.AVAILABLE)])
        release_blocked_front_on_launch(state, 5, launched=False)
        assert state.priority_queue == [5]  # issue never picked up -> stays

    def test_noop_for_unowned_issue(self):
        state = OrchestratorState(priority_queue=[5])  # operator-owned
        release_blocked_front_on_launch(state, 5, launched=True)
        assert state.priority_queue == [5]  # operator priority untouched
