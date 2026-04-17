"""Direct tests for review/rework launch support helpers."""

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from issue_orchestrator.control.session_review_support import (
    copy_review_feedback_to_rework,
    find_review_feedback_file,
    format_reviewer_feedback,
    read_local_reviewer_feedback,
)
from issue_orchestrator.ports import ReviewState


def _feedback_file(run_dir: Path, review_issues: str = "Fix the tests") -> Path:
    feedback_file = run_dir / "reviewer-feedback.json"
    feedback_file.parent.mkdir(parents=True, exist_ok=True)
    feedback_file.write_text(
        json.dumps(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "review_issues": review_issues,
            }
        )
    )
    return feedback_file


def test_find_review_feedback_file_returns_latest_matching_review_run(tmp_path: Path) -> None:
    older = tmp_path / ".issue-orchestrator" / "sessions" / "review-7__20260101"
    newer = tmp_path / ".issue-orchestrator" / "sessions" / "review-7__20260102"
    _feedback_file(older, "older")
    expected = _feedback_file(newer, "newer")

    assert find_review_feedback_file(tmp_path, 7) == expected


def test_copy_review_feedback_to_rework_copies_latest_file(tmp_path: Path) -> None:
    source = _feedback_file(
        tmp_path / ".issue-orchestrator" / "sessions" / "review-7__20260102",
        "Use explicit assertions",
    )
    rework_run_dir = tmp_path / ".issue-orchestrator" / "sessions" / "rework-7__20260103"
    rework_run_dir.mkdir(parents=True)

    copied = copy_review_feedback_to_rework(
        worktree_path=tmp_path,
        pr_number=7,
        rework_run_dir=rework_run_dir,
    )

    assert copied == rework_run_dir / "reviewer-feedback.json"
    assert copied is not None
    assert copied.read_text() == source.read_text()


def test_read_local_reviewer_feedback_rejects_negative_cache(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    _feedback_file(run_dir)

    assert read_local_reviewer_feedback(run_dir=run_dir, cache_minutes=-1) is None


def test_read_local_reviewer_feedback_ignores_malformed_json(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "reviewer-feedback.json").write_text("{not json")

    assert read_local_reviewer_feedback(run_dir=run_dir, cache_minutes=10) is None


def test_format_reviewer_feedback_uses_local_cache_before_github(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    _feedback_file(run_dir, "Local feedback")
    repository_host = SimpleNamespace(get_pr_reviews=lambda _pr_number: [])

    result = format_reviewer_feedback(
        pr_number=7,
        repository_host=repository_host,
        cache_minutes=10,
        run_dir=run_dir,
        sleep_fn=lambda _delay: None,
    )

    assert result == "REVIEWER FEEDBACK (address these issues):\n\nLocal feedback"


def test_format_reviewer_feedback_formats_actionable_reviews(tmp_path: Path) -> None:
    repository_host = SimpleNamespace(
        get_pr_reviews=lambda _pr_number: [
            {"state": ReviewState.APPROVED.value, "body": "Looks good"},
            {
                "state": ReviewState.CHANGES_REQUESTED.value,
                "body": "Please add tests",
                "user": {"login": "reviewer"},
            },
        ]
    )

    result = format_reviewer_feedback(
        pr_number=7,
        repository_host=repository_host,
        cache_minutes=0,
        run_dir=None,
        sleep_fn=lambda _delay: None,
    )

    assert result == (
        "REVIEWER FEEDBACK (address these issues):\n"
        "\n[reviewer - CHANGES_REQUESTED]\n"
        "Please add tests"
    )
