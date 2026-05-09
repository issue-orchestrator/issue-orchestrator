"""Unit tests for queue cache abstraction and invariant boundaries."""

from pathlib import Path
from unittest.mock import Mock

import pytest

from issue_orchestrator.control.queue_cache import (
    QUEUE_SHRINK_CONFIRM_DELAY_SECONDS,
    QueueCache,
    QueueMutationStatus,
    clear_issue_refresh,
    queue_shrink_confirmation_due,
    queue_shrink_confirmation_pending,
    record_issue_refreshes,
)
from issue_orchestrator.domain.models import AgentConfig, Issue, OrchestratorState, SessionHistoryEntry
from issue_orchestrator.infra.config import Config


def _make_config() -> Config:
    return Config(
        repo="owner/repo",
        repo_root=Path("/tmp/repo"),
        worktree_base=Path("/tmp/worktrees"),
        agents={"agent:web": AgentConfig(prompt_path=Path("/tmp/prompt.txt"))},
    )


def _make_issues(numbers: range | list[int]) -> list[Issue]:
    return [
        Issue(number=number, title=f"Issue {number}", labels=["agent:web"])
        for number in numbers
    ]


def test_upsert_accepts_in_scope_issue():
    config = _make_config()
    config.filtering.label = "agent:web"
    state = OrchestratorState()
    cache = QueueCache(config, state)

    outcome = cache.upsert_refreshed_issue(Issue(number=1, title="A", labels=["agent:web"]))

    assert outcome.status == QueueMutationStatus.ACCEPTED
    assert outcome.in_queue is True
    assert [issue.number for issue in state.cached_scope_issues] == [1]
    assert [issue.number for issue in state.cached_queue_issues] == [1]


def test_upsert_rejects_out_of_scope_issue():
    config = _make_config()
    config.filtering.label = "agent:web"
    state = OrchestratorState(cached_queue_issues=[Issue(number=1, title="A", labels=["agent:web"])])
    cache = QueueCache(config, state)

    outcome = cache.upsert_refreshed_issue(Issue(number=1, title="A2", labels=["agent:other"]))

    assert outcome.status == QueueMutationStatus.REJECTED_OUT_OF_SCOPE
    assert outcome.in_queue is False
    assert state.cached_scope_issues == []
    assert state.cached_queue_issues == []


def test_upsert_rejects_closed_issue_even_when_filters_match():
    config = _make_config()
    config.filtering.label = "agent:web"
    state = OrchestratorState(cached_queue_issues=[Issue(number=1, title="A", labels=["agent:web"])])
    cache = QueueCache(config, state)

    outcome = cache.upsert_refreshed_issue(
        Issue(number=1, title="A closed", labels=["agent:web"], state="closed")
    )

    assert outcome.status == QueueMutationStatus.REJECTED_OUT_OF_SCOPE
    assert outcome.in_queue is False
    assert state.cached_scope_issues == []
    assert state.cached_queue_issues == []


def test_save_snapshot_persists_scope_watermark_and_repo():
    config = _make_config()
    state = OrchestratorState(
        cached_scope_issues=[Issue(number=1, title="A", labels=["agent:web"])],
        queue_delta_watermark="2026-05-09T14:00:00Z",
    )
    store = Mock()

    QueueCache(config, state, store).save_snapshot()

    store.save_snapshot.assert_called_once_with(
        state.cached_scope_issues,
        "2026-05-09T14:00:00Z",
        repo="owner/repo",
    )


def test_save_snapshot_fails_fast_without_store():
    config = _make_config()
    state = OrchestratorState()

    with pytest.raises(RuntimeError, match="QueueCacheStore is required"):
        QueueCache(config, state).save_snapshot()


def test_remove_issue_and_save_persists_removed_snapshot():
    config = _make_config()
    issue = Issue(number=1, title="A", labels=["agent:web"])
    state = OrchestratorState(
        cached_scope_issues=[issue],
        cached_queue_issues=[issue],
        queue_delta_watermark="2026-05-09T14:00:00Z",
    )
    store = Mock()

    QueueCache(config, state, store).remove_issue_and_save(1)

    assert state.cached_scope_issues == []
    assert state.cached_queue_issues == []
    store.save_snapshot.assert_called_once_with(
        [],
        "2026-05-09T14:00:00Z",
        repo="owner/repo",
    )


def test_replace_from_refresh_warns_on_non_empty_to_empty_drop(caplog):
    config = _make_config()
    config.filtering.label = "agent:web"
    state = OrchestratorState(
        cached_queue_issues=[Issue(number=1, title="A", labels=["agent:web"])]
    )
    cache = QueueCache(config, state)

    caplog.clear()
    with caplog.at_level("WARNING", logger="issue_orchestrator.control.queue_cache"):
        cache.replace_from_refresh([])

    assert state.cached_queue_issues == []
    assert any(
        "dropping in-memory queue from 1 to 0" in r.message for r in caplog.records
    ), caplog.text


def test_replace_from_refresh_silent_on_cold_start():
    config = _make_config()
    config.filtering.label = "agent:web"
    state = OrchestratorState()
    cache = QueueCache(config, state)

    import logging
    logger = logging.getLogger("issue_orchestrator.control.queue_cache")
    captured: list[str] = []
    handler = logging.Handler()
    handler.emit = lambda record: captured.append(record.getMessage())
    handler.setLevel(logging.WARNING)
    logger.addHandler(handler)
    try:
        cache.replace_from_refresh([])
    finally:
        logger.removeHandler(handler)

    assert captured == []


def test_replace_from_refresh_silent_when_populated():
    config = _make_config()
    config.filtering.label = "agent:web"
    state = OrchestratorState(
        cached_queue_issues=[Issue(number=1, title="A", labels=["agent:web"])]
    )
    cache = QueueCache(config, state)

    import logging
    logger = logging.getLogger("issue_orchestrator.control.queue_cache")
    captured: list[str] = []
    handler = logging.Handler()
    handler.emit = lambda record: captured.append(record.getMessage())
    handler.setLevel(logging.WARNING)
    logger.addHandler(handler)
    try:
        cache.replace_from_refresh([Issue(number=2, title="B", labels=["agent:web"])])
    finally:
        logger.removeHandler(handler)

    assert captured == []
    assert [i.number for i in state.cached_queue_issues] == [2]


def test_replace_from_refresh_retains_suspicious_large_shrink_until_confirmation(
    monkeypatch, caplog
):
    from issue_orchestrator.control import queue_cache as queue_cache_module

    config = _make_config()
    config.filtering.label = "agent:web"
    prior = _make_issues(list(range(1, 21)))
    state = OrchestratorState(
        cached_scope_issues=list(prior),
        cached_queue_issues=list(prior),
    )
    cache = QueueCache(config, state)
    monkeypatch.setattr(queue_cache_module.time, "time", lambda: 1000.0)

    with caplog.at_level("WARNING", logger="issue_orchestrator.control.queue_cache"):
        queue = cache.replace_from_refresh(
            [Issue(number=1, title="Issue 1 updated", labels=["agent:web"])]
        )

    assert [issue.number for issue in queue] == list(range(1, 21))
    assert [issue.number for issue in state.cached_scope_issues] == list(range(1, 21))
    assert queue_shrink_confirmation_pending(state) is True
    assert state.queue_pending_shrink_missing_issue_numbers == list(range(2, 21))
    assert (
        state.queue_pending_shrink_confirm_at
        == 1000.0 + QUEUE_SHRINK_CONFIRM_DELAY_SECONDS
    )
    assert not queue_shrink_confirmation_due(state, 1059.0)
    assert queue_shrink_confirmation_due(state, 1060.0)
    assert "suspicious queue shrink retained pending confirmation" in caplog.text


def test_replace_from_refresh_confirms_repeated_large_shrink(monkeypatch):
    from issue_orchestrator.control import queue_cache as queue_cache_module

    config = _make_config()
    config.filtering.label = "agent:web"
    prior = _make_issues(list(range(1, 21)))
    state = OrchestratorState(
        cached_scope_issues=list(prior),
        cached_queue_issues=list(prior),
    )
    cache = QueueCache(config, state)
    monkeypatch.setattr(queue_cache_module.time, "time", lambda: 1000.0)
    first_issue = Issue(number=1, title="Issue 1 updated", labels=["agent:web"])
    cache.replace_from_refresh([first_issue])

    monkeypatch.setattr(queue_cache_module.time, "time", lambda: 1060.0)
    queue = cache.replace_from_refresh([first_issue])

    assert [issue.number for issue in queue] == [1]
    assert [issue.number for issue in state.cached_scope_issues] == [1]
    assert queue_shrink_confirmation_pending(state) is False
    assert state.queue_pending_shrink_confirm_at == 0.0


def test_replace_from_refresh_preserves_deadline_for_changing_missing_sets(
    monkeypatch,
):
    from issue_orchestrator.control import queue_cache as queue_cache_module

    config = _make_config()
    config.filtering.label = "agent:web"
    prior = _make_issues(list(range(1, 21)))
    state = OrchestratorState(
        cached_scope_issues=list(prior),
        cached_queue_issues=list(prior),
    )
    cache = QueueCache(config, state)
    monkeypatch.setattr(queue_cache_module.time, "time", lambda: 1000.0)
    cache.replace_from_refresh([Issue(number=1, title="Issue 1", labels=["agent:web"])])

    original_confirm_at = state.queue_pending_shrink_confirm_at
    monkeypatch.setattr(queue_cache_module.time, "time", lambda: 1010.0)
    queue = cache.replace_from_refresh([Issue(number=2, title="Issue 2", labels=["agent:web"])])

    assert [issue.number for issue in queue] == [2, 1, *range(3, 21)]
    assert state.queue_pending_shrink_missing_issue_numbers == [1, *range(3, 21)]
    assert state.queue_pending_shrink_confirm_at == original_confirm_at


def test_replace_from_refresh_clears_pending_large_shrink_when_refresh_recovers(
    monkeypatch,
):
    from issue_orchestrator.control import queue_cache as queue_cache_module

    config = _make_config()
    config.filtering.label = "agent:web"
    prior = _make_issues(list(range(1, 21)))
    state = OrchestratorState(
        cached_scope_issues=list(prior),
        cached_queue_issues=list(prior),
    )
    cache = QueueCache(config, state)
    monkeypatch.setattr(queue_cache_module.time, "time", lambda: 1000.0)
    cache.replace_from_refresh(
        [Issue(number=1, title="Issue 1 updated", labels=["agent:web"])]
    )

    recovered = _make_issues(list(range(1, 21)))
    recovered[0] = Issue(number=1, title="Issue 1 recovered", labels=["agent:web"])
    queue = cache.replace_from_refresh(recovered)

    assert [issue.number for issue in queue] == list(range(1, 21))
    assert state.cached_queue_issues[0].title == "Issue 1 recovered"
    assert queue_shrink_confirmation_pending(state) is False


def test_replace_from_refresh_applies_small_shrink_without_confirmation():
    config = _make_config()
    config.filtering.label = "agent:web"
    prior = _make_issues(list(range(1, 21)))
    state = OrchestratorState(
        cached_scope_issues=list(prior),
        cached_queue_issues=list(prior),
    )
    cache = QueueCache(config, state)

    queue = cache.replace_from_refresh(_make_issues(list(range(1, 13))))

    assert [issue.number for issue in queue] == list(range(1, 13))
    assert queue_shrink_confirmation_pending(state) is False


def test_replace_from_refresh_filters_excluded_history_issue():
    config = _make_config()
    config.filtering.label = "agent:web"
    state = OrchestratorState(
        session_history=[
            SessionHistoryEntry(
                issue_number=2,
                title="Old",
                agent_type="agent:web",
                status="completed",
                runtime_minutes=3,
            )
        ]
    )
    cache = QueueCache(config, state)

    queue = cache.replace_from_refresh(
        [
            Issue(number=1, title="Keep", labels=["agent:web"]),
            Issue(number=2, title="History", labels=["agent:web"]),
            Issue(number=3, title="Other", labels=["agent:other"]),
        ]
    )

    assert [issue.number for issue in queue] == [1]
    assert [issue.number for issue in state.cached_scope_issues] == [1, 2]
    assert [issue.number for issue in state.cached_queue_issues] == [1]


def test_prune_refresh_timestamps_keeps_only_tracked_issue_numbers():
    config = _make_config()
    state = OrchestratorState(
        cached_scope_issues=[Issue(number=1, title="Keep", labels=["agent:web"])],
        cached_queue_issues=[Issue(number=1, title="Keep", labels=["agent:web"])],
        issue_refresh_timestamps={1: 100.0, 2: 200.0},
        issue_last_refreshed_at={1: 100.0, 2: 200.0},
        awaiting_merge_drift_scan_timestamps={1: 300.0, 2: 400.0},
    )
    cache = QueueCache(config, state)

    cache.prune_refresh_timestamps()

    assert state.issue_refresh_timestamps == {1: 100.0}
    assert state.issue_last_refreshed_at == {1: 100.0}
    assert state.awaiting_merge_drift_scan_timestamps == {1: 300.0}


def test_replace_from_refresh_tracks_blocked_scope_issues():
    config = _make_config()
    config.filtering.label = "agent:web"
    state = OrchestratorState()
    cache = QueueCache(config, state)

    queue = cache.replace_from_refresh(
        [
            Issue(number=1, title="Runnable", labels=["agent:web"]),
            Issue(number=2, title="Publish failed", labels=["agent:web", "publish-failed"]),
        ]
    )

    assert [issue.number for issue in state.cached_scope_issues] == [1, 2]
    assert [issue.number for issue in queue] == [1, 2]
    assert [issue.number for issue in state.cached_queue_issues] == [1, 2]


def test_record_issue_refreshes_updates_both_freshness_maps():
    state = OrchestratorState()

    record_issue_refreshes(state, {4057, 4058}, 1234.5)

    assert state.issue_refresh_timestamps == {4057: 1234.5, 4058: 1234.5}
    assert state.issue_last_refreshed_at == {4057: 1234.5, 4058: 1234.5}


def test_clear_issue_refresh_removes_both_freshness_maps():
    state = OrchestratorState(
        issue_refresh_timestamps={4057: 1234.5},
        issue_last_refreshed_at={4057: 1234.5},
        awaiting_merge_drift_scan_timestamps={4057: 1234.5},
    )

    clear_issue_refresh(state, 4057)

    assert state.issue_refresh_timestamps == {}
    assert state.issue_last_refreshed_at == {}
    assert state.awaiting_merge_drift_scan_timestamps == {}


def test_prune_refresh_timestamps_keeps_recently_visible_issue_numbers(monkeypatch):
    config = _make_config()
    state = OrchestratorState(
        issue_refresh_timestamps={1: 100.0, 2: 200.0},
        issue_last_refreshed_at={1: 100.0, 2: 200.0},
        awaiting_merge_drift_scan_timestamps={1: 300.0, 2: 400.0},
        ui_visible_issue_numbers=[2],
        ui_visible_updated_at=50_000.0,
    )
    cache = QueueCache(config, state)

    from issue_orchestrator.control import queue_cache as queue_cache_module

    monkeypatch.setattr(queue_cache_module.time, "time", lambda: 50_060.0)

    cache.prune_refresh_timestamps()

    assert state.issue_refresh_timestamps == {2: 200.0}
    assert state.issue_last_refreshed_at == {2: 200.0}
    assert state.awaiting_merge_drift_scan_timestamps == {2: 400.0}


def test_prune_refresh_timestamps_discards_stale_visible_issue_numbers(monkeypatch):
    config = _make_config()
    state = OrchestratorState(
        issue_refresh_timestamps={1: 100.0, 2: 200.0},
        issue_last_refreshed_at={1: 100.0, 2: 200.0},
        awaiting_merge_drift_scan_timestamps={1: 300.0, 2: 400.0},
        ui_visible_issue_numbers=[2],
        ui_visible_updated_at=50_000.0,
    )
    cache = QueueCache(config, state)

    from issue_orchestrator.control import queue_cache as queue_cache_module

    monkeypatch.setattr(queue_cache_module.time, "time", lambda: 50_121.0)

    cache.prune_refresh_timestamps()

    assert state.issue_refresh_timestamps == {}
    assert state.issue_last_refreshed_at == {}
    assert state.awaiting_merge_drift_scan_timestamps == {}
