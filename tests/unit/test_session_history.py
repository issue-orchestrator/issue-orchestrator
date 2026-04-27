"""Tests for session history ownership helpers."""

from datetime import datetime, timezone

from issue_orchestrator.control.session_history import (
    CLOSED_ISSUE_HISTORY_STATUS_REASON,
    ClosedIssueHistoryMutation,
    ClosedIssueHistoryNoop,
    SessionHistoryOwner,
)
from issue_orchestrator.domain.models import SessionHistoryEntry, SessionHistoryStatus


def _history_entry(
    *,
    issue_number: int,
    status: SessionHistoryStatus,
    status_reason: str = "",
) -> SessionHistoryEntry:
    return SessionHistoryEntry(
        issue_number=issue_number,
        title=f"Issue {issue_number}",
        agent_type="agent:web",
        status=status,
        runtime_minutes=1,
        pr_url=None,
        status_reason=status_reason,
        completed_at=datetime.now(timezone.utc),
    )


def test_reconcile_closed_issue_marks_latest_blocking_history_terminal() -> None:
    older = _history_entry(issue_number=270, status="blocked", status_reason="old")
    latest = _history_entry(issue_number=270, status="needs_human", status_reason="needs input")
    owner = SessionHistoryOwner([older, latest])

    result = owner.reconcile_closed_issue(
        issue_number=270,
        status_reason=CLOSED_ISSUE_HISTORY_STATUS_REASON,
    )

    assert isinstance(result, ClosedIssueHistoryMutation)
    assert result.previous_status == "needs_human"
    assert latest.status == "closed"
    assert latest.status_reason == CLOSED_ISSUE_HISTORY_STATUS_REASON
    assert older.status == "blocked"


def test_reconcile_closed_issue_leaves_terminal_history_unchanged() -> None:
    entry = _history_entry(issue_number=270, status="closed", status_reason="already closed")
    owner = SessionHistoryOwner([entry])

    result = owner.reconcile_closed_issue(
        issue_number=270,
        status_reason=CLOSED_ISSUE_HISTORY_STATUS_REASON,
    )

    assert isinstance(result, ClosedIssueHistoryNoop)
    assert result.reason == "already_terminal"
    assert entry.status == "closed"
    assert entry.status_reason == "already closed"


def test_reconcile_closed_issue_reports_missing_when_no_history_entry() -> None:
    owner = SessionHistoryOwner([])

    result = owner.reconcile_closed_issue(
        issue_number=270,
        status_reason=CLOSED_ISSUE_HISTORY_STATUS_REASON,
    )

    assert isinstance(result, ClosedIssueHistoryNoop)
    assert result.reason == "missing"
