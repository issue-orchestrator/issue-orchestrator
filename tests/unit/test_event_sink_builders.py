"""Typed event builder coverage."""

from __future__ import annotations

from issue_orchestrator.events import EventName
from issue_orchestrator.ports.event_sink import (
    make_review_exchange_completed_event,
    make_review_exchange_round_completed_event,
)


def test_round_completed_builder_preserves_review_decision_fields() -> None:
    event = make_review_exchange_round_completed_event(
        {
            "issue_number": 42,
            "session_name": "review-exchange-42",
            "round_index": 1,
            "reviewer_response_type": "ok",
            "reviewer_response_text": "Approved.",
            "coder_response_type": None,
            "review_decision_verdict": "approved",
            "review_nit_policy": "address",
            "review_abstraction_status": "no_issues",
            "artifacts": [
                {
                    "type": "review_report",
                    "label": "Review report",
                    "value": "/tmp/review-report.md",
                    "render_mode": "markdown",
                },
            ],
        }
    )

    assert event.event_type is EventName.REVIEW_EXCHANGE_ROUND_COMPLETED
    assert event.data["review_decision_verdict"] == "approved"
    assert event.data["review_nit_policy"] == "address"
    assert event.data["review_abstraction_status"] == "no_issues"


def test_review_exchange_completed_builder_preserves_review_decision_fields() -> None:
    event = make_review_exchange_completed_event(
        {
            "issue_number": 42,
            "session_name": "review-exchange-42",
            "rounds": 1,
            "status": "ok",
            "reason": "reviewer_ok",
            "review_decision_verdict": "approved",
            "review_nit_policy": "address",
            "review_abstraction_status": "no_issues",
        }
    )

    assert event.event_type is EventName.REVIEW_EXCHANGE_COMPLETED
    assert event.data["review_decision_verdict"] == "approved"
    assert event.data["review_nit_policy"] == "address"
    assert event.data["review_abstraction_status"] == "no_issues"
