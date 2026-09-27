"""What the engine did with each proposed tech-lead action, from its events.

Payload shapes are the emitters' own: ``publish_proposal_surfaced``
(``control/tech_lead_reset_retry.py``), the act-level executors, and
``record_decision_applied`` (``control/tech_lead_decision_receipt.py``).
"""

from __future__ import annotations

import pytest

from issue_orchestrator.testing.exam import TechLeadActionDisposition as D
from issue_orchestrator.testing.exam.tech_lead import ProposedAction, resolve_dispositions

ANCHOR = 7313
ESCALATE = ProposedAction("A1", "escalate_to_human")
COMMENT = ProposedAction("A2", "post_comment")
RESET = ProposedAction("A3", "reset_retry")


def _event(name: str, **payload: object) -> dict:
    return {"type": name, "issue_key": str(ANCHOR), "payload": {"issue_number": ANCHOR, **payload}}


def test_receipts_are_matched_by_action_id_or_by_type_when_they_carry_no_id() -> None:
    events = [
        _event("tech_lead.action_executed", action="escalate_to_human"),
        _event("tech_lead.action_executed", action="post_comment", tech_lead_action_id="A2"),
        _event("tech_lead.action_proposed", action_id="A3", proposal_type="reset_retry", mode="shadow"),
    ]

    assert resolve_dispositions(
        events, anchor_issue_number=ANCHOR, anchor_shared=False, run_failed=False, actions=[ESCALATE, COMMENT, RESET]
    ) == (D.EXECUTED, D.EXECUTED, D.PROPOSED)


def test_an_act_level_execution_is_matched_to_its_own_action_id_only() -> None:
    other_reset = ProposedAction("A4", "reset_retry")
    events = [_event("tech_lead.action_executed", action_id="A3", proposal_type="reset_retry")]

    assert resolve_dispositions(
        events, anchor_issue_number=ANCHOR, anchor_shared=False, run_failed=False, actions=[RESET, other_reset]
    ) == (D.EXECUTED, D.UNKNOWN)


def test_another_runs_events_do_not_count() -> None:
    other = {"type": "tech_lead.action_executed", "payload": {"issue_number": 9999, "action": "escalate_to_human"}}
    rejected_elsewhere = {"type": "tech_lead.decision_rejected", "payload": {"issue_number": 9999}}

    assert resolve_dispositions(
        [other, rejected_elsewhere], anchor_issue_number=ANCHOR, anchor_shared=False, run_failed=False, actions=[ESCALATE]
    ) == (D.UNKNOWN,)


def test_a_rejected_decision_or_failed_run_rejects_every_action() -> None:
    rejected = [_event("tech_lead.decision_rejected", mode="rejected", proposal_type="decision")]

    assert resolve_dispositions(
        rejected, anchor_issue_number=ANCHOR, anchor_shared=False, run_failed=False, actions=[ESCALATE, COMMENT]
    ) == (D.REJECTED, D.REJECTED)
    assert resolve_dispositions(
        [], anchor_issue_number=ANCHOR, anchor_shared=False, run_failed=True, actions=[ESCALATE]
    ) == (D.REJECTED,)


def test_a_comment_receipt_names_which_comment_landed() -> None:
    """Two comments, one receipt carrying ``tech_lead_action_id``: only that one landed."""
    second_comment = ProposedAction("A5", "post_comment")
    events = [_event("tech_lead.action_executed", action="post_comment", tech_lead_action_id="A2")]

    assert resolve_dispositions(
        events, anchor_issue_number=ANCHOR, anchor_shared=False, run_failed=False, actions=[COMMENT, second_comment]
    ) == (D.EXECUTED, D.UNKNOWN)



def test_runs_sharing_an_anchor_resolve_unknown_rather_than_borrow_receipts() -> None:
    """Round 1 F4: events carry no run id or time; two runs of one anchor are ambiguous."""
    events = [
        _event("tech_lead.decision_rejected", mode="rejected"),
        _event("tech_lead.action_executed", action_id="A1", proposal_type="escalate_to_human"),
    ]
    assert resolve_dispositions(
        events, anchor_issue_number=ANCHOR, anchor_shared=True, run_failed=False, actions=[ESCALATE, COMMENT]
    ) == (D.UNKNOWN, D.UNKNOWN)


def test_an_execution_survives_its_run_failing() -> None:
    """Round 1 F4: a failed run can still have executed a kill before failing."""
    kill = ProposedAction("A1", "kill_hung_session")
    events = [_event("tech_lead.action_executed", action_id="A1", proposal_type="kill_hung_session")]
    assert resolve_dispositions(
        events, anchor_issue_number=ANCHOR, anchor_shared=False, run_failed=True, actions=[kill, COMMENT]
    ) == (D.EXECUTED, D.REJECTED)


def test_every_execution_receipt_is_kept_unattributed() -> None:
    from issue_orchestrator.testing.exam import TechLeadReceipt
    from issue_orchestrator.testing.exam.tech_lead import executed_receipts

    events = [
        _event("tech_lead.action_executed", action_id="A3", proposal_type="reset_retry", target_number=ANCHOR),
        _event("tech_lead.action_executed", action="escalate_to_human"),
        _event("tech_lead.action_proposed", action_id="A4", proposal_type="reset_retry"),
        {"type": "tech_lead.action_executed", "payload": {"issue_number": 42, "action": "post_comment", "target_number": 5204}},
    ]
    assert executed_receipts(events) == (
        TechLeadReceipt("reset_retry", ANCHOR, ANCHOR),
        TechLeadReceipt("escalate_to_human", None, ANCHOR),
        TechLeadReceipt("post_comment", 5204, 42),
    )
    with pytest.raises(ValueError, match="without anchor/action"):
        executed_receipts([{"type": "tech_lead.action_executed", "payload": {"action": "post_comment"}}])


def test_a_type_only_receipt_credits_no_action_when_several_share_its_type() -> None:
    """Round 2 F1: two escalations, one id-less receipt — it could be either."""
    generic = ProposedAction("A1", "escalate_to_human")
    release = ProposedAction("A2", "escalate_to_human")
    events = [_event("tech_lead.action_executed", action="escalate_to_human")]

    assert resolve_dispositions(
        events, anchor_issue_number=ANCHOR, anchor_shared=False, run_failed=False, actions=[generic, release]
    ) == (D.UNKNOWN, D.UNKNOWN)
    # An id still identifies its action however many share the type.
    events = [_event("tech_lead.action_executed", action="escalate_to_human", tech_lead_action_id="A2")]
    assert resolve_dispositions(
        events, anchor_issue_number=ANCHOR, anchor_shared=False, run_failed=False, actions=[generic, release]
    ) == (D.UNKNOWN, D.EXECUTED)
