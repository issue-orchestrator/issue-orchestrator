from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from issue_orchestrator.control.action_applier import ActionApplier
from issue_orchestrator.control.actions import ReconcileHistoryEntryAction
from issue_orchestrator.control.awaiting_merge_reconciler import (
    POST_PUBLISH_VALIDATION_SOURCE,
    AwaitingMergeReconciler,
)
from issue_orchestrator.control.label_manager import LabelManager
from issue_orchestrator.control.session_history import SessionHistoryOwner
from issue_orchestrator.domain.models import (
    Issue,
    OrchestratorState,
    SessionHistoryEntry,
)
from issue_orchestrator.events import EventName
from issue_orchestrator.infra.config import Config
from issue_orchestrator.ports import InMemoryEventSink
from issue_orchestrator.ports.pull_request_tracker import PRInfo
from issue_orchestrator.ports.repository_host import RepositoryHostError


def _history_entry() -> SessionHistoryEntry:
    return SessionHistoryEntry(
        issue_number=228,
        title="Shared cache read misses",
        agent_type="agent:backend",
        status="completed",
        runtime_minutes=0,
        pr_url="https://github.com/owner/repo/pull/318",
        status_reason="Recovered awaiting merge state on startup",
    )


def _label_manager() -> LabelManager:
    return LabelManager(Config())


def _pr(
    state: str,
    *,
    mergeable_state: str | None = None,
    labels: list[str] | None = None,
) -> PRInfo:
    return PRInfo(
        number=318,
        title="Add distributed coalescing for shared-cache read misses",
        url="https://github.com/owner/repo/pull/318",
        branch="228-cache-read-misses",
        body="",
        state=state,
        labels=labels or [],
        mergeable_state=mergeable_state,
    )


def _issue(state: str) -> Issue:
    return Issue(
        number=228,
        title="Shared cache read misses",
        labels=["agent:backend", "pr-pending"],
        state=state,
    )


def test_recovered_awaiting_merge_entry_reconciles_when_pr_is_merged() -> None:
    entry = _history_entry()
    state = OrchestratorState(session_history=[entry])
    repository_host = MagicMock()
    repository_host.get_pr.return_value = _pr("merged")

    result = AwaitingMergeReconciler(
        repository_host,
        clock=lambda: 1234.5,
    ).discover(state)

    assert result.checked == 1
    assert result.discovered == 1
    assert entry.status == "completed"
    assert entry.status_reason == "Recovered awaiting merge state on startup"
    assert result.reconciliations[0].status == "merged"
    assert result.reconciliations[0].status_reason == "PR merged; awaiting merge reconciled"
    assert result.reconciliations[0].source == "pull_request"
    assert entry.pr_url == "https://github.com/owner/repo/pull/318"
    repository_host.get_pr.assert_called_once_with(318)
    repository_host.get_issue.assert_not_called()


def test_recovered_entry_reconciles_when_linked_issue_is_closed() -> None:
    entry = _history_entry()
    state = OrchestratorState(session_history=[entry])
    repository_host = MagicMock()
    repository_host.get_pr.return_value = _pr("open")
    repository_host.get_issue.return_value = _issue("closed")

    result = AwaitingMergeReconciler(
        repository_host,
        clock=lambda: 1234.5,
    ).discover(state)

    assert result.checked == 1
    assert result.discovered == 1
    assert entry.status == "completed"
    assert entry.status_reason == "Recovered awaiting merge state on startup"
    assert result.reconciliations[0].status == "closed"
    assert result.reconciliations[0].status_reason == "Issue closed; awaiting merge reconciled"
    assert result.reconciliations[0].source == "issue"
    assert entry.pr_url == "https://github.com/owner/repo/pull/318"
    assert state.issue_refresh_timestamps[228] == 1234.5
    assert state.issue_last_refreshed_at[228] == 1234.5


def test_recovered_entry_reconciles_when_pr_is_closed() -> None:
    entry = _history_entry()
    state = OrchestratorState(session_history=[entry])
    repository_host = MagicMock()
    repository_host.get_pr.return_value = _pr("closed")

    result = AwaitingMergeReconciler(
        repository_host,
        clock=lambda: 1234.5,
    ).discover(state)

    assert result.checked == 1
    assert result.discovered == 1
    assert entry.status == "completed"
    assert entry.status_reason == "Recovered awaiting merge state on startup"
    assert result.reconciliations[0].status == "closed"
    assert result.reconciliations[0].status_reason == "PR closed; awaiting merge reconciled"
    assert result.reconciliations[0].source == "pull_request"
    assert entry.pr_url == "https://github.com/owner/repo/pull/318"
    repository_host.get_issue.assert_not_called()


def test_open_pr_and_failed_issue_refresh_remain_awaiting_merge_without_freshness() -> None:
    entry = _history_entry()
    state = OrchestratorState(session_history=[entry])
    repository_host = MagicMock()
    repository_host.get_pr.return_value = _pr("open")
    repository_host.get_issue.side_effect = RepositoryHostError("github unavailable")

    result = AwaitingMergeReconciler(
        repository_host,
        clock=lambda: 1234.5,
    ).discover(state)

    assert result.checked == 1
    assert result.still_pending == 1
    assert result.skipped == 0
    assert entry.status == "completed"
    assert state.issue_refresh_timestamps == {}
    assert state.issue_last_refreshed_at == {}


def test_unexpected_issue_refresh_bug_propagates() -> None:
    entry = _history_entry()
    state = OrchestratorState(session_history=[entry])
    repository_host = MagicMock()
    repository_host.get_pr.return_value = _pr("open")
    repository_host.get_issue.side_effect = TypeError("programming bug")

    with pytest.raises(TypeError, match="programming bug"):
        AwaitingMergeReconciler(repository_host).discover(state)


def test_pr_fetch_failure_can_still_reconcile_closed_linked_issue() -> None:
    entry = _history_entry()
    state = OrchestratorState(session_history=[entry])
    repository_host = MagicMock()
    repository_host.get_pr.side_effect = RepositoryHostError("github unavailable")
    repository_host.get_issue.return_value = _issue("closed")

    result = AwaitingMergeReconciler(
        repository_host,
        clock=lambda: 1234.5,
    ).discover(state)

    assert result.checked == 1
    assert result.discovered == 1
    assert result.skipped == 0
    assert entry.status == "completed"
    assert entry.status_reason == "Recovered awaiting merge state on startup"
    assert result.reconciliations[0].status == "closed"
    assert result.reconciliations[0].status_reason == "Issue closed; awaiting merge reconciled"
    assert state.issue_refresh_timestamps[228] == 1234.5
    assert state.issue_last_refreshed_at[228] == 1234.5


def test_invalid_pr_url_is_skipped_without_repository_fetches() -> None:
    entry = _history_entry()
    entry.pr_url = "https://github.com/owner/repo/pull/not-a-number"
    state = OrchestratorState(session_history=[entry])
    repository_host = MagicMock()

    result = AwaitingMergeReconciler(repository_host).discover(state)

    assert result.checked == 1
    assert result.skipped == 1
    assert entry.status == "completed"
    repository_host.get_pr.assert_not_called()
    repository_host.get_issue.assert_not_called()


def test_non_completed_history_entry_is_ignored() -> None:
    entry = _history_entry()
    entry.status = "merged"
    state = OrchestratorState(session_history=[entry])
    repository_host = MagicMock()

    result = AwaitingMergeReconciler(repository_host).discover(state)

    assert result.checked == 0
    assert result.discovered == 0
    repository_host.get_pr.assert_not_called()
    repository_host.get_issue.assert_not_called()


def test_second_reconcile_pass_on_terminal_entry_is_noop() -> None:
    entry = _history_entry()
    state = OrchestratorState(session_history=[entry])
    repository_host = MagicMock()
    repository_host.get_pr.return_value = _pr("merged")
    reconciler = AwaitingMergeReconciler(repository_host)

    first_result = reconciler.discover(state)
    action = ReconcileHistoryEntryAction(
        issue_number=228,
        pr_number=318,
        pr_url=entry.pr_url or "",
        status=first_result.reconciliations[0].status,
        source=first_result.reconciliations[0].source,
        reason=first_result.reconciliations[0].status_reason,
    )
    events = InMemoryEventSink()
    applier = ActionApplier(
        labels=MagicMock(),
        sessions=MagicMock(),
        events=events,
        history_owner=SessionHistoryOwner(state.session_history),
    )
    applier.apply(action)
    second_result = reconciler.discover(state)

    assert first_result.checked == 1
    assert first_result.discovered == 1
    assert second_result.checked == 0
    assert second_result.discovered == 0
    repository_host.get_pr.assert_called_once_with(318)
    assert entry.status == "merged"
    assert events.last_event(EventName.HISTORY_RECONCILED.value) is not None


def test_open_pr_and_open_issue_remain_awaiting_merge_with_freshness_updated() -> None:
    entry = _history_entry()
    state = OrchestratorState(session_history=[entry])
    repository_host = MagicMock()
    repository_host.get_pr.return_value = _pr("open")
    repository_host.get_issue.return_value = _issue("open")

    result = AwaitingMergeReconciler(
        repository_host,
        clock=lambda: 1234.5,
    ).discover(state)

    assert result.checked == 1
    assert result.still_pending == 1
    assert result.discovered == 0
    assert entry.status == "completed"
    assert entry.status_reason == "Recovered awaiting merge state on startup"
    assert entry.pr_url == "https://github.com/owner/repo/pull/318"
    assert state.issue_refresh_timestamps[228] == 1234.5
    assert state.issue_last_refreshed_at[228] == 1234.5


def test_merge_conflict_after_review_discovers_post_publish_validation_rework() -> None:
    entry = _history_entry()
    state = OrchestratorState(session_history=[entry])
    repository_host = MagicMock()
    repository_host.get_pr.return_value = _pr(
        "open",
        mergeable_state="dirty",
        labels=["code-reviewed"],
    )
    repository_host.get_issue.return_value = _issue("open")

    result = AwaitingMergeReconciler(
        repository_host,
        label_manager=_label_manager(),
        clock=lambda: 1234.5,
    ).discover(state)

    assert result.checked == 1
    assert result.discovered == 0
    assert result.rework_discovered == 1
    assert result.still_pending == 1
    rework = result.reworks[0]
    assert rework.issue_number == 228
    assert rework.pr_number == 318
    assert rework.branch_name == "228-cache-read-misses"
    assert rework.agent_type == "agent:backend"
    assert rework.rework_cycle == 1
    assert rework.source == POST_PUBLISH_VALIDATION_SOURCE
    assert "Mergeability: dirty" in (rework.feedback or "")


def test_post_publish_validation_rework_is_suppressed_when_rework_already_pending() -> None:
    entry = _history_entry()
    pending_rework = MagicMock()
    pending_rework.resolve_issue_number.return_value = 228
    state = OrchestratorState(
        session_history=[entry],
        pending_reworks=[pending_rework],
    )
    repository_host = MagicMock()
    repository_host.get_pr.return_value = _pr(
        "open",
        mergeable_state="behind",
        labels=["code-reviewed"],
    )
    repository_host.get_issue.return_value = _issue("open")

    result = AwaitingMergeReconciler(
        repository_host,
        label_manager=_label_manager(),
        clock=lambda: 1234.5,
    ).discover(state)

    assert result.checked == 1
    assert result.rework_discovered == 0
    assert result.still_pending == 1
    assert state.issue_last_refreshed_at[228] == 1234.5
