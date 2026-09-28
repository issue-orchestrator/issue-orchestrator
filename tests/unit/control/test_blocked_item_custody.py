"""The custody owner's POLICY: one blocked item's facts -> its custody (#7331).

Pure: every case hands :func:`derive_item_custody` a hand-built fact set and
reads the decided state, reason, clock, staleness and charter basis. The
fact-gathering half is covered in ``test_blocked_item_custody_reader.py``.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from issue_orchestrator.control.blocked_item_custody import (
    CUSTODY_UNKNOWN,
    ActiveWork,
    BoardCustodyFacts,
    ItemCustodyFacts,
    ObservedLabels,
    OpenProposal,
    ProviderWait,
    RateLimitWait,
    StuckSweepSchedule,
    TrackedFix,
    derive_item_custody,
)
from issue_orchestrator.domain.blocked_item_custody import (
    OWNED_CUSTODY_STATES,
    BlockedCustodyBoard,
    BlockedItemCustody,
    CustodyStaleThresholds,
    CustodyState,
)
from issue_orchestrator.domain.human_block import NeedsHumanCause
from issue_orchestrator.domain.tech_lead_charter import (
    CharterAuthority,
    CharterDepth,
    CharterRole,
    RoleCharter,
    TechLeadCharter,
    decide_charter,
)
from issue_orchestrator.domain.tech_lead_charter_decisions import (
    CharterDecisionSource,
    CharterExecutionLink,
    CharterExecutionResult,
    CharterProposalLifecycle,
    TechLeadCharterDecision,
    decision_key,
)
from issue_orchestrator.ports.blocked_item_custody import ParkedActionFact

NOW = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)
HOUR = timedelta(hours=1)

THRESHOLDS = CustodyStaleThresholds(
    by_state={state: timedelta(hours=2) for state in OWNED_CUSTODY_STATES}
)
SWEEP_ON = StuckSweepSchedule(
    enabled=True, max_attempts=3, next_due_at=NOW + 3 * HOUR
)
BOARD = BoardCustodyFacts(now=NOW, sweep=SWEEP_ON)

BLOCKED_FAILED = ObservedLabels(blocking=("blocked-failed",))
NEEDS_HUMAN = ObservedLabels(blocking=("needs-human",), needs_human=True)


def _item(**overrides: object) -> ItemCustodyFacts:
    base = ItemCustodyFacts(issue_number=42, labels=BLOCKED_FAILED)
    return replace(base, **overrides)  # type: ignore[arg-type]


def _derive(item: ItemCustodyFacts, board: BoardCustodyFacts = BOARD) -> BlockedItemCustody:
    return derive_item_custody(item, board, THRESHOLDS)


def _decision(
    kind: str,
    *,
    charter: TechLeadCharter | None = None,
    ceiling: CharterAuthority = CharterAuthority.EXECUTE,
    at: datetime = NOW - HOUR,
    target: int | None = 42,
    tracks_proposal: bool = False,
    proposal_issue_number: int | None = None,
    action_id: str = "A1",
) -> TechLeadCharterDecision:
    verdict = decide_charter(
        kind,
        charter or TechLeadCharter.default(),
        action_ceiling=ceiling,
        ceiling_source=f"tech_lead.authority.{kind}",
    )
    return TechLeadCharterDecision.from_verdict(
        verdict,
        decision_id=decision_key("run-1", action_id),
        source=CharterDecisionSource.DECISION,
        run_id="run-1",
        action_id=action_id,
        anchor_issue_number=42,
        target_number=target,
        target_is_pr=False,
        decided_at=at.isoformat(),
        tracks_proposal=tracks_proposal,
        proposal_issue_number=proposal_issue_number,
    )


def _linked(
    decision: TechLeadCharterDecision,
    result: CharterExecutionResult = CharterExecutionResult.APPLIED,
    reason: str | None = None,
) -> TechLeadCharterDecision:
    """An executed decision with its applier's result linked back (#7362)."""
    return decision.with_execution(
        CharterExecutionLink(decision.decision_id, result, reason), at=decision.decided_at
    )


# -- one rule per state --------------------------------------------------------


def test_nothing_on_it_is_unowned_and_always_needs_attention() -> None:
    custody = _derive(_item(last_activity_at=NOW - 5 * HOUR))

    assert custody.state is CustodyState.UNOWNED
    assert custody.needs_attention
    assert custody.stale_after is None
    assert custody.reason.startswith("Blocked by blocked-failed; nothing has picked it up.")
    assert "next stuck sweep is due at 2026-09-27T15:00" in custody.reason
    assert custody.clock is not None and custody.clock.lower_bound


def test_unowned_names_a_disabled_sweep_so_nobody_waits_for_it() -> None:
    board = replace(BOARD, sweep=replace(SWEEP_ON, enabled=False, next_due_at=None))

    assert "stuck sweep is off" in _derive(_item(), board).reason


def test_a_live_tech_lead_session_is_investigating() -> None:
    custody = _derive(
        _item(tech_lead_session=ActiveWork("tech-lead session", NOW - timedelta(minutes=30)))
    )

    assert custody.state is CustodyState.INVESTIGATING
    assert not custody.stale
    assert custody.clock is not None and custody.clock.basis == "tech-lead session started"


def test_a_live_fix_session_is_being_fixed() -> None:
    custody = _derive(_item(active_fix=ActiveWork("rework session", NOW - HOUR)))

    assert custody.state is CustodyState.BEING_FIXED
    assert custody.reason == "A rework session is working on it now."


def test_an_open_proposal_is_waiting_on_you_with_the_decision_that_filed_it() -> None:
    filed = _decision(
        "kill_hung_session",
        ceiling=CharterAuthority.PROPOSE,
        tracks_proposal=True,
        proposal_issue_number=700,
    )
    custody = _derive(
        _item(
            proposals=(OpenProposal(700, "kill_hung_session", NOW - HOUR),),
            decisions=(filed,),
        )
    )

    assert custody.state is CustodyState.WAITING_ON_YOU
    assert "Proposal #700 (kill hung session) awaits your approval" in custody.reason
    assert custody.charter is not None
    assert custody.charter.decision_id == filed.decision_id
    assert custody.charter.outcome == "proposed"
    assert custody.charter.role == "flow"
    assert custody.charter.role_authority == "execute"
    assert custody.charter.action_ceiling == "propose"
    assert custody.charter.reason == filed.reason


def test_an_awaiting_decision_without_an_open_op_does_not_claim_the_item() -> None:
    """The op ledger decides what is still open; a stale lifecycle cannot."""
    lingering = _decision(
        "kill_hung_session", ceiling=CharterAuthority.PROPOSE, tracks_proposal=True
    )
    assert lingering.lifecycle is CharterProposalLifecycle.AWAITING_APPROVAL

    assert _derive(_item(decisions=(lingering,))).state is CustodyState.UNOWNED


@pytest.mark.parametrize(
    ("cause", "marker", "words"),
    [
        (NeedsHumanCause.AGENT_COMPLETION, False, "An agent asked for a human"),
        (NeedsHumanCause.MERGE_ESCALATION, False, "escalated out of the merge lifecycle"),
        (NeedsHumanCause.CLAIM_QUARANTINE, False, "work claim could not be read"),
        (NeedsHumanCause.VALIDATED_WORK_DISPOSITION, False, "could not be published"),
        (None, True, "The tech lead escalated it to you."),
    ],
)
def test_a_request_for_a_person_is_waiting_on_you(
    cause: NeedsHumanCause | None, marker: bool, words: str
) -> None:
    labels = replace(NEEDS_HUMAN, tech_lead_escalated=marker)
    causes = frozenset({cause}) if cause else frozenset()

    custody = _derive(_item(labels=labels, needs_human_causes=causes))

    assert custody.state is CustodyState.WAITING_ON_YOU
    assert words in custody.reason


def test_a_policy_escalation_is_held_not_waiting_on_you() -> None:
    custody = _derive(
        _item(labels=NEEDS_HUMAN, needs_human_causes=frozenset({NeedsHumanCause.SESSION_LIFECYCLE}))
    )

    assert custody.state is CustodyState.HELD
    assert custody.reason.startswith("Escalated by policy")


def test_a_needs_human_nobody_recorded_is_waiting_on_you() -> None:
    """Nobody but a person set it; the sweep may still re-examine it."""
    custody = _derive(_item(labels=NEEDS_HUMAN))

    assert custody.state is CustodyState.WAITING_ON_YOU
    assert "no orchestrator cause on record" in custody.reason
    assert "stuck sweep may re-examine it" in custody.reason


def test_a_bare_needs_human_the_sweep_is_re_checking_is_the_tech_lead_s() -> None:
    """Agree with the stuck sweep: it treats a bare needs-human as eligible."""
    custody = _derive(_item(labels=NEEDS_HUMAN, sweep_attempts=1))

    assert custody.state is CustodyState.QUEUED_FOR_TECH_LEAD


def test_a_sweep_budget_is_no_queue_once_the_sweep_is_off() -> None:
    board = replace(BOARD, sweep=replace(SWEEP_ON, enabled=False, next_due_at=None))

    custody = _derive(_item(sweep_attempts=1), board)

    assert custody.state is CustodyState.UNOWNED
    assert "stuck sweep is off" in custody.reason


def test_an_exhausted_stuck_sweep_is_held_once_its_escalation_landed() -> None:
    custody = _derive(
        _item(labels=NEEDS_HUMAN, sweep_attempts=3, last_activity_at=NOW - 5 * HOUR)
    )

    assert custody.state is CustodyState.HELD
    assert "spent its 3 recovery attempt(s)" in custody.reason
    assert custody.clock is not None and custody.clock.since == NOW - 5 * HOUR
    assert custody.clock.lower_bound
    assert custody.stale  # a board-wide sweep time would have restarted it


def test_a_later_sweep_does_not_restart_a_held_clock() -> None:
    """The landed hold is dated by the item's own activity, not the sweep time."""
    item = _item(labels=NEEDS_HUMAN, sweep_attempts=3, last_activity_at=NOW - 5 * HOUR)

    before = _derive(item)
    after_sweep = _derive(item, replace(BOARD, now=NOW + HOUR))

    assert before.clock == after_sweep.clock
    assert after_sweep.stale


def test_a_sweep_queue_is_undated_rather_than_dated_by_older_activity() -> None:
    """The sweep writes nothing on the issue, so old activity predates the queue."""
    for facts in ({"sweep_attempts": 1}, {"sweep_attempts": 3, "sweep_escalation_pending": True}):
        custody = _derive(_item(last_activity_at=NOW - 5 * 24 * HOUR, **facts))
        assert custody.state is CustodyState.QUEUED_FOR_TECH_LEAD
        assert custody.clock is None
        assert not custody.stale


def test_a_pending_escalation_is_no_queue_once_the_sweep_is_off() -> None:
    board = replace(BOARD, sweep=replace(SWEEP_ON, enabled=False, next_due_at=None))

    custody = _derive(_item(sweep_attempts=3, sweep_escalation_pending=True), board)

    assert custody.state is CustodyState.UNOWNED
    assert custody.needs_attention
    assert "stuck sweep is off" in custody.reason


def test_an_unlanded_escalation_is_still_the_sweep_s_to_retry() -> None:
    custody = _derive(_item(sweep_attempts=3, sweep_escalation_pending=True))

    assert custody.state is CustodyState.QUEUED_FOR_TECH_LEAD
    assert "has not landed yet" in custody.reason
    assert "escalated it for a person" not in custody.reason
    landed = _derive(_item(labels=NEEDS_HUMAN, sweep_attempts=3, sweep_escalation_pending=True))
    assert landed.state is CustodyState.HELD


def test_an_exhausted_budget_with_nothing_in_flight_is_unowned() -> None:
    assert _derive(_item(sweep_attempts=3)).state is CustodyState.UNOWNED


def test_a_parked_action_is_held_with_the_liveness_owner_s_reason() -> None:
    parked = ParkedActionFact(
        action="settle_promotion",
        outcome="permanent",
        reason="registry already shipped",
        parked_since=NOW - 3 * HOUR,
    )
    custody = _derive(_item(parked=(parked,)))

    assert custody.state is CustodyState.HELD
    assert custody.reason == (
        "The orchestrator stopped retrying settle promotion (permanent): registry already shipped"
    )
    assert custody.stale


@pytest.mark.parametrize(
    ("facts", "words"),
    [
        ({"queued_fix": ActiveWork("rework of its PR")}, "A rework of its PR is queued."),
        (
            {"tracked_fix": TrackedFix(900, NOW - HOUR, NOW + 20 * HOUR)},
            "waiting on fix issue #900",
        ),
        (
            {"labels": replace(BLOCKED_FAILED, recovery_pending=True)},
            "Validated-work recovery is publishing",
        ),
    ],
)
def test_a_fix_on_its_way_is_being_fixed(facts: dict[str, object], words: str) -> None:
    custody = _derive(_item(**facts))

    assert custody.state is CustodyState.BEING_FIXED
    assert words in custody.reason


@pytest.mark.parametrize(
    ("facts", "words"),
    [
        (
            {
                "labels": replace(BLOCKED_FAILED, provider_unavailable=True),
                "provider_wait": ProviderWait("codex:metered", NOW + HOUR, NOW - HOUR),
            },
            "Provider codex:metered is unavailable",
        ),
        ({"checks_pending_since": NOW - HOUR}, "required CI checks"),
        ({"labels": replace(BLOCKED_FAILED, cross_milestone=True)}, "Waiting on a dependency"),
        ({"dependency_summary": "blocked by #7"}, "Waiting on a dependency: blocked by #7."),
    ],
)
def test_the_environment_is_waiting_on_world(facts: dict[str, object], words: str) -> None:
    custody = _derive(_item(**facts))

    assert custody.state is CustodyState.WAITING_ON_WORLD
    assert words in custody.reason


def test_a_provider_label_whose_circuit_closed_is_not_waiting_on_anything() -> None:
    """The resilience manager no longer owns it: an orphan, exactly as the sweep sees it."""
    custody = _derive(_item(labels=replace(BLOCKED_FAILED, provider_unavailable=True)))

    assert custody.state is CustodyState.UNOWNED


def test_the_tech_lead_s_queue_is_queued_for_tech_lead() -> None:
    custody = _derive(
        _item(tech_lead_queue=ActiveWork("tech-lead failure investigation", NOW - HOUR))
    )

    assert custody.state is CustodyState.QUEUED_FOR_TECH_LEAD
    assert custody.reason == "Queued for a tech-lead failure investigation."


def test_a_sweep_recovery_in_progress_is_queued_for_tech_lead() -> None:
    custody = _derive(_item(sweep_attempts=1))

    assert custody.state is CustodyState.QUEUED_FOR_TECH_LEAD
    assert "failed cycles 1 of 3" in custody.reason


def test_a_rate_limit_turns_a_queued_launch_into_waiting_on_world() -> None:
    wait = RateLimitWait(resets_at=NOW + HOUR, since=NOW - 3 * HOUR)

    custody = _derive(
        _item(
            tech_lead_queue=ActiveWork(
                "tech-lead failure investigation", NOW, rate_limited=wait
            )
        )
    )

    assert custody.state is CustodyState.WAITING_ON_WORLD
    assert "deferred by a GitHub rate limit until 2026-09-27T13:00" in custody.reason
    assert custody.stale  # three hours against a two-hour threshold


@pytest.mark.parametrize(
    ("kind", "words"),
    [
        ("recover_validated_work", "recover validated work"),
        # #7399: a released review is verified by the review actually running.
        ("release_withheld_review", "release withheld review"),
    ],
)
def test_an_executed_remedy_is_verify_with_its_decision(kind: str, words: str) -> None:
    executed = _linked(_decision(kind))

    custody = _derive(_item(decisions=(executed,), blocked_at=NOW - 2 * HOUR))

    assert custody.state is CustodyState.VERIFY
    assert custody.charter is not None and custody.charter.outcome == "executed"
    assert words in custody.reason


@pytest.mark.parametrize(
    ("result", "reason", "words"),
    [
        (CharterExecutionResult.REFUSED, "stale precondition: work_claimed",
         "(release withheld review) did not take effect: refused, stale precondition: work_claimed"),
        (CharterExecutionResult.FAILED, "review release failed (label_write)",
         "did not take effect: failed, review release failed (label_write)"),
        (CharterExecutionResult.WITHHELD, "withheld: a mandated tech-lead action did not commit",
         "did not take effect: withheld"),
        (None, None, "no result of it is recorded, so nothing shows it took effect"),
    ],
)
def test_an_executed_remedy_that_did_not_take_effect_is_never_verify(
    result: CharterExecutionResult | None, reason: str | None, words: str
) -> None:
    """#7362: the charter let it execute, but what the applier did decides."""
    decided = _decision("release_withheld_review")
    executed = decided if result is None else _linked(decided, result, reason)

    custody = _derive(_item(decisions=(executed,), blocked_at=NOW - 2 * HOUR))

    assert custody.state is CustodyState.UNOWNED
    assert "applied" not in custody.reason
    assert words in custody.reason


def test_a_parked_remedy_is_held_only_while_its_park_stands() -> None:
    """#7362 review r5: the liveness owner's live park holds the item; once a
    person releases it, the record's PARKED is only why the remedy never took
    effect, and the item is unowned again."""
    parked = _linked(
        _decision("release_withheld_review"), CharterExecutionResult.PARKED,
        "the orchestrator stopped retrying it (transient): 403",
    )
    fact = ParkedActionFact(action="release_withheld_review", outcome="transient",
                            reason="403", parked_since=NOW - HOUR)
    item = _item(decisions=(parked,), blocked_at=NOW - 2 * HOUR)

    held = _derive(replace(item, parked=(fact,)))
    released = _derive(item)

    assert held.state is CustodyState.HELD
    assert released.state is CustodyState.UNOWNED
    assert "did not take effect: parked, the orchestrator stopped retrying it" in released.reason


def test_a_later_applied_remedy_speaks_for_the_item_over_an_earlier_refusal() -> None:
    refused = _linked(
        _decision("release_withheld_review", action_id="A1", at=NOW - 3 * HOUR),
        CharterExecutionResult.REFUSED, "stale precondition: work_claimed",
    )
    applied = _linked(_decision("release_withheld_review", action_id="A2", at=NOW - HOUR))

    custody = _derive(_item(decisions=(applied, refused), blocked_at=NOW - 4 * HOUR))

    assert custody.state is CustodyState.VERIFY
    assert custody.charter is not None and custody.charter.decision_id == applied.decision_id


def test_a_remedy_nothing_ties_to_this_block_is_named_but_not_verified() -> None:
    executed = _linked(_decision("recover_validated_work"))

    custody = _derive(_item(decisions=(executed,)))

    assert custody.state is CustodyState.UNOWNED
    assert "last applied recover validated work" in custody.reason
    assert "nothing ties that remedy to this block" in custody.reason


def test_a_follow_up_filed_for_the_item_is_not_a_remedy_of_its_block() -> None:
    follow_up = _decision("create_issue", target=None)

    custody = _derive(_item(decisions=(follow_up,), blocked_at=NOW - 2 * HOUR))

    assert custody.state is CustodyState.UNOWNED
    assert "last applied" not in custody.reason


def test_charter_advice_older_than_the_block_does_not_hold_it() -> None:
    narrow = TechLeadCharter(
        roles={
            **TechLeadCharter.default().roles,
            CharterRole.FLOW: RoleCharter(
                depth=CharterDepth.WORKAROUND, authority=CharterAuthority.EXECUTE
            ),
        }
    )
    advice = _decision("create_issue", charter=narrow, at=NOW - 5 * HOUR)

    custody = _derive(_item(decisions=(advice,), blocked_at=NOW - HOUR))

    assert custody.state is CustodyState.UNOWNED
    assert custody.charter is None
    undated_block = _derive(_item(decisions=(advice,)))
    assert undated_block.state is CustodyState.UNOWNED


def test_a_remedy_older_than_the_block_is_not_this_block_s_owner() -> None:
    executed = _linked(_decision("recover_validated_work", at=NOW - 5 * HOUR))

    custody = _derive(_item(decisions=(executed,), blocked_at=NOW - HOUR))

    assert custody.state is CustodyState.UNOWNED


def test_charter_advice_beyond_depth_is_held_by_the_charter() -> None:
    narrow = TechLeadCharter(
        roles={
            **TechLeadCharter.default().roles,
            CharterRole.FLOW: RoleCharter(
                depth=CharterDepth.WORKAROUND, authority=CharterAuthority.EXECUTE
            ),
        }
    )
    advice = _decision("create_issue", charter=narrow)
    assert advice.outcome.value == "advice_only"

    custody = _derive(_item(decisions=(advice,), blocked_at=NOW - 2 * HOUR))

    assert custody.state is CustodyState.HELD
    assert custody.charter is not None
    assert custody.charter.reason_code == "beyond_depth"
    assert custody.charter.role_depth == "workaround"
    assert custody.charter.required_depth == "fix"


def test_advice_and_floors_are_not_remedies() -> None:
    comment = _decision("post_comment", action_id="A1")
    escalation = _decision("escalate_to_human", action_id="A2")

    assert _derive(_item(decisions=(comment, escalation))).state is CustodyState.UNOWNED


# -- precedence -------------------------------------------------------------------


def test_live_work_outranks_everything_a_label_says() -> None:
    custody = _derive(
        _item(
            labels=NEEDS_HUMAN,
            needs_human_causes=frozenset({NeedsHumanCause.AGENT_COMPLETION}),
            tech_lead_session=ActiveWork("tech-lead session", NOW),
        )
    )

    assert custody.state is CustodyState.INVESTIGATING


def test_a_person_s_answer_outranks_queued_work_it_blocks() -> None:
    custody = _derive(
        _item(
            labels=NEEDS_HUMAN,
            needs_human_causes=frozenset({NeedsHumanCause.AGENT_COMPLETION}),
            queued_fix=ActiveWork("rework of its PR"),
        )
    )

    assert custody.state is CustodyState.WAITING_ON_YOU


# -- fail visible -------------------------------------------------------------------


def test_an_unreadable_source_is_custody_unknown_not_a_guess() -> None:
    custody = _derive(
        _item(
            tech_lead_session=ActiveWork("tech-lead session", NOW),
            unreadable=("charter decision ledger could not be read",),
        )
    )

    assert custody.state is CustodyState.UNOWNED
    assert custody.reason == (
        f"{CUSTODY_UNKNOWN}: charter decision ledger could not be read"
    )
    assert custody.needs_attention


def test_unobserved_labels_are_custody_unknown_once_labels_would_decide() -> None:
    assert _derive(_item(labels=None)).reason == (
        f"{CUSTODY_UNKNOWN}: this refresh did not observe the issue's labels"
    )
    # ...but live work is certain whatever the labels say.
    assert (
        _derive(_item(labels=None, active_fix=ActiveWork("coding session", NOW))).state
        is CustodyState.BEING_FIXED
    )


# -- staleness ------------------------------------------------------------------------


def test_staleness_is_time_in_state_past_that_state_s_threshold() -> None:
    thresholds = CustodyStaleThresholds(
        by_state={
            **{state: timedelta(hours=2) for state in OWNED_CUSTODY_STATES},
            CustodyState.INVESTIGATING: timedelta(minutes=30),
        }
    )
    young = replace(_item(), tech_lead_session=ActiveWork("tech-lead session", NOW - timedelta(minutes=29)))
    old = replace(young, tech_lead_session=ActiveWork("tech-lead session", NOW - timedelta(minutes=31)))

    assert not derive_item_custody(young, BOARD, thresholds).stale
    stale = derive_item_custody(old, BOARD, thresholds)
    assert stale.stale and stale.needs_attention
    assert stale.stale_after == timedelta(minutes=30)


def test_an_undated_state_is_never_judged_stale_on_a_guess() -> None:
    custody = _derive(_item(queued_fix=ActiveWork("code review of its PR")))

    assert custody.clock is None
    assert not custody.stale and not custody.needs_attention


def test_the_board_counts_unowned_and_stale_items() -> None:
    items = (
        _derive(_item(issue_number=1)),
        _derive(_item(issue_number=2, active_fix=ActiveWork("coding session", NOW - 3 * HOUR))),
        _derive(_item(issue_number=3, active_fix=ActiveWork("coding session", NOW))),
    )
    board = BlockedCustodyBoard(items=items)

    assert (board.unowned_count, board.stale_count, board.needs_attention_count) == (1, 1, 2)
    assert board.for_issue(2).stale
    with pytest.raises(KeyError):
        board.for_issue(99)


def test_thresholds_must_cover_every_owned_state() -> None:
    partial = {CustodyState.HELD: HOUR}
    with pytest.raises(ValueError, match="missing state"):
        CustodyStaleThresholds(by_state=partial)
    with pytest.raises(ValueError, match="UNOWNED has no staleness threshold"):
        CustodyStaleThresholds(
            by_state={**THRESHOLDS.by_state, CustodyState.UNOWNED: HOUR}
        )


def test_a_policy_needs_human_the_sweep_is_still_retrying_is_queued() -> None:
    """The sweep treats the label as eligible while its budget lasts."""
    causes = frozenset({NeedsHumanCause.SESSION_LIFECYCLE})

    custody = _derive(_item(labels=NEEDS_HUMAN, needs_human_causes=causes, sweep_attempts=1))

    assert custody.state is CustodyState.QUEUED_FOR_TECH_LEAD
    assert "failed cycles 1 of 3" in custody.reason
    off = replace(BOARD, sweep=replace(SWEEP_ON, enabled=False, next_due_at=None))
    assert (
        _derive(_item(labels=NEEDS_HUMAN, needs_human_causes=causes, sweep_attempts=1), off).state
        is CustodyState.HELD
    )


def test_a_proposal_is_explained_only_by_the_decision_linked_to_it() -> None:
    earlier = _decision(
        "kill_hung_session",
        ceiling=CharterAuthority.PROPOSE,
        tracks_proposal=True,
        proposal_issue_number=10,
        action_id="A1",
    )

    custody = _derive(
        _item(
            proposals=(OpenProposal(20, "kill_hung_session", NOW - HOUR),),
            decisions=(earlier,),
        )
    )

    assert custody.state is CustodyState.WAITING_ON_YOU
    assert "Proposal #20" in custody.reason
    assert custody.charter is None


def test_the_latest_effect_decides_verify_not_the_latest_decision() -> None:
    from issue_orchestrator.domain.tech_lead_charter_decisions import CharterProposalLifecycle

    approved = _decision(
        "kill_hung_session",
        ceiling=CharterAuthority.PROPOSE,
        tracks_proposal=True,
        at=NOW - 4 * HOUR,
        action_id="A1",
    ).with_lifecycle(
        CharterProposalLifecycle.APPROVED_APPLIED, at=(NOW - HOUR).isoformat(), proposal_issue_number=700
    )
    executed = _linked(_decision("recover_validated_work", at=NOW - 3 * HOUR, action_id="A2"))

    custody = _derive(_item(decisions=(executed, approved), blocked_at=NOW - 5 * HOUR))

    assert custody.state is CustodyState.VERIFY
    assert custody.charter is not None and custody.charter.decision_id == approved.decision_id
    assert custody.clock is not None and custody.clock.since == NOW - HOUR
