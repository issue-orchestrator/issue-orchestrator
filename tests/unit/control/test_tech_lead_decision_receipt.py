"""Every applied tech-lead decision effect reports itself (#7080, #7262 F3).

`TECH_LEAD_ACTION_EXECUTED` used to come only from the act-level executors --
reset_retry, kill_hung_session, scoped rework, validated-work recovery -- all of
which default to `propose` authority and wait for an approval that may never
arrive. So the store held ZERO such rows while the tech lead filed follow-ups and
posted diagnoses daily, and a healthy comment-only health review read as
write-dead.
"""

from __future__ import annotations

from typing import Any, cast

from issue_orchestrator.control.actions import AddCommentAction
from issue_orchestrator.control.required_issue_comment import (
    RequiredTechLeadDiagnosisAction,
    TechLeadDecisionCommentAction,
    note_tech_lead_comment_applied,
)
from issue_orchestrator.domain.tech_lead_comment import TechLeadCommentIntent
from issue_orchestrator.events import EventName
from issue_orchestrator.ports import InMemoryEventSink


def _intent() -> TechLeadCommentIntent:
    return TechLeadCommentIntent(action_id="A3", body="The diagnosis.")


def _executed(events: InMemoryEventSink) -> list[Any]:
    return [
        event
        for event in events.events
        if event.name == str(EventName.TECH_LEAD_ACTION_EXECUTED)
    ]


class TestDecisionCommentsReportThemselves:
    def test_an_executed_decision_comment_emits_a_receipt(self) -> None:
        events = InMemoryEventSink()
        intent = _intent()

        note_tech_lead_comment_applied(
            TechLeadDecisionCommentAction(
                number=7255, comment=intent.comment, intent=intent
            ),
            events,
        )

        [receipt] = _executed(events)
        assert receipt.data["issue_number"] == 7255
        assert receipt.data["action"] == "post_comment"
        assert receipt.data["tech_lead_action_id"] == "A3"

    def test_a_required_diagnosis_emits_a_receipt(self) -> None:
        events = InMemoryEventSink()
        intent = _intent()

        note_tech_lead_comment_applied(
            RequiredTechLeadDiagnosisAction(
                number=7255, comment=intent.comment, intent=intent
            ),
            events,
        )

        assert len(_executed(events)) == 1

    def test_an_ordinary_comment_is_not_a_tech_lead_decision(self) -> None:
        # Every completion posts comments. Only the typed decision actions are
        # evidence that the TECH LEAD wrote something.
        events = InMemoryEventSink()

        note_tech_lead_comment_applied(
            AddCommentAction(number=7255, comment="## Blocked"), events
        )

        assert _executed(events) == []

    def test_no_event_sink_is_tolerated(self) -> None:
        intent = _intent()

        note_tech_lead_comment_applied(
            TechLeadDecisionCommentAction(
                number=7255, comment=intent.comment, intent=intent
            ),
            cast(Any, None),
        )
