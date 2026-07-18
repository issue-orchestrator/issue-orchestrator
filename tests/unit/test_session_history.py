"""Tests for session history ownership helpers."""

from datetime import datetime, timezone

from issue_orchestrator.control.session_history import (
    CLOSED_ISSUE_HISTORY_STATUS_REASON,
    ClosedIssueHistoryMutation,
    ClosedIssueHistoryNoop,
    HistoryReconciliationMutation,
    SessionHistoryOwner,
)
from issue_orchestrator.domain.models import SessionHistoryEntry, SessionHistoryStatus


def _history_entry(
    *,
    issue_number: int,
    status: SessionHistoryStatus,
    status_reason: str = "",
    pr_url: str | None = None,
) -> SessionHistoryEntry:
    return SessionHistoryEntry(
        issue_number=issue_number,
        title=f"Issue {issue_number}",
        agent_type="agent:web",
        status=status,
        runtime_minutes=1,
        pr_url=pr_url,
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


# --- #6692 regression: owner must resolve session_history at mutation time ---


class _FakeState:
    """Minimal stand-in for orchestrator state whose ``session_history`` list
    is replaced wholesale by recovery/retry paths."""

    def __init__(self, session_history: list[SessionHistoryEntry]) -> None:
        self.session_history = session_history


def test_reconcile_awaiting_merge_follows_session_history_replacement() -> None:
    """Regression for #6692.

    An owner bound to mutable state (via a provider, as the production wiring
    does) must reconcile against whatever list ``state.session_history``
    currently points at -- even after recovery/retry finalization replaces it
    with a brand-new list object. The pre-fix owner captured the list handed to
    it at construction, so the replacement was invisible and every awaiting
    -merge reconciliation reported a spurious ``missing`` no-op, stranding
    closed/merged PR-backed entries in the Awaiting Merge lane.
    """
    pr_url = "https://github.com/BruceBGordon/issue-orchestrator/pull/6687"
    # Owner is constructed against the initial list (mirrors startup wiring
    # binding to state.session_history before any replacement).
    state = _FakeState([_history_entry(issue_number=6686, status="completed")])
    owner = SessionHistoryOwner(lambda: state.session_history)

    # A publish-retry finalize / recovery path replaces the whole list with a
    # new one carrying the reconcilable awaiting-merge entry.
    reconcilable = _history_entry(
        issue_number=6686, status="completed", pr_url=pr_url
    )
    state.session_history = [reconcilable]

    result = owner.reconcile_awaiting_merge(
        issue_number=6686,
        pr_url=pr_url,
        status="merged",
        status_reason="PR merged",
    )

    assert isinstance(result, HistoryReconciliationMutation)
    assert result.previous_status == "completed"
    assert reconcilable.status == "merged"
    assert reconcilable.status_reason == "PR merged"


def test_session_history_owner_provider_resolves_current_list_each_access() -> None:
    """The ``session_history`` view always reflects the live provider result."""
    first = [_history_entry(issue_number=1, status="completed")]
    second = [_history_entry(issue_number=2, status="completed")]
    box = {"history": first}
    owner = SessionHistoryOwner(lambda: box["history"])

    assert list(owner.session_history) == first
    box["history"] = second
    assert list(owner.session_history) == second


def test_reconcile_awaiting_merge_concrete_list_still_supported() -> None:
    """Backward-compat: constructing with a concrete list keeps mutating that
    same list. Short-lived per-operation owners and existing call sites rely on
    this."""
    pr_url = "https://github.com/BruceBGordon/issue-orchestrator/pull/999"
    entry = _history_entry(issue_number=42, status="completed", pr_url=pr_url)
    owner = SessionHistoryOwner([entry])

    result = owner.reconcile_awaiting_merge(
        issue_number=42,
        pr_url=pr_url,
        status="closed",
        status_reason="PR closed",
    )

    assert isinstance(result, HistoryReconciliationMutation)
    assert entry.status == "closed"
    assert entry.status_reason == "PR closed"
