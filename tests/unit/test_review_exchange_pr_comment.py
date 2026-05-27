"""Tests for review-exchange PR comment rendering."""

from __future__ import annotations

import json
from pathlib import Path

from issue_orchestrator.control.review_exchange_pr_comment import (
    build_review_exchange_pr_comment_body,
)
from issue_orchestrator.domain.review_artifacts import REVIEW_REPORT_ARTIFACT
from issue_orchestrator.execution.review_artifact_reader import ManifestReviewArtifactReader


def test_transcript_ignores_exchange_dir_outside_run_dir(tmp_path: Path) -> None:
    """Spoofed exchange dirs must not contribute transcript text."""
    run_dir = tmp_path / "run"
    run_turns = run_dir / "review-exchange" / "turns"
    run_turns.mkdir(parents=True)
    fallback_report = run_turns / "round-1-reviewer-attempt-1.review-report.md"
    fallback_report.write_text("# Final Report\n\nScoped report.\n", encoding="utf-8")

    outside_exchange = tmp_path / "outside" / "review-exchange"
    outside_turns = outside_exchange / "turns"
    outside_turns.mkdir(parents=True)
    outside_report = outside_turns / "round-1-reviewer-attempt-1.review-report.md"
    outside_report.write_text("# Outside Report\n\nDo not include.\n", encoding="utf-8")
    (outside_turns / "round-1-reviewer-attempt-1.result.json").write_text(
        json.dumps({"kind": "ok", "response_text": "Outside response."}),
        encoding="utf-8",
    )

    body = build_review_exchange_pr_comment_body(
        issue_number=123,
        run_dir=run_dir,
        exchange_dir=outside_exchange,
        artifacts=[
            {
                "type": REVIEW_REPORT_ARTIFACT,
                "label": "Review report",
                "value": str(fallback_report),
                "render_mode": "markdown",
            }
        ],
        review_artifact_reader=ManifestReviewArtifactReader(),
    )

    assert body is not None
    assert body == "# Final Report\n\nScoped report."
    assert "Outside" not in body
    assert "Review Exchange Transcript" not in body
