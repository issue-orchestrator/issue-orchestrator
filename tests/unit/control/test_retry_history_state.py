"""Tests for retry/history state mutation owner."""

from __future__ import annotations

from pathlib import Path

from issue_orchestrator.adapters.github.github_issue import GitHubIssue
from issue_orchestrator.control.retry_history_state import RetryHistoryState
from issue_orchestrator.domain.issue_key import FakeIssueKey
from issue_orchestrator.domain.models import (
    OrchestratorState,
    PendingCleanup,
    PendingReview,
    PendingRework,
    SessionHistoryEntry,
)


def _history_entry(issue_number: int) -> SessionHistoryEntry:
    return SessionHistoryEntry(
        issue_number=issue_number,
        title=f"Issue {issue_number}",
        agent_type="agent:web",
        status="failed",
        runtime_minutes=1,
    )


def test_remove_issue_from_history_removes_completed_today_gate() -> None:
    state = OrchestratorState(
        session_history=[_history_entry(1), _history_entry(2), _history_entry(1)],
        completed_today=[1, 2],
    )

    result = RetryHistoryState(state).remove_issue_from_history(1)

    assert result.removed_history_entries == 2
    assert result.removed_completed_today is True
    assert [entry.issue_number for entry in state.session_history] == [2]
    assert state.completed_today == [2]


def test_clear_history_clears_completed_today_with_history() -> None:
    state = OrchestratorState(
        session_history=[_history_entry(1)],
        completed_today=[1, 2],
    )

    result = RetryHistoryState(state).clear_history()

    assert result.cleared_history_entries == 1
    assert result.cleared_completed_today == 2
    assert state.session_history == []
    assert state.completed_today == []


def test_deprioritize_and_prioritize_issue_front_own_priority_queue_mutation() -> None:
    state = OrchestratorState(priority_queue=[1, 2, 3])
    retry_state = RetryHistoryState(state)

    removed = retry_state.deprioritize_issues([2, 4])
    inserted = retry_state.prioritize_issue_front(4)
    duplicate_inserted = retry_state.prioritize_issue_front(4)

    assert removed == [2]
    assert inserted is True
    assert duplicate_inserted is False
    assert state.priority_queue == [4, 1, 3]


def test_clear_scratch_retry_pending_state_removes_issue_and_superseded_prs() -> None:
    state = OrchestratorState(
        pending_reviews=[
            PendingReview(
                issue_key=FakeIssueKey("10"),
                pr_number=100,
                pr_url="url",
                branch_name="branch",
                _issue_number=10,
            ),
            PendingReview(
                issue_key=FakeIssueKey("99"),
                pr_number=376,
                pr_url="url",
                branch_name="branch",
                _issue_number=99,
            ),
            PendingReview(
                issue_key=FakeIssueKey("11"),
                pr_number=101,
                pr_url="url",
                branch_name="branch",
                _issue_number=11,
            ),
        ],
        pending_reworks=[
            PendingRework(
                issue_key=FakeIssueKey("10"),
                agent_type="agent:backend",
                issue_number=10,
                pr_number=100,
            ),
            PendingRework(
                issue_key=FakeIssueKey("99"),
                agent_type="agent:backend",
                issue_number=99,
                pr_number=376,
            ),
            PendingRework(
                issue_key=FakeIssueKey("11"),
                agent_type="agent:backend",
                issue_number=11,
                pr_number=101,
            ),
        ],
        pending_cleanups=[
            PendingCleanup(
                issue=GitHubIssue(number=10, repo="owner/repo", title="Issue 10"),
                pr_number=100,
                pr_url="url",
                branch_name="branch",
                terminal_id="issue-10",
                worktree_path=Path("/tmp/issue-10"),
            ),
            PendingCleanup(
                issue=GitHubIssue(number=99, repo="owner/repo", title="Issue 99"),
                pr_number=376,
                pr_url="url",
                branch_name="branch",
                terminal_id="issue-99",
                worktree_path=Path("/tmp/issue-99"),
            ),
            PendingCleanup(
                issue=GitHubIssue(number=11, repo="owner/repo", title="Issue 11"),
                pr_number=101,
                pr_url="url",
                branch_name="branch",
                terminal_id="issue-11",
                worktree_path=Path("/tmp/issue-11"),
            ),
        ],
    )

    result = RetryHistoryState(state).clear_scratch_retry_pending_state(
        issue_number=10,
        superseded_prs=[376],
    )

    assert result.review_count_before == 3
    assert result.review_count_after == 1
    assert result.rework_count_before == 3
    assert result.rework_count_after == 1
    assert result.cleanup_count_before == 3
    assert result.cleanup_count_after == 1
    assert result.superseded_prs == (376,)
    assert [review.pr_number for review in state.pending_reviews] == [101]
    assert [rework.pr_number for rework in state.pending_reworks] == [101]
    assert [cleanup.pr_number for cleanup in state.pending_cleanups] == [101]
