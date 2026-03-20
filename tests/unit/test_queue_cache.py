"""Unit tests for queue cache abstraction and invariant boundaries."""

from pathlib import Path

from issue_orchestrator.control.queue_cache import QueueCache, QueueMutationStatus
from issue_orchestrator.domain.models import AgentConfig, Issue, OrchestratorState, SessionHistoryEntry
from issue_orchestrator.infra.config import Config


def _make_config() -> Config:
    return Config(
        repo="owner/repo",
        repo_root=Path("/tmp/repo"),
        worktree_base=Path("/tmp/worktrees"),
        agents={"agent:web": AgentConfig(prompt_path=Path("/tmp/prompt.txt"))},
    )


def test_upsert_accepts_in_scope_issue():
    config = _make_config()
    config.filtering.label = "agent:web"
    state = OrchestratorState()
    cache = QueueCache(config, state)

    outcome = cache.upsert_refreshed_issue(Issue(number=1, title="A", labels=["agent:web"]))

    assert outcome.status == QueueMutationStatus.ACCEPTED
    assert outcome.in_queue is True
    assert [issue.number for issue in state.cached_queue_issues] == [1]


def test_upsert_rejects_out_of_scope_issue():
    config = _make_config()
    config.filtering.label = "agent:web"
    state = OrchestratorState(cached_queue_issues=[Issue(number=1, title="A", labels=["agent:web"])])
    cache = QueueCache(config, state)

    outcome = cache.upsert_refreshed_issue(Issue(number=1, title="A2", labels=["agent:other"]))

    assert outcome.status == QueueMutationStatus.REJECTED_OUT_OF_SCOPE
    assert outcome.in_queue is False
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
    assert state.cached_queue_issues == []


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
    assert [issue.number for issue in state.cached_queue_issues] == [1]


def test_prune_refresh_timestamps_keeps_only_tracked_issue_numbers():
    config = _make_config()
    state = OrchestratorState(
        cached_queue_issues=[Issue(number=1, title="Keep", labels=["agent:web"])],
        issue_refresh_timestamps={1: 100.0, 2: 200.0},
        issue_last_refreshed_at={1: 100.0, 2: 200.0},
    )
    cache = QueueCache(config, state)

    cache.prune_refresh_timestamps()

    assert state.issue_refresh_timestamps == {1: 100.0}
    assert state.issue_last_refreshed_at == {1: 100.0}


def test_prune_refresh_timestamps_keeps_recently_visible_issue_numbers(monkeypatch):
    config = _make_config()
    state = OrchestratorState(
        issue_refresh_timestamps={1: 100.0, 2: 200.0},
        issue_last_refreshed_at={1: 100.0, 2: 200.0},
        ui_visible_issue_numbers=[2],
        ui_visible_updated_at=50_000.0,
    )
    cache = QueueCache(config, state)

    from issue_orchestrator.control import queue_cache as queue_cache_module

    monkeypatch.setattr(queue_cache_module.time, "time", lambda: 50_060.0)

    cache.prune_refresh_timestamps()

    assert state.issue_refresh_timestamps == {2: 200.0}
    assert state.issue_last_refreshed_at == {2: 200.0}


def test_prune_refresh_timestamps_discards_stale_visible_issue_numbers(monkeypatch):
    config = _make_config()
    state = OrchestratorState(
        issue_refresh_timestamps={1: 100.0, 2: 200.0},
        issue_last_refreshed_at={1: 100.0, 2: 200.0},
        ui_visible_issue_numbers=[2],
        ui_visible_updated_at=50_000.0,
    )
    cache = QueueCache(config, state)

    from issue_orchestrator.control import queue_cache as queue_cache_module

    monkeypatch.setattr(queue_cache_module.time, "time", lambda: 50_121.0)

    cache.prune_refresh_timestamps()

    assert state.issue_refresh_timestamps == {}
    assert state.issue_last_refreshed_at == {}
