"""Tests for dashboard view model generation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from issue_orchestrator.domain.issue_key import FakeIssueKey
from issue_orchestrator.domain.models import (
    AgentConfig,
    DependencyProblem,
    Issue,
    OrchestratorState,
    PendingReview,
    Session,
    SessionHistoryEntry,
)
from issue_orchestrator.domain.session_key import SessionKey, TaskKind
from issue_orchestrator.infra.config import Config
from issue_orchestrator.view_models.dashboard import (
    _exclude_flow_overlaps,
    _normalize_status_reason,
    build_dashboard_view_model,
)
from issue_orchestrator.contracts.public import DashboardViewModelContract


@dataclass
class _OrchestratorStub:
    state: OrchestratorState
    config: Config
    shutdown_requested: bool = False


def _make_config() -> Config:
    config = Config()
    config.repo = "test/repo"
    config.repo_root = Path("/tmp/repo")
    config.queue_refresh_seconds = 600
    config.terminal_adapter = "subprocess"
    config.e2e.enabled = False
    return config


def _make_agent_config() -> AgentConfig:
    return AgentConfig(
        prompt_path=Path("/tmp/prompt.txt"),
        model="sonnet",
        timeout_minutes=45,
    )


def test_view_model_active_session_and_dashboard_data():
    config = _make_config()
    agent_config = _make_agent_config()
    config.agents = {"agent:web": agent_config}

    issue = Issue(number=12, title="Fix bug", labels=["agent:web"])
    session_key = SessionKey(issue=FakeIssueKey("12"), task=TaskKind.REVIEW)
    session = Session(
        key=session_key,
        issue=issue,
        agent_config=agent_config,
        terminal_id="review-12",
        worktree_path=Path("/tmp/worktree-12"),
        branch_name="feature/12",
        started_at=datetime.now() - timedelta(minutes=5),
    )

    state = OrchestratorState(active_sessions=[session], startup_status="complete")
    orchestrator = _OrchestratorStub(state=state, config=config)

    view_model = build_dashboard_view_model(
        orchestrator,
        queue_page=1,
        active_tab="flow",
        e2e_page=1,
        e2e_status_provider=lambda _: {"enabled": False, "running": False},
    )

    assert view_model.active_count == 1
    assert view_model.issues == view_model.active_items
    assert view_model.active_items[0]["status"] == "active"
    assert view_model.active_items[0]["flow_stage"] == "review"
    assert view_model.active_items[0]["action_hint"] == "Click to view agent UI log"
    assert view_model.flow_columns
    assert view_model.flow_columns[1]["id"] == "running"
    assert view_model.flow_columns[1]["count"] == 1

    dashboard_data = view_model.dashboard_data()
    assert dashboard_data["paused"] is False
    assert dashboard_data["queueRefreshSeconds"] == 600
    assert dashboard_data["agents"] == ["agent:web"]
    assert "scope" in dashboard_data
    assert dashboard_data["refresh"]["fetchLayerEnabled"] is True


def test_view_model_queue_and_blocked_items():
    config = _make_config()
    agent_config = _make_agent_config()
    config.agents = {"agent:web": agent_config}

    queued_issue = Issue(
        number=1,
        title="Queued",
        labels=["agent:web"],
        body="Depends-on: #5",
    )
    blocked_issue = Issue(
        number=2,
        title="Blocked",
        labels=["agent:web", "blocked"],
    )

    state = OrchestratorState(
        startup_status="complete",
        cached_queue_issues=[queued_issue, blocked_issue],
        pending_reviews=[
            PendingReview(
                issue_key=FakeIssueKey("1"),
                pr_number=101,
                pr_url="https://github.com/test/repo/pull/101",
                branch_name="feature/1",
                _issue_number=1,
            )
        ],
        dependency_problems={
            2: DependencyProblem(
                issue_number=2,
                issue_title="Blocked",
                blocked_by=[(5, "Dep", "open")],
                summary="Blocked - waiting on: #5",
            )
        },
    )
    orchestrator = _OrchestratorStub(state=state, config=config)

    view_model = build_dashboard_view_model(
        orchestrator,
        queue_page=1,
        active_tab="flow",
        e2e_page=1,
        e2e_status_provider=lambda _: {"enabled": False, "running": False},
    )

    assert len(view_model.queue_items) == 1
    assert len(view_model.blocked_items) == 1

    queue_item = view_model.queue_items[0]
    assert queue_item["issue_number"] == 1
    assert queue_item["flow_stage"] == "review"
    assert queue_item["has_dependencies"] is True
    assert "#5" in (queue_item["dependency_summary"] or "")

    blocked_item = view_model.blocked_items[0]
    assert blocked_item["issue_number"] == 2
    assert blocked_item["status"] == "blocked"
    assert "blocked" in (blocked_item["blocked_summary"] or "").lower()
    assert "waiting on" in (blocked_item["blocked_summary"] or "").lower()
    # Dependency-blocked items (issue #2) stay in blocked because they also have
    # the "blocked" label — only pure dependency blocks stay in queued
    assert view_model.blocked_count == 1


def test_view_model_includes_refresh_freshness_metadata():
    config = _make_config()
    config.flow_refresh_stale_seconds = 60
    issue = Issue(number=21, title="Stale card", labels=["agent:web"])
    state = OrchestratorState(
        startup_status="complete",
        cached_queue_issues=[issue],
    )
    state.queue_last_refresh_at = datetime.now().timestamp() - 300
    orchestrator = _OrchestratorStub(state=state, config=config)

    view_model = build_dashboard_view_model(
        orchestrator,
        queue_page=1,
        active_tab="flow",
        e2e_page=1,
        e2e_status_provider=lambda _: {"enabled": False, "running": False},
    )

    queued_col = next(col for col in view_model.flow_columns if col["id"] == "queued")
    assert queued_col["items"]
    card = queued_col["items"][0]
    assert card["issue_number"] == 21
    assert card["is_stale"] is True
    assert "ago" in card["last_refreshed_label"]
    refresh_meta = view_model.dashboard_data()["refresh"]
    assert refresh_meta["flowLazyEnabled"] is True
    assert refresh_meta["networkSyncSeconds"] == 60
    assert refresh_meta["flowStaleSeconds"] == 60
    assert refresh_meta["freshnessMode"] == "balanced"
    assert refresh_meta["apiBudget"] == "medium"
    assert refresh_meta["attentionPriority"] == "strict"

    gh_usage = view_model.dashboard_data()["githubUsage"]
    assert "total_calls" in gh_usage
    assert "calls_per_minute" in gh_usage


def test_pr_pending_issue_not_shown_in_queued_flow_column():
    config = _make_config()
    agent_config = _make_agent_config()
    config.agents = {"agent:web": agent_config}

    pr_pending_issue = Issue(
        number=4072,
        title="PR pending merge",
        labels=["agent:web", "pr-pending"],
    )
    state = OrchestratorState(
        startup_status="complete",
        cached_queue_issues=[pr_pending_issue],
    )
    orchestrator = _OrchestratorStub(state=state, config=config)

    view_model = build_dashboard_view_model(
        orchestrator,
        queue_page=1,
        active_tab="flow",
        e2e_page=1,
        e2e_status_provider=lambda _: {"enabled": False, "running": False},
    )

    queued_col = next(col for col in view_model.flow_columns if col["id"] == "queued")
    assert queued_col["count"] == 0
    assert all(item["issue_number"] != 4072 for item in queued_col["items"])
    assert any(item["issue_number"] == 4072 for item in view_model.awaiting_merge_items)


def test_view_model_includes_refresh_staleness_meta():
    config = _make_config()
    agent_config = _make_agent_config()
    config.agents = {"agent:web": agent_config}
    config.queue_refresh_seconds = 300
    queued_issue = Issue(number=22, title="Queued", labels=["agent:web"])

    state = OrchestratorState(
        startup_status="complete",
        cached_queue_issues=[queued_issue],
        issue_refresh_timestamps={22: datetime.now().timestamp() - 1200},
    )
    orchestrator = _OrchestratorStub(state=state, config=config)

    view_model = build_dashboard_view_model(
        orchestrator,
        queue_page=1,
        active_tab="flow",
        e2e_page=1,
        e2e_status_provider=lambda _: {"enabled": False, "running": False},
    )

    queue_item = view_model.queue_items[0]
    assert queue_item["refresh_age_label"]
    assert queue_item["refresh_age_seconds"] is not None
    assert queue_item["is_stale"] is True


def test_view_model_includes_refresh_staleness_meta():
    config = _make_config()
    agent_config = _make_agent_config()
    config.agents = {"agent:web": agent_config}
    config.queue_refresh_seconds = 300
    queued_issue = Issue(number=22, title="Queued", labels=["agent:web"])

    state = OrchestratorState(
        startup_status="complete",
        cached_queue_issues=[queued_issue],
        issue_refresh_timestamps={22: datetime.now().timestamp() - 1200},
    )
    orchestrator = _OrchestratorStub(state=state, config=config)

    view_model = build_dashboard_view_model(
        orchestrator,
        queue_page=1,
        active_tab="flow",
        e2e_page=1,
        e2e_status_provider=lambda _: {"enabled": False, "running": False},
    )

    queue_item = view_model.queue_items[0]
    assert queue_item["refresh_age_label"]
    assert queue_item["refresh_age_seconds"] is not None
    assert queue_item["is_stale"] is True


def test_view_model_history_routing():
    config = _make_config()
    state = OrchestratorState(
        startup_status="complete",
        session_history=[
            SessionHistoryEntry(
                issue_number=10,
                title="Failed",
                agent_type="agent:web",
                status="failed",
                runtime_minutes=12,
            ),
            SessionHistoryEntry(
                issue_number=11,
                title="Needs Human",
                agent_type="agent:web",
                status="needs_human",
                runtime_minutes=8,
            ),
        ],
    )
    orchestrator = _OrchestratorStub(state=state, config=config)

    view_model = build_dashboard_view_model(
        orchestrator,
        queue_page=1,
        active_tab="history",
        e2e_page=1,
        e2e_status_provider=lambda _: {"enabled": False, "running": False},
    )

    blocked_numbers = {item["issue_number"] for item in view_model.blocked_items}

    # Both failed (#10) and needs_human (#11) now go to blocked column
    assert 10 in blocked_numbers
    assert 11 in blocked_numbers


def test_exclude_flow_overlaps_handles_string_issue_numbers():
    backlog_items = [{"issue_number": 4070, "title": "Backlog"}]
    queue_items = [{"issue_number": "4070", "title": "Queued"}]

    result = _exclude_flow_overlaps(
        backlog_items=backlog_items,
        queue_items=queue_items,
        active_items=[],
        blocked_items=[],
        completed_items=[],
    )

    assert result == []


def test_normalize_status_reason_drops_sync_noise() -> None:
    assert _normalize_status_reason("Synced 10s ago") is None
    assert _normalize_status_reason(" synced 5m ago ") is None
    assert _normalize_status_reason("blocked by dependency #100") == "blocked by dependency #100"


def test_view_model_history_dedupes_latest_per_issue():
    config = _make_config()
    state = OrchestratorState(
        startup_status="complete",
        session_history=[
            SessionHistoryEntry(
                issue_number=10,
                title="Failed First",
                agent_type="agent:web",
                status="failed",
                runtime_minutes=12,
            ),
            SessionHistoryEntry(
                issue_number=10,
                title="Blocked Latest",
                agent_type="agent:web",
                status="blocked",
                runtime_minutes=3,
            ),
        ],
    )
    orchestrator = _OrchestratorStub(state=state, config=config)

    view_model = build_dashboard_view_model(
        orchestrator,
        queue_page=1,
        active_tab="history",
        e2e_page=1,
        e2e_status_provider=lambda _: {"enabled": False, "running": False},
    )

    combined = view_model.history_items + view_model.blocked_items
    assert len([item for item in combined if item["issue_number"] == 10]) == 1
    assert any(item["status"] == "blocked" for item in combined if item["issue_number"] == 10)


def test_view_model_e2e_items_from_provider():
    config = _make_config()
    config.e2e.enabled = True
    state = OrchestratorState(startup_status="complete")
    orchestrator = _OrchestratorStub(state=state, config=config)

    def e2e_status_provider(_):
        return {
            "enabled": True,
            "running": True,
            "needs_attention": True,
            "untriaged_count": 2,
            "last_run": {"id": 7, "relative_time": "1h ago"},
            "failed_tests": [
                {"nodeid": "tests/test_a.py::test_x", "duration_seconds": 1.2},
            ],
        }

    view_model = build_dashboard_view_model(
        orchestrator,
        queue_page=1,
        active_tab="e2e",
        e2e_page=1,
        e2e_status_provider=e2e_status_provider,
    )

    assert view_model.e2e_count == 2
    assert len(view_model.e2e_items) == 2
    assert view_model.issues == view_model.e2e_items
    assert any(item.get("e2e_running") for item in view_model.e2e_items)
    assert any(item.get("status") == "needs_attention" for item in view_model.e2e_items)
    e2e_vm = view_model.e2e_status.get("view_model", {})
    assert e2e_vm.get("badge", {}).get("state") in {"failed", "running", "passed", "idle"}
    assert isinstance(e2e_vm.get("runs"), list)


def test_view_model_api_endpoint():
    from fastapi.testclient import TestClient
    from issue_orchestrator.entrypoints import web
    from issue_orchestrator.entrypoints.web import get_orchestrator, set_orchestrator

    config = _make_config()
    state = OrchestratorState(startup_status="complete")
    orchestrator = _OrchestratorStub(state=state, config=config)

    original = get_orchestrator()
    set_orchestrator(orchestrator)
    try:
        client = TestClient(web.app)
        response = client.get("/api/view-model")
        assert response.status_code == 200
        data = response.json()
        assert data["dashboard_data"]["repo"] == "test/repo"
        assert data["dashboard_data"]["queueRefreshSeconds"] == 600
    finally:
        set_orchestrator(original)


def test_view_model_matches_public_contract():
    config = _make_config()
    state = OrchestratorState(startup_status="complete")
    orchestrator = _OrchestratorStub(state=state, config=config)

    view_model = build_dashboard_view_model(
        orchestrator,
        queue_page=1,
        active_tab="flow",
        e2e_page=1,
        e2e_status_provider=lambda _: {"enabled": False, "running": False},
    )

    DashboardViewModelContract.model_validate(view_model.to_dict())


def test_view_model_surfaces_provider_circuit_breaker_status():
    from datetime import timezone

    from issue_orchestrator.ports.provider_resilience import (
        InMemoryProviderCircuitStore,
        ProviderCircuitState,
    )

    config = _make_config()
    state = OrchestratorState(startup_status="complete")

    store = InMemoryProviderCircuitStore()
    now = datetime.now(timezone.utc)
    open_until = now + timedelta(minutes=5)
    store.save(ProviderCircuitState(
        provider="claude",
        open_until=open_until,
        consecutive_outages=2,
        last_error_summary="Rate limit exceeded",
        updated_at=now,
    ))

    class _FakeProviderResilience:
        def __init__(self, s):
            self.store = s

    class _FakeDeps:
        def __init__(self, s):
            self.provider_resilience = _FakeProviderResilience(s)

    @dataclass
    class _OrchestratorWithDeps:
        state: OrchestratorState
        config: Config
        deps: object
        shutdown_requested: bool = False

    orchestrator = _OrchestratorWithDeps(
        state=state,
        config=config,
        deps=_FakeDeps(store),
    )

    view_model = build_dashboard_view_model(
        orchestrator,
        queue_page=1,
        active_tab="kanban",
        e2e_page=1,
        e2e_status_provider=lambda _: {"enabled": False, "running": False},
    )

    cb = view_model.dashboard_data()["providerCircuitBreakers"]
    assert len(cb) == 1
    assert cb[0]["provider"] == "claude"
    assert cb[0]["is_open"] is True
    assert cb[0]["consecutive_outages"] == 2
    assert cb[0]["last_error_summary"] == "Rate limit exceeded"
    assert cb[0]["open_until"] is not None


def test_view_model_circuit_breaker_empty_when_no_deps():
    config = _make_config()
    state = OrchestratorState(startup_status="complete")
    orchestrator = _OrchestratorStub(state=state, config=config)  # no .deps

    view_model = build_dashboard_view_model(
        orchestrator,
        queue_page=1,
        active_tab="kanban",
        e2e_page=1,
        e2e_status_provider=lambda _: {"enabled": False, "running": False},
    )

    assert view_model.dashboard_data()["providerCircuitBreakers"] == []
