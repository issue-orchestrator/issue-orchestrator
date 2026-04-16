"""Tests for retry/history state mutation owner."""

from __future__ import annotations

from issue_orchestrator.control.retry_history_state import RetryHistoryState
from issue_orchestrator.domain.models import OrchestratorState, SessionHistoryEntry


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
