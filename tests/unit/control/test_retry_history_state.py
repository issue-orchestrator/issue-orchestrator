"""Tests for retry/history state mutation owner."""

from __future__ import annotations

from pathlib import Path

from issue_orchestrator.adapters.github.github_issue import GitHubIssue
from issue_orchestrator.control.retry_history_state import RetryHistoryState
from issue_orchestrator.domain.issue_key import FakeIssueKey
from issue_orchestrator.domain.models import (
    DependencyProblem,
    DiscoveredAwaitingMergeDrift,
    DiscoveredAwaitingMergeReconciliation,
    DiscoveredEscalation,
    DiscoveredFailure,
    DiscoveredReview,
    DiscoveredRework,
    ImmediateCleanup,
    OrchestratorState,
    PendingCleanup,
    PendingReview,
    PendingRework,
    PendingTriageReview,
    PendingValidationRetry,
    PublishJob,
    PublishJobStatus,
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


def _seeded_state_for_contract(target: int, other: int) -> OrchestratorState:
    """Seed every issue-keyed field on OrchestratorState with both `target` and
    `other` so a clear-scratch operation must remove `target` while leaving
    `other` untouched.

    This fixture is the contract: any field added to OrchestratorState that is
    keyed by issue_number must appear here AND be cleared by
    clear_scratch_retry_pending_state, OR the team must explicitly justify why
    it's exempt by adding a comment in this fixture noting the carve-out.
    """
    return OrchestratorState(
        # session/history gates (cleared by RetryHistoryState.remove_issue_from_history,
        # called from reset_issue's _clear_history_gates — included for end-to-end realism)
        session_history=[
            SessionHistoryEntry(
                issue_number=target,
                title=f"Issue {target}",
                agent_type="agent:backend",
                status="failed",
                runtime_minutes=1,
            ),
            SessionHistoryEntry(
                issue_number=other,
                title=f"Issue {other}",
                agent_type="agent:backend",
                status="failed",
                runtime_minutes=1,
            ),
        ],
        completed_today=[target, other],
        # in-flight publish/review/rework state
        pending_reviews=[
            PendingReview(
                issue_key=FakeIssueKey(str(target)),
                pr_number=100,
                pr_url="url",
                branch_name="branch",
                _issue_number=target,
            ),
            PendingReview(
                issue_key=FakeIssueKey(str(other)),
                pr_number=200,
                pr_url="url",
                branch_name="branch",
                _issue_number=other,
            ),
        ],
        pending_reworks=[
            PendingRework(
                issue_key=FakeIssueKey(str(target)),
                agent_type="agent:backend",
                issue_number=target,
                pr_number=100,
            ),
            PendingRework(
                issue_key=FakeIssueKey(str(other)),
                agent_type="agent:backend",
                issue_number=other,
                pr_number=200,
            ),
        ],
        pending_cleanups=[
            PendingCleanup(
                issue=GitHubIssue(number=target, repo="o/r", title=f"Issue {target}"),
                pr_number=100,
                pr_url="url",
                branch_name="branch",
                terminal_id=f"issue-{target}",
                worktree_path=Path(f"/tmp/issue-{target}"),
            ),
            PendingCleanup(
                issue=GitHubIssue(number=other, repo="o/r", title=f"Issue {other}"),
                pr_number=200,
                pr_url="url",
                branch_name="branch",
                terminal_id=f"issue-{other}",
                worktree_path=Path(f"/tmp/issue-{other}"),
            ),
        ],
        pending_triage_reviews=[
            PendingTriageReview(issue_number=target, title=f"Triage {target}"),
            PendingTriageReview(issue_number=other, title=f"Triage {other}"),
        ],
        pending_validation_retries=[
            PendingValidationRetry(
                issue_number=target,
                issue_title=f"Issue {target}",
                agent_label="agent:backend",
                worktree_path=f"/tmp/issue-{target}",
                branch_name="branch",
                original_prompt=None,
                validation_error="dirty tree",
                validation_error_file=None,
                retry_count=1,
            ),
            PendingValidationRetry(
                issue_number=other,
                issue_title=f"Issue {other}",
                agent_label="agent:backend",
                worktree_path=f"/tmp/issue-{other}",
                branch_name="branch",
                original_prompt=None,
                validation_error="dirty tree",
                validation_error_file=None,
                retry_count=1,
            ),
        ],
        pending_publish_jobs={
            f"job-{target}": PublishJob(
                job_id=f"job-{target}",
                issue_number=target,
                session_key="session-1",
                status=PublishJobStatus.QUEUED,
            ),
            f"job-{other}": PublishJob(
                job_id=f"job-{other}",
                issue_number=other,
                session_key="session-2",
                status=PublishJobStatus.QUEUED,
            ),
        },
        running_publish_jobs={
            f"running-{target}": PublishJob(
                job_id=f"running-{target}",
                issue_number=target,
                session_key="session-1",
                status=PublishJobStatus.RUNNING,
            ),
            f"running-{other}": PublishJob(
                job_id=f"running-{other}",
                issue_number=other,
                session_key="session-2",
                status=PublishJobStatus.RUNNING,
            ),
        },
        # discovered facts — Planner inputs that should not survive a scratch reset
        discovered_reviews=[
            DiscoveredReview(
                issue_number=target,
                pr_number=100,
                pr_url="url",
                branch_name="branch",
            ),
            DiscoveredReview(
                issue_number=other,
                pr_number=200,
                pr_url="url",
                branch_name="branch",
            ),
        ],
        discovered_reworks=[
            DiscoveredRework(
                issue_number=target,
                pr_number=100,
                branch_name="branch",
                agent_type="agent:backend",
            ),
            DiscoveredRework(
                issue_number=other,
                pr_number=200,
                branch_name="branch",
                agent_type="agent:backend",
            ),
        ],
        discovered_escalations=[
            DiscoveredEscalation(issue_number=target, pr_number=100, rework_cycle=1),
            DiscoveredEscalation(issue_number=other, pr_number=200, rework_cycle=1),
        ],
        discovered_failures=[
            DiscoveredFailure(
                issue_number=target,
                issue_title=f"Issue {target}",
                failure_reason="failed",
            ),
            DiscoveredFailure(
                issue_number=other,
                issue_title=f"Issue {other}",
                failure_reason="failed",
            ),
        ],
        discovered_awaiting_merge_reconciliations=[
            DiscoveredAwaitingMergeReconciliation(
                issue_number=target,
                pr_number=100,
                pr_url="url",
                status="merged",
                status_reason="merged",
                source="pull_request",
            ),
            DiscoveredAwaitingMergeReconciliation(
                issue_number=other,
                pr_number=200,
                pr_url="url",
                status="merged",
                status_reason="merged",
                source="pull_request",
            ),
        ],
        discovered_awaiting_merge_drifts=[
            DiscoveredAwaitingMergeDrift(
                issue_number=target,
                pr_number=100,
                pr_url="url",
                status_reason="closed",
            ),
            DiscoveredAwaitingMergeDrift(
                issue_number=other,
                pr_number=200,
                pr_url="url",
                status_reason="closed",
            ),
        ],
        immediate_cleanups=[
            ImmediateCleanup(
                issue_number=target,
                terminal_id=f"issue-{target}",
                worktree_path=f"/tmp/issue-{target}",
                reason="completed",
            ),
            ImmediateCleanup(
                issue_number=other,
                terminal_id=f"issue-{other}",
                worktree_path=f"/tmp/issue-{other}",
                reason="completed",
            ),
        ],
        # progress-blocking flags
        failed_this_cycle={target, other},
        stale_issue_ticks={target: 5, other: 3},
        dependency_problems={
            target: DependencyProblem(
                issue_number=target,
                issue_title=f"Issue {target}",
                blocked_by=[(99, "Dep", "open")],
                summary="blocked",
            ),
            other: DependencyProblem(
                issue_number=other,
                issue_title=f"Issue {other}",
                blocked_by=[(99, "Dep", "open")],
                summary="blocked",
            ),
        },
        # UI/refresh hints
        ui_visible_issue_numbers=[target, other],
        issue_refresh_timestamps={target: 1.0, other: 2.0},
        issue_last_refreshed_at={target: 1.0, other: 2.0},
        awaiting_merge_drift_scan_timestamps={target: 1.0, other: 2.0},
        # priority queue — manual override that should be re-seeded post-reset by
        # _enqueue_reset_retry_issue's prioritize_issue_front; clearing first ensures
        # idempotent re-add and prevents stale duplicate entries.
        priority_queue=[target, other],
        # candidate queue removals — should not pin a target across a reset
        queue_pending_shrink_missing_issue_numbers=[target, other],
    )


def test_clear_scratch_retry_state_contract_no_leaks_for_target() -> None:
    """Contract: from-scratch reset must remove `target` from every issue-keyed
    collection on OrchestratorState while leaving `other` untouched.

    This test is the regression contract for issue-359/360-style failures where
    "reset and retry from scratch" left stale state behind, causing the next
    attempt to inherit pending_*, stale_ticks, or discovered_* records from the
    abandoned attempt. Multiple PRs have tried to fix this; without a contract
    test the same leaks keep coming back.

    If this test fails: either (a) you added an issue-keyed field to
    OrchestratorState and forgot to clear it on scratch reset, or (b) you
    intentionally exempted a field — in which case add a justification comment
    in `_seeded_state_for_contract` and amend the assertion list below.
    """
    target = 10
    other = 11
    state = _seeded_state_for_contract(target, other)

    # Mirror the reset path: history gates clear first (done by reset_issue's
    # _clear_history_gates), then scratch-pending state (done by RetryHistoryState).
    RetryHistoryState(state).remove_issue_from_history(target)
    RetryHistoryState(state).clear_scratch_retry_pending_state(
        issue_number=target,
        superseded_prs=[100],
    )

    # `target` removed from every collection
    assert all(e.issue_number != target for e in state.session_history)
    assert target not in state.completed_today
    assert all(r.issue_number != target for r in state.pending_reviews)
    assert all(r.issue_number != target for r in state.pending_reworks)
    assert all(c.issue.number != target for c in state.pending_cleanups)
    assert all(t.issue_number != target for t in state.pending_triage_reviews)
    assert all(v.issue_number != target for v in state.pending_validation_retries)
    assert all(j.issue_number != target for j in state.pending_publish_jobs.values())
    assert all(j.issue_number != target for j in state.running_publish_jobs.values())
    assert all(d.issue_number != target for d in state.discovered_reviews)
    assert all(d.issue_number != target for d in state.discovered_reworks)
    assert all(d.issue_number != target for d in state.discovered_escalations)
    assert all(d.issue_number != target for d in state.discovered_failures)
    assert all(d.issue_number != target for d in state.discovered_awaiting_merge_reconciliations)
    assert all(d.issue_number != target for d in state.discovered_awaiting_merge_drifts)
    assert all(c.issue_number != target for c in state.immediate_cleanups)
    assert target not in state.failed_this_cycle
    assert target not in state.stale_issue_ticks
    assert target not in state.dependency_problems
    assert target not in state.ui_visible_issue_numbers
    assert target not in state.issue_refresh_timestamps
    assert target not in state.issue_last_refreshed_at
    assert target not in state.awaiting_merge_drift_scan_timestamps
    assert target not in state.priority_queue
    assert target not in state.queue_pending_shrink_missing_issue_numbers

    # `other` survives every collection
    assert any(e.issue_number == other for e in state.session_history)
    assert other in state.completed_today
    assert any(r.issue_number == other for r in state.pending_reviews)
    assert any(r.issue_number == other for r in state.pending_reworks)
    assert any(c.issue.number == other for c in state.pending_cleanups)
    assert any(t.issue_number == other for t in state.pending_triage_reviews)
    assert any(v.issue_number == other for v in state.pending_validation_retries)
    assert any(j.issue_number == other for j in state.pending_publish_jobs.values())
    assert any(j.issue_number == other for j in state.running_publish_jobs.values())
    assert any(d.issue_number == other for d in state.discovered_reviews)
    assert any(d.issue_number == other for d in state.discovered_reworks)
    assert any(d.issue_number == other for d in state.discovered_escalations)
    assert any(d.issue_number == other for d in state.discovered_failures)
    assert any(d.issue_number == other for d in state.discovered_awaiting_merge_reconciliations)
    assert any(d.issue_number == other for d in state.discovered_awaiting_merge_drifts)
    assert any(c.issue_number == other for c in state.immediate_cleanups)
    assert other in state.failed_this_cycle
    assert other in state.stale_issue_ticks
    assert other in state.dependency_problems
    assert other in state.ui_visible_issue_numbers
    assert other in state.issue_refresh_timestamps
    assert other in state.issue_last_refreshed_at
    assert other in state.awaiting_merge_drift_scan_timestamps
    assert other in state.priority_queue
    assert other in state.queue_pending_shrink_missing_issue_numbers


def test_clear_scratch_retry_records_tombstones_for_active_publish_jobs() -> None:
    """Active publish jobs at reset time must be tombstoned, not just removed
    from the dict. The PublishJobExecutor worker keeps running after the dict
    entry is dropped — its late result would otherwise re-populate
    discovered_reviews/completed_today for the freshly-reset issue. Bug
    flagged in PR #6131 review.
    """
    target = 10
    other = 11
    state = OrchestratorState(
        pending_publish_jobs={
            "job-target-pending": PublishJob(
                job_id="job-target-pending",
                issue_number=target,
                session_key="session-1",
                status=PublishJobStatus.QUEUED,
            ),
            "job-other-pending": PublishJob(
                job_id="job-other-pending",
                issue_number=other,
                session_key="session-2",
                status=PublishJobStatus.QUEUED,
            ),
        },
        running_publish_jobs={
            "job-target-running": PublishJob(
                job_id="job-target-running",
                issue_number=target,
                session_key="session-1",
                status=PublishJobStatus.RUNNING,
            ),
            "job-other-running": PublishJob(
                job_id="job-other-running",
                issue_number=other,
                session_key="session-2",
                status=PublishJobStatus.RUNNING,
            ),
        },
    )

    RetryHistoryState(state).clear_scratch_retry_pending_state(
        issue_number=target,
        superseded_prs=[],
    )

    # Both pending and running job IDs for `target` are tombstoned.
    assert "job-target-pending" in state.superseded_job_ids
    assert "job-target-running" in state.superseded_job_ids
    # Other issue's jobs are NOT tombstoned — its workers should still
    # report their results normally.
    assert "job-other-pending" not in state.superseded_job_ids
    assert "job-other-running" not in state.superseded_job_ids
