"""Tests for the untrusted tech_lead artifact pair loader."""

import json
from pathlib import Path

from issue_orchestrator.control.tech_lead_decision_loader import (
    TechLeadDecisionLoadFailure,
    load_tech_lead_artifact_pair,
)


def _valid_decision_payload() -> dict:
    return {
        "schema_version": 1,
        "summary": "Two sessions hung on the same provider outage.",
        "findings": [
            {
                "id": "T1",
                "title": "Provider outage stalled sessions",
                "classification": "infra",
                "evidence": ["log:orchestrator.log:1023"],
            }
        ],
        "proposed_actions": [
            {
                "id": "A1",
                "action_type": "post_comment",
                "target_number": 42,
                "body": "Diagnosis: provider outage.",
                "finding_ids": ["T1"],
            }
        ],
    }


def _write_pair(
    tmp_path: Path,
    *,
    decision: dict | str | None = None,
    report: str | None = "Report mentions T1 and A1.",
) -> tuple[Path, Path]:
    decision_path = tmp_path / "tech-lead-decision.json"
    report_path = tmp_path / "tech-lead-report.md"
    if decision is not None:
        text = decision if isinstance(decision, str) else json.dumps(decision)
        decision_path.write_text(text)
    if report is not None:
        report_path.write_text(report)
    return decision_path, report_path


def test_missing_decision_file(tmp_path: Path) -> None:
    decision_path, report_path = _write_pair(tmp_path, decision=None)

    result = load_tech_lead_artifact_pair(decision_path, report_path)

    assert not result.ok
    assert result.failure == TechLeadDecisionLoadFailure.MISSING_DECISION
    assert "tech-lead-decision.json" in result.detail


def test_empty_decision_file_is_missing(tmp_path: Path) -> None:
    decision_path, report_path = _write_pair(tmp_path, decision="")

    result = load_tech_lead_artifact_pair(decision_path, report_path)

    assert result.failure == TechLeadDecisionLoadFailure.MISSING_DECISION


def test_missing_report_file(tmp_path: Path) -> None:
    decision_path, report_path = _write_pair(
        tmp_path, decision=_valid_decision_payload(), report=None
    )

    result = load_tech_lead_artifact_pair(decision_path, report_path)

    assert not result.ok
    assert result.failure == TechLeadDecisionLoadFailure.MISSING_REPORT
    assert "tech-lead-report.md" in result.detail


def test_oversized_decision_file(tmp_path: Path) -> None:
    decision_path, report_path = _write_pair(
        tmp_path, decision="x" * (2 * 1024 * 1024 + 1)
    )

    result = load_tech_lead_artifact_pair(decision_path, report_path)

    assert not result.ok
    assert result.failure == TechLeadDecisionLoadFailure.TOO_LARGE
    assert "tech-lead-decision.json" in result.detail


def test_oversized_report_file(tmp_path: Path) -> None:
    decision_path, report_path = _write_pair(
        tmp_path,
        decision=_valid_decision_payload(),
        report="T1 A1 " + "x" * (2 * 1024 * 1024),
    )

    result = load_tech_lead_artifact_pair(decision_path, report_path)

    assert result.failure == TechLeadDecisionLoadFailure.TOO_LARGE
    assert "tech-lead-report.md" in result.detail


def test_invalid_json(tmp_path: Path) -> None:
    decision_path, report_path = _write_pair(tmp_path, decision="{not json")

    result = load_tech_lead_artifact_pair(decision_path, report_path)

    assert not result.ok
    assert result.failure == TechLeadDecisionLoadFailure.INVALID_JSON
    assert "Invalid JSON" in result.detail


def test_contract_violation_bad_action_type(tmp_path: Path) -> None:
    payload = _valid_decision_payload()
    payload["proposed_actions"][0]["action_type"] = "merge_pr"
    decision_path, report_path = _write_pair(tmp_path, decision=payload)

    result = load_tech_lead_artifact_pair(decision_path, report_path)

    assert not result.ok
    assert result.failure == TechLeadDecisionLoadFailure.CONTRACT_VIOLATION
    assert "invalid action_type" in result.detail


def test_contract_violation_report_missing_id(tmp_path: Path) -> None:
    decision_path, report_path = _write_pair(
        tmp_path,
        decision=_valid_decision_payload(),
        report="Report mentions T1 only.",
    )

    result = load_tech_lead_artifact_pair(decision_path, report_path)

    assert not result.ok
    assert result.failure == TechLeadDecisionLoadFailure.CONTRACT_VIOLATION
    assert "A1" in result.detail


def test_happy_path(tmp_path: Path) -> None:
    decision_path, report_path = _write_pair(tmp_path, decision=_valid_decision_payload())

    result = load_tech_lead_artifact_pair(decision_path, report_path)

    assert result.ok
    assert result.failure is None
    assert result.detail == ""
    assert result.decision is not None
    assert result.decision.summary.startswith("Two sessions hung")
    assert [f.id for f in result.decision.findings] == ["T1"]
    assert [a.id for a in result.decision.proposed_actions] == ["A1"]
