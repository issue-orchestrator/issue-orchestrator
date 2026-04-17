"""Direct tests for completion failure-reporting comments."""

from pathlib import Path

from issue_orchestrator.control.completion_failure_reporting import (
    build_cleanup_failure_comment,
    build_gate_failure_comment,
    build_processing_failure_comment,
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
