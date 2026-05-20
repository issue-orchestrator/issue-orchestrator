"""Review artifact reader command tests."""

from __future__ import annotations

import json
from pathlib import Path

from issue_orchestrator.domain.review_artifacts import (
    REVIEW_DECISION_ARTIFACT,
    REVIEW_REPORT_ARTIFACT,
)
from issue_orchestrator.execution.review_artifact_reader import (
    ManifestReviewArtifactReader,
)
from issue_orchestrator.ports.review_artifact_reader import ReviewArtifactReadCommand


def _turns_dir(run_dir: Path) -> Path:
    turns = run_dir / "review-exchange" / "turns"
    turns.mkdir(parents=True)
    return turns


def test_review_artifact_read_command_returns_exact_report_content(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    report = _turns_dir(run_dir) / "round-001.reviewer.attempt-001.review-report.md"
    report_text = "# Review Report\n\n## N1\n\nRename helper.\n"
    report.write_text(report_text, encoding="utf-8")

    content = ManifestReviewArtifactReader().read_review_artifact(
        ReviewArtifactReadCommand(
            issue_number=4057,
            run_dir=run_dir,
            artifact_path=str(report),
            artifact_type=REVIEW_REPORT_ARTIFACT,
        )
    )

    assert content.issue_number == 4057
    assert content.artifact_path == report
    assert content.artifact_type == REVIEW_REPORT_ARTIFACT
    assert content.content_type == "text/markdown"
    assert content.content == report_text


def test_review_artifact_read_command_returns_exact_decision_json(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    decision = _turns_dir(run_dir) / "round-001.reviewer.attempt-001.review-decision.json"
    decision_payload = {
        "schema_version": 1,
        "verdict": "approved",
        "nits": [{"id": "N1", "title": "Rename helper"}],
    }
    decision_text = json.dumps(decision_payload, indent=2, sort_keys=True) + "\n"
    decision.write_text(decision_text, encoding="utf-8")

    content = ManifestReviewArtifactReader().read_review_artifact(
        ReviewArtifactReadCommand(
            issue_number=4057,
            run_dir=run_dir,
            artifact_path=str(decision),
            artifact_type=REVIEW_DECISION_ARTIFACT,
        )
    )

    assert content.issue_number == 4057
    assert content.artifact_path == decision
    assert content.artifact_type == REVIEW_DECISION_ARTIFACT
    assert content.content_type == "application/json"
    assert json.loads(content.content) == decision_payload
