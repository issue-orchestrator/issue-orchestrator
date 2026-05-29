"""Tests for fresh-rerun no-PR recovery policy."""

from __future__ import annotations

import pytest

from issue_orchestrator.control.fresh_rerun_no_pr import (
    try_recover_fresh_rerun_no_pr,
)
from issue_orchestrator.domain.models import RequestedAction
from issue_orchestrator.domain.review_exchange import ReviewExchangeOutcome
from issue_orchestrator.execution.session_output_adapter import FileSystemSessionOutput


@pytest.mark.parametrize(
    (
        "action",
        "exchange_mode",
        "exchange_result",
        "error",
        "manifest",
        "session_name",
    ),
    [
        pytest.param(
            RequestedAction.POST_COMMENT,
            "via-local-loop",
            ReviewExchangeOutcome(status="ok", rounds=1, reason="reviewer_ok"),
            RuntimeError("No commits between main and issue-123"),
            {"rerun_intent": "fresh_lifecycle"},
            "issue-123",
            id="wrong-action",
        ),
        pytest.param(
            RequestedAction.CREATE_PR,
            "via-draft-pr",
            ReviewExchangeOutcome(status="ok", rounds=1, reason="reviewer_ok"),
            RuntimeError("No commits between main and issue-123"),
            {"rerun_intent": "fresh_lifecycle"},
            "issue-123",
            id="non-final-exchange-mode",
        ),
        pytest.param(
            RequestedAction.CREATE_PR,
            "via-local-loop",
            None,
            RuntimeError("No commits between main and issue-123"),
            {"rerun_intent": "fresh_lifecycle"},
            "issue-123",
            id="review-not-completed",
        ),
        pytest.param(
            RequestedAction.CREATE_PR,
            "via-local-loop",
            ReviewExchangeOutcome(status="ok", rounds=1, reason="reviewer_ok"),
            RuntimeError("GitHub API unavailable"),
            {"rerun_intent": "fresh_lifecycle"},
            "issue-123",
            id="different-error",
        ),
        pytest.param(
            RequestedAction.CREATE_PR,
            "via-local-loop",
            ReviewExchangeOutcome(status="ok", rounds=1, reason="reviewer_ok"),
            RuntimeError("No commits between main and issue-123"),
            {},
            "issue-123",
            id="not-fresh-rerun",
        ),
        pytest.param(
            RequestedAction.CREATE_PR,
            "via-local-loop",
            ReviewExchangeOutcome(status="ok", rounds=1, reason="reviewer_ok"),
            RuntimeError("No commits between main and issue-123"),
            {"rerun_intent": "fresh_lifecycle"},
            None,
            id="missing-session-name",
        ),
    ],
)
def test_try_recover_fresh_rerun_no_pr_rejects_non_matching_cases(
    tmp_path,
    action: RequestedAction,
    exchange_mode: str | None,
    exchange_result: ReviewExchangeOutcome | None,
    error: Exception,
    manifest: dict[str, object],
    session_name: str | None,
) -> None:
    session_output = FileSystemSessionOutput()
    if session_name:
        run = session_output.start_run(tmp_path, session_name)
        session_output.update_manifest(run.run_dir, manifest)
    actions_taken: list[str] = []

    recovered = try_recover_fresh_rerun_no_pr(
        session_output=session_output,
        worktree=tmp_path,
        session_name=session_name,
        action=action,
        error=error,
        exchange_mode=exchange_mode,
        exchange_result=exchange_result,
        actions_taken=actions_taken,
        issue_number=123,
        branch="issue-123",
    )

    assert recovered is False
    assert actions_taken == []
