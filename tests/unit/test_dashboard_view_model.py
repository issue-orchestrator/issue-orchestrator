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
from issue_orchestrator.view_models.dashboard import build_dashboard_view_model
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
        active_tab="active",
        e2e_page=1,
        e2e_status_provider=lambda _: {"enabled": False, "running": False},
    )

    assert view_model.active_count == 1
    assert view_model.issues == view_model.active_items
    assert view_model.active_items[0]["status"] == "active"
    assert view_model.active_items[0]["flow_stage"] == "review"
    assert view_model.active_items[0]["action_hint"] == "Click to view agent UI log"

    dashboard_data = view_model.dashboard_data()
    assert dashboard_data["paused"] is False
    assert dashboard_data["queueRefreshSeconds"] == 600
    assert dashboard_data["agents"] == ["agent:web"]


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
        active_tab="queue",
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
    assert "blocked" in (blocked_item["blocked_summary"] or "")
    assert "waiting on" in (blocked_item["blocked_summary"] or "")


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

    history_numbers = {item["issue_number"] for item in view_model.history_items}
    blocked_numbers = {item["issue_number"] for item in view_model.blocked_items}

    assert 10 in history_numbers
    assert 11 in blocked_numbers


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
        active_tab="active",
        e2e_page=1,
        e2e_status_provider=lambda _: {"enabled": False, "running": False},
    )

    DashboardViewModelContract.model_validate(view_model.to_dict())
