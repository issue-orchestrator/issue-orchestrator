"""What the engine did with each proposed tech-lead action, from its events.

Payload shapes are the emitters' own: ``publish_proposal_surfaced``
(``control/tech_lead_reset_retry.py``), the act-level executors, and
``record_decision_applied`` (``control/tech_lead_decision_receipt.py``).
"""

from __future__ import annotations

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
        events, anchor_issue_number=ANCHOR, run_failed=False, actions=[ESCALATE, COMMENT, RESET]
    ) == (D.EXECUTED, D.EXECUTED, D.PROPOSED)


def test_an_act_level_execution_is_matched_to_its_own_action_id_only() -> None:
    other_reset = ProposedAction("A4", "reset_retry")
    events = [_event("tech_lead.action_executed", action_id="A3", proposal_type="reset_retry")]

    assert resolve_dispositions(
        events, anchor_issue_number=ANCHOR, run_failed=False, actions=[RESET, other_reset]
    ) == (D.EXECUTED, D.UNKNOWN)


def test_another_runs_events_do_not_count() -> None:
    other = {"type": "tech_lead.action_executed", "payload": {"issue_number": 9999, "action": "escalate_to_human"}}
    rejected_elsewhere = {"type": "tech_lead.decision_rejected", "payload": {"issue_number": 9999}}

    assert resolve_dispositions(
        [other, rejected_elsewhere], anchor_issue_number=ANCHOR, run_failed=False, actions=[ESCALATE]
    ) == (D.UNKNOWN,)


def test_a_rejected_decision_or_failed_run_rejects_every_action() -> None:
    rejected = [_event("tech_lead.decision_rejected", mode="rejected", proposal_type="decision")]

    assert resolve_dispositions(
        rejected, anchor_issue_number=ANCHOR, run_failed=False, actions=[ESCALATE, COMMENT]
    ) == (D.REJECTED, D.REJECTED)
    assert resolve_dispositions(
        [], anchor_issue_number=ANCHOR, run_failed=True, actions=[ESCALATE]
    ) == (D.REJECTED,)


def test_a_comment_receipt_names_which_comment_landed() -> None:
    """Two comments, one receipt carrying ``tech_lead_action_id``: only that one landed."""
    second_comment = ProposedAction("A5", "post_comment")
    events = [_event("tech_lead.action_executed", action="post_comment", tech_lead_action_id="A2")]

    assert resolve_dispositions(
        events, anchor_issue_number=ANCHOR, run_failed=False, actions=[COMMENT, second_comment]
    ) == (D.EXECUTED, D.UNKNOWN)
