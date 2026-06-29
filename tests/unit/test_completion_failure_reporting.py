"""Direct tests for completion failure-reporting comments."""

from pathlib import Path

from issue_orchestrator.control.completion_failure_reporting import (
    build_cleanup_failure_comment,
    build_gate_failure_comment,
    build_processing_failure_comment,
    build_review_exchange_recovery_note,
)


def test_build_cleanup_failure_comment_includes_diagnostic_reference(tmp_path: Path) -> None:
    record_path = tmp_path / ".issue-orchestrator" / "completion.json"
    record_path.parent.mkdir()
    record_path.write_text("{}")

    comment = build_cleanup_failure_comment(
        issue_number=123,
        worktree=tmp_path,
        record_path=record_path,
    )

    assert "WARNING: Cleanup incomplete" in comment
    assert "Diagnostic file:" in comment
    diagnostics = tmp_path / ".issue-orchestrator" / "diagnostics"
    assert list(diagnostics.glob("*completion-cleanup-issue-123.json"))


def test_build_gate_failure_comment_names_reason_and_label() -> None:
    comment = build_gate_failure_comment(
        gate_reason="Tests failed",
        validation_failed_label="validation-failed",
    )

    assert "## Validation Failed" in comment
    assert "- Reason: Tests failed" in comment
    assert "- Label added: `validation-failed`" in comment


def test_build_processing_failure_comment_includes_primary_error_and_diagnostic() -> None:
    comment = build_processing_failure_comment(
        errors=["push failed", "create PR failed"],
        actions_taken=["Committed changes", "Pushed branch"],
        diagnostic_path=".issue-orchestrator/diagnostics/failure.json",
    )

    assert "- Primary error: push failed" in comment
    assert "- Actions completed before failure: Committed changes, Pushed branch" in comment
    assert "- Diagnostic file: `.issue-orchestrator/diagnostics/failure.json`" in comment


def test_build_processing_failure_comment_handles_empty_errors() -> None:
    comment = build_processing_failure_comment(
        errors=[],
        actions_taken=[],
        diagnostic_path=None,
    )

    assert "- Primary error: Unknown processing error" in comment


def test_review_exchange_recovery_note_returned_for_exchange_errors() -> None:
    note = build_review_exchange_recovery_note(
        ["review_exchange: background job raised: checkout failed"]
    )

    assert note is not None
    assert "review-exchange finalization" in note
    assert "not a coding failure" in note
    assert "git rm --cached" in note


def test_review_exchange_recovery_note_absent_for_other_errors() -> None:
    assert build_review_exchange_recovery_note(["push failed"]) is None
    assert build_review_exchange_recovery_note([]) is None


def test_processing_failure_comment_appends_recovery_note_for_exchange_failures() -> None:
    comment = build_processing_failure_comment(
        errors=["review_exchange: background job raised: reviewer checkout failed"],
        actions_taken=[],
        diagnostic_path=None,
    )

    assert "## Orchestrator Processing Failed" in comment
    assert "### Recovery: review-exchange finalization failed" in comment


def test_processing_failure_comment_no_recovery_note_for_publish_failures() -> None:
    comment = build_processing_failure_comment(
        errors=["push_branch: remote rejected"],
        actions_taken=[],
        diagnostic_path=None,
    )

    assert "Recovery: review-exchange" not in comment
