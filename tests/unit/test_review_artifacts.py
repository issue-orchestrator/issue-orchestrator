"""Review artifact pair contract tests."""

from __future__ import annotations

import json

import pytest

from issue_orchestrator.domain.review_artifacts import (
    ReviewDecision,
    persist_review_artifact_pair,
    review_artifacts_from_summary,
    review_requires_nit_rework,
)


def _no_issues_abstraction() -> dict[str, object]:
    return {"status": "no_issues", "findings": []}


def test_persists_report_and_authoritative_decision_json(tmp_path):
    report = tmp_path / "authored.md"
    report.write_text(
        "# Review\n\n## N1\n\nLooks good after a rename.\n", encoding="utf-8"
    )

    decision = ReviewDecision.from_agent_payload(
        {
            "decision": {
                "verdict": "approved",
                "risk": "low",
                "nits": [{"id": "N1", "title": "Rename helper"}],
                "tests_reviewed": ["pytest tests/unit/test_x.py -q"],
                "abstraction_review": _no_issues_abstraction(),
            }
        },
        response_type="ok",
        response_text="Approved with one nit.",
        nit_policy="surface",
    )

    pair = persist_review_artifact_pair(
        report_path=tmp_path / "review-report.md",
        decision_path=tmp_path / "review-decision.json",
        decision=decision,
        authored_report_path=report,
    )

    payload = json.loads(pair.decision_path.read_text(encoding="utf-8"))
    assert pair.report_path.read_text(encoding="utf-8").startswith("# Review")
    assert payload["verdict"] == "approved"
    assert payload["nits"][0]["id"] == "N1"
    assert payload["abstraction_review"] == {
        "status": "no_issues",
        "findings": [],
    }
    assert payload["report_sha256"]
    assert pair.to_event_artifacts()[0]["type"] == "review_report"
    assert pair.to_event_artifacts()[1]["type"] == "review_decision"


def test_changes_requested_legacy_payload_synthesizes_blocker(tmp_path):
    decision = ReviewDecision.from_agent_payload(
        {"response_type": "changes_requested", "response_text": "Missing tests"},
        response_type="changes_requested",
        response_text="Missing tests",
        nit_policy="surface",
    )

    assert decision.verdict == "changes_requested"
    assert decision.blocking_findings[0].id == "F1"
    assert decision.blocking_findings[0].title == "Missing tests"
    assert decision.abstraction_review.status == "no_issues"


def test_structured_decision_requires_abstraction_review():
    with pytest.raises(ValueError, match="abstraction_review is required"):
        ReviewDecision.from_agent_payload(
            {"decision": {"verdict": "approved", "risk": "low"}},
            response_type="ok",
            response_text="Approved.",
            nit_policy="surface",
        )


def test_structured_decision_rejects_invalid_abstraction_review_status():
    with pytest.raises(ValueError, match="invalid abstraction_review.status"):
        ReviewDecision.from_agent_payload(
            {
                "decision": {
                    "verdict": "approved",
                    "risk": "low",
                    "abstraction_review": {
                        "status": "changes-requested",
                        "findings": [],
                    },
                }
            },
            response_type="ok",
            response_text="Approved.",
            nit_policy="surface",
        )


def test_report_must_reference_every_json_item_id(tmp_path):
    report = tmp_path / "authored.md"
    report.write_text(
        "# Review\n\nNit is described without the id.\n", encoding="utf-8"
    )
    decision = ReviewDecision.from_agent_payload(
        {
            "decision": {
                "verdict": "approved",
                "nits": [{"id": "N1", "title": "Small nit"}],
                "abstraction_review": _no_issues_abstraction(),
            }
        },
        response_type="ok",
        response_text="Approved.",
        nit_policy="surface",
    )

    with pytest.raises(ValueError, match="N1"):
        persist_review_artifact_pair(
            report_path=tmp_path / "review-report.md",
            decision_path=tmp_path / "review-decision.json",
            decision=decision,
            authored_report_path=report,
        )


def test_address_policy_routes_approved_nits_to_rework():
    decision = ReviewDecision.from_agent_payload(
        {
            "decision": {
                "verdict": "approved",
                "nits": [{"id": "N1", "title": "Rename helper"}],
                "abstraction_review": _no_issues_abstraction(),
            }
        },
        response_type="ok",
        response_text="Approved.",
        nit_policy="address",
    )

    assert review_requires_nit_rework(decision) is True


def test_abstraction_review_is_authoritative_in_decision_json(tmp_path):
    report = tmp_path / "authored.md"
    report.write_text(
        "# Review\n\n## F1\n\nFix access.\n\n## A1\n\nUse the command port owner.\n",
        encoding="utf-8",
    )
    decision = ReviewDecision.from_agent_payload(
        {
            "decision": {
                "verdict": "changes_requested",
                "risk": "medium",
                "blocking_findings": [{"id": "F1", "title": "Fix access"}],
                "abstraction_review": {
                    "status": "changes_requested",
                    "findings": [
                        {
                            "id": "A1",
                            "title": "Use the command port owner",
                            "rationale": "The direct route duplicates access policy.",
                        }
                    ],
                },
            }
        },
        response_type="changes_requested",
        response_text="Fix access through the owner abstraction.",
        nit_policy="surface",
    )

    pair = persist_review_artifact_pair(
        report_path=tmp_path / "review-report.md",
        decision_path=tmp_path / "review-decision.json",
        decision=decision,
        authored_report_path=report,
    )
    payload = json.loads(pair.decision_path.read_text(encoding="utf-8"))

    assert payload["abstraction_review"]["status"] == "changes_requested"
    assert payload["abstraction_review"]["findings"][0]["id"] == "A1"
    assert (
        payload["abstraction_review"]["findings"][0]["title"]
        == "Use the command port owner"
    )


def test_approved_decision_rejects_required_abstraction_changes():
    with pytest.raises(ValueError, match="abstraction changes_requested"):
        ReviewDecision.from_agent_payload(
            {
                "decision": {
                    "verdict": "approved",
                    "risk": "low",
                    "abstraction_review": {
                        "status": "changes_requested",
                        "findings": [{"id": "A1", "title": "Add owner port"}],
                    },
                }
            },
            response_type="ok",
            response_text="Approved.",
            nit_policy="surface",
        )


def test_deferred_abstraction_review_round_trips_with_follow_up(tmp_path):
    decision = ReviewDecision.from_agent_payload(
        {
            "decision": {
                "verdict": "approved",
                "risk": "low",
                "abstraction_review": {
                    "status": "deferred",
                    "findings": [],
                    "follow_up_issue_url": "https://github.com/org/repo/issues/123",
                },
            }
        },
        response_type="ok",
        response_text="Approved with deferred abstraction follow-up.",
        nit_policy="surface",
    )

    pair = persist_review_artifact_pair(
        report_path=tmp_path / "review-report.md",
        decision_path=tmp_path / "review-decision.json",
        decision=decision,
        authored_report_path=None,
    )
    payload = json.loads(pair.decision_path.read_text(encoding="utf-8"))

    assert payload["abstraction_review"] == {
        "status": "deferred",
        "findings": [],
        "follow_up_issue_url": "https://github.com/org/repo/issues/123",
    }
    assert "Deferred to follow-up issue." in pair.report_path.read_text(
        encoding="utf-8"
    )


def test_deferred_abstraction_review_requires_follow_up_issue_url():
    with pytest.raises(ValueError, match="follow_up_issue_url"):
        ReviewDecision.from_agent_payload(
            {
                "decision": {
                    "verdict": "approved",
                    "risk": "low",
                    "abstraction_review": {"status": "deferred", "findings": []},
                }
            },
            response_type="ok",
            response_text="Approved.",
            nit_policy="surface",
        )


def test_report_must_reference_abstraction_item_id(tmp_path):
    report = tmp_path / "authored.md"
    report.write_text("# Review\n\n## F1\n\nFix access.\n", encoding="utf-8")
    decision = ReviewDecision.from_agent_payload(
        {
            "decision": {
                "verdict": "changes_requested",
                "risk": "medium",
                "blocking_findings": [{"id": "F1", "title": "Fix access"}],
                "abstraction_review": {
                    "status": "changes_requested",
                    "findings": [{"id": "A1", "title": "Use the owner port"}],
                },
            }
        },
        response_type="changes_requested",
        response_text="Fix access through the owner abstraction.",
        nit_policy="surface",
    )

    with pytest.raises(ValueError, match="A1"):
        persist_review_artifact_pair(
            report_path=tmp_path / "review-report.md",
            decision_path=tmp_path / "review-decision.json",
            decision=decision,
            authored_report_path=report,
        )


def test_review_artifacts_from_summary_filters_to_valid_refs():
    artifacts = review_artifacts_from_summary(
        {
            "artifacts": [
                {
                    "type": "review_report",
                    "label": "Review report",
                    "value": "/tmp/run/review-exchange/turns/r.review-report.md",
                    "render_mode": "markdown",
                    "ignored": {"not": "a string"},
                },
                {"type": "review_decision", "label": "", "value": "/tmp/x"},
                "not-a-dict",
            ]
        }
    )

    assert artifacts == [
        {
            "type": "review_report",
            "label": "Review report",
            "value": "/tmp/run/review-exchange/turns/r.review-report.md",
            "render_mode": "markdown",
        }
    ]
