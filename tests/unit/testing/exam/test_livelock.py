"""Livelocks: the same failure, same subject, no state change (#7345/#7346 class).

Event shapes are the engine's own: ``reconciliation.required`` /
``issue.paused_reconcile`` as porchpin#410 emitted them 130 times, and the
``apply.failed`` that ``orchestrator_support._emit_apply_failed`` publishes
for a failed action (#7345's settlement, which fails on every tick).
"""

from __future__ import annotations

import pytest

from issue_orchestrator.testing.exam import grade, render_summary
from issue_orchestrator.testing.exam.cases import HALTED_EXCHANGE_WITH_VALIDATED_WORK
from issue_orchestrator.testing.exam.livelock import (
    LIVELOCK_THRESHOLD,
    RepeatingFailure,
    find_repeating_failures,
)

from .builders import item, observation, pr
from .test_grading import CASE_A
from issue_orchestrator.testing.exam import PullRequestState

RECONCILE = "Has forbidden labels: frozenset({'io:needs-reconcile'})"


def _reconcile_tick() -> list[dict]:
    return [
        {"type": "stale.in_progress_detected", "issue_key": "410", "payload": {"issue_number": 410}},
        {"type": "reconciliation.required", "issue_key": "410", "payload": {"issue_number": 410, "reason": RECONCILE}},
        {"type": "issue.paused_reconcile", "issue_key": "410", "payload": {"issue_number": 410, "reason": RECONCILE}},
        {"type": "tick.completed", "issue_key": None, "payload": {"tick_id": 1}},
    ]


def _settlement_failure(tick: int) -> dict:
    return {
        "type": "apply.failed",
        "issue_key": None,
        "payload": {
            "step_type": "settle_tech_lead_promotion",
            "issue_number": None,
            "error": f"pattern 'sig-{tick}' is terminal ('shipped'); tick {tick}",
        },
    }


def test_the_porchpin_410_reconcile_loop_is_a_livelock() -> None:
    events = [event for _ in range(130) for event in _reconcile_tick()]

    found = find_repeating_failures(events)

    assert {(r.event, r.subject, r.count) for r in found} == {
        ("reconciliation.required", "#410", 130),
        ("issue.paused_reconcile", "#410", 130),
    }


def test_a_board_wide_settlement_failing_every_tick_is_a_livelock() -> None:
    """#7345: no issue at all — the engine itself is the subject; the error's
    changing ids/counts do not hide that it is the same failure."""
    found = find_repeating_failures([_settlement_failure(tick) for tick in range(10)])

    assert found == (
        RepeatingFailure(
            event="apply.failed",
            subject="the engine",
            detail="settle_tech_lead_promotion; pattern 'sig-N' is terminal ('shipped'); tick N",
            count=10,
        ),
    )


def test_a_bounded_retry_is_not_a_livelock() -> None:
    """The review exchange's three no-completion attempts (Case A's planted fault)."""
    events = [
        {"type": "review_exchange.role_timeout", "issue_key": "7307",
         "payload": {"issue_number": 7307, "role": "reviewer", "failure_reason": "prompt_write_failed"}}
        for _ in range(3)
    ]
    assert find_repeating_failures(events) == ()


def test_a_state_change_between_failures_resets_the_count() -> None:
    change = {"type": "issue.labels_changed", "issue_key": "410", "payload": {"issue_number": 410}}
    events = [event for _ in range(4) for event in _reconcile_tick()] + [change] + [
        event for _ in range(4) for event in _reconcile_tick()
    ]
    assert find_repeating_failures(events) == ()


def test_a_successful_apply_of_the_same_step_resets_it() -> None:
    success = {"type": "apply.step_applied", "issue_key": None,
               "payload": {"step_type": "settle_tech_lead_promotion", "result": "success"}}
    events = [_settlement_failure(t) for t in range(4)] + [success] + [_settlement_failure(t) for t in range(4)]
    assert find_repeating_failures(events) == ()


def test_other_subjects_do_not_reset_or_join_a_subjects_count() -> None:
    other_change = {"type": "issue.labels_changed", "issue_key": "999", "payload": {"issue_number": 999}}
    events = []
    for _ in range(LIVELOCK_THRESHOLD):
        events += _reconcile_tick() + [other_change]
    found = find_repeating_failures(events)
    assert {r.subject for r in found} == {"#410"}


def test_the_threshold_must_mean_a_repeat() -> None:
    with pytest.raises(ValueError, match="at least 2"):
        find_repeating_failures([], threshold=1)


def test_a_livelock_fails_a_case_whose_goals_all_held() -> None:
    done = item(prs=(pr(state=PullRequestState.READY, labels=("code-reviewed",)),))
    loop = find_repeating_failures([_settlement_failure(t) for t in range(8)])
    card = grade(CASE_A, observation(HALTED_EXCHANGE_WITH_VALIDATED_WORK, done, repeating=loop))

    assert all(goal.passed for goal in card.goals)
    assert not card.passed
    assert card.failures == (f"livelock: {loop[0].describe()}",)
    assert "livelocks [FAIL]: apply.failed [settle_tech_lead_promotion" in render_summary(card)


def test_case_c_passes_only_when_the_pause_stays_and_nothing_loops() -> None:
    from issue_orchestrator.testing.exam.cases import (
        STALE_CLAIM_PAUSED_FOR_RECONCILE,
        stale_claim_paused_for_reconcile,
    )

    case = stale_claim_paused_for_reconcile(needs_reconcile_label="io:needs-reconcile")
    paused = item(issue_labels=("in-progress", "io:needs-reconcile"), events=("issue.paused_reconcile",))

    quiet = grade(case, observation(STALE_CLAIM_PAUSED_FOR_RECONCILE, paused))
    assert quiet.passed, quiet.failures

    looping = find_repeating_failures([event for _ in range(30) for event in _reconcile_tick()])
    loop = grade(case, observation(STALE_CLAIM_PAUSED_FOR_RECONCILE, paused, repeating=looping))
    assert not loop.passed and all(f.startswith("livelock: ") for f in loop.failures)

    unpaused = grade(case, observation(STALE_CLAIM_PAUSED_FOR_RECONCILE, item(issue_labels=("in-progress",), events=("issue.paused_reconcile",))))
    assert "goal subject.keeps_labels: issue #901 lost ['io:needs-reconcile']" in unpaused.failures



def test_a_paused_claim_the_engine_only_notices_is_not_a_livelock() -> None:
    """Round 7 F1: stale-claim DETECTION repeats while a pause legitimately
    holds; only repeated refusals/re-pauses are the loop."""
    from issue_orchestrator.testing.exam.cases import (
        STALE_CLAIM_PAUSED_FOR_RECONCILE,
        stale_claim_paused_for_reconcile,
    )

    noticing = [
        {"type": "stale.in_progress_detected", "issue_key": "410", "payload": {"issue_number": 410}}
        for _ in range(40)
    ]
    assert find_repeating_failures(noticing) == ()
    case = stale_claim_paused_for_reconcile(needs_reconcile_label="io:needs-reconcile")
    card = grade(
        case,
        observation(
            STALE_CLAIM_PAUSED_FOR_RECONCILE,
            item(issue_labels=("in-progress", "io:needs-reconcile"), events=("stale.in_progress_detected",)),
            repeating=find_repeating_failures(noticing),
        ),
    )
    assert card.passed, card.failures


@pytest.mark.parametrize("reason", ["no_capacity", "orchestrator_paused", "retrospective_review_no_capacity"])
def test_waiting_skips_are_not_livelocks(reason: str) -> None:
    waiting = [{"type": "review.skipped", "issue_key": None, "payload": {"reason": reason}} for _ in range(20)]
    assert find_repeating_failures(waiting) == ()


def test_a_review_refused_every_scan_is_a_livelock() -> None:
    """The #7291 veto shape: the same review dropped as stale, scan after scan."""
    refused = [
        {"type": "review.skipped", "issue_key": "7309",
         "payload": {"issue_number": 7309, "reason": "stale_pending_review:issue_blocked"}}
        for _ in range(6)
    ]
    assert [r.detail for r in find_repeating_failures(refused)] == ["stale_pending_review:issue_blocked"]
