"""Direct tests for completion failure-reporting comments."""

from pathlib import Path

import pytest

from issue_orchestrator.control.completion_failure_reporting import (
    build_cleanup_failure_comment,
    build_gate_failure_comment,
    build_processing_failure_comment,
    build_review_exchange_recovery_note,
)
from issue_orchestrator.domain.review_exchange import (
    REVIEWER_WORKTREE_CHECKOUT_FAILURE_MARKER,
)

# A reviewer-worktree checkout failure surfaced as a background-job error: it
# carries the marker that scopes the runtime-artifact recovery note (#6659).
_CHECKOUT_FAILURE_ERROR = (
    "review_exchange: background job raised: Failed to fast-forward reviewer "
    "worktree /wt-review to 6659-branch@abc123: git command failed (exit 1) "
    f"{REVIEWER_WORKTREE_CHECKOUT_FAILURE_MARKER}"
)

# Other review-exchange halts share the ``review_exchange:`` prefix but are NOT
# fixed by removing runtime artifacts, so they must not get the recovery note.
_NON_CHECKOUT_EXCHANGE_ERRORS = [
    "review_exchange: changes_requested (max rounds reached)",
    "review_exchange: 3 consecutive rounds with no coder completion",
    "review_exchange: missing exchange outcome before PR creation",
    "review_exchange: background job cancelled: orchestrator shutdown",
    "review_exchange: review exchange requires session_name",
]


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


def test_review_exchange_recovery_note_returned_for_checkout_failure() -> None:
    note = build_review_exchange_recovery_note([_CHECKOUT_FAILURE_ERROR])

    assert note is not None
    assert "review-exchange finalization" in note
    assert "not a coding failure" in note
    assert "git rm --cached" in note


def test_review_exchange_recovery_note_absent_for_other_errors() -> None:
    assert build_review_exchange_recovery_note(["push failed"]) is None
    assert build_review_exchange_recovery_note([]) is None


@pytest.mark.parametrize("error", _NON_CHECKOUT_EXCHANGE_ERRORS)
def test_recovery_note_absent_for_non_checkout_exchange_halts(error: str) -> None:
    # These share the ``review_exchange:`` prefix but are not checkout/runtime
    # artifact failures, so the runtime-artifact recovery note must not appear.
    assert build_review_exchange_recovery_note([error]) is None


def test_recovery_note_present_when_checkout_failure_mixed_with_other_errors() -> None:
    note = build_review_exchange_recovery_note(
        [
            "review_exchange: changes_requested (max rounds reached)",
            _CHECKOUT_FAILURE_ERROR,
        ]
    )

    assert note is not None


def test_processing_failure_comment_appends_recovery_note_for_checkout_failure() -> None:
    comment = build_processing_failure_comment(
        errors=[_CHECKOUT_FAILURE_ERROR],
        actions_taken=[],
        diagnostic_path=None,
    )

    assert "## Orchestrator Processing Failed" in comment
    assert "### Recovery: review-exchange finalization failed" in comment


def test_processing_failure_comment_no_recovery_note_for_non_checkout_halt() -> None:
    comment = build_processing_failure_comment(
        errors=["review_exchange: missing exchange outcome before PR creation"],
        actions_taken=[],
        diagnostic_path=None,
    )

    assert "## Orchestrator Processing Failed" in comment
    assert "Recovery: review-exchange" not in comment


def test_processing_failure_comment_no_recovery_note_for_publish_failures() -> None:
    comment = build_processing_failure_comment(
        errors=["push_branch: remote rejected"],
        actions_taken=[],
        diagnostic_path=None,
    )

    assert "Recovery: review-exchange" not in comment
