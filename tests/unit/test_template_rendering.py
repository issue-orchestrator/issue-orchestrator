"""Template rendering tests using Jinja2 + BeautifulSoup."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from bs4 import BeautifulSoup
from jinja2 import Environment, FileSystemLoader

from issue_orchestrator.domain.issue_key import FakeIssueKey
from issue_orchestrator.domain.models import AgentConfig, Issue, OrchestratorState, Session, SessionHistoryEntry
from issue_orchestrator.domain.session_key import SessionKey, TaskKind
from issue_orchestrator.infra.config import Config
from issue_orchestrator.view_models.dashboard import build_dashboard_view_model


TEMPLATE_DIR = Path(__file__).parent.parent.parent / "src" / "issue_orchestrator" / "templates"


@pytest.fixture
def jinja_env() -> Environment:
    return Environment(loader=FileSystemLoader(str(TEMPLATE_DIR)))


@dataclass
class OrchestratorStub:
    state: OrchestratorState
    config: Config
    shutdown_requested: bool = False


def make_config() -> Config:
    config = Config()
    config.repo = "test/repo"
    config.repo_root = Path("/tmp/repo")
    config.queue_refresh_seconds = 600
    config.terminal_adapter = "subprocess"
    config.filtering.milestones = ["M7"]
    config.filtering.label = "agent-ready"
    config.e2e.enabled = False
    return config


def make_agent_config() -> AgentConfig:
    return AgentConfig(prompt_path=Path("/tmp/prompt.txt"), model="sonnet", timeout_minutes=45)


def make_session(issue: Issue, task: TaskKind = TaskKind.CODE) -> Session:
    agent_config = make_agent_config()
    return Session(
        key=SessionKey(issue=FakeIssueKey(str(issue.number)), task=task),
        issue=issue,
        agent_config=agent_config,
        terminal_id=f"issue-{issue.number}",
        worktree_path=Path(f"/tmp/worktree-{issue.number}"),
        branch_name=f"feature/{issue.number}",
        started_at=datetime.now() - timedelta(minutes=6),
    )


def render_dashboard(jinja_env: Environment, view_model) -> BeautifulSoup:
    template = jinja_env.get_template("dashboard.html")
    html = template.render(**view_model.template_context())
    return BeautifulSoup(html, "html.parser")


def e2e_disabled(_config) -> dict:
    return {"enabled": False, "running": False}


def test_flow_dashboard_renders_columns_and_scope(jinja_env):
    config = make_config()
    config.agents = {"agent:web": make_agent_config()}
    issue = Issue(number=101, title="Ship board", labels=["agent:web"])
    state = OrchestratorState(
        startup_status="complete",
        active_sessions=[make_session(issue)],
        cached_queue_issues=[Issue(number=102, title="Queued", labels=["agent:web"])],
    )
    vm = build_dashboard_view_model(
        OrchestratorStub(state=state, config=config),
        active_tab="flow",
        e2e_status_provider=e2e_disabled,
    )

    soup = render_dashboard(jinja_env, vm)

    assert soup.select_one("#tab-flow.active") is not None
    columns = soup.select(".kanban-column")
    assert len(columns) == 5
    assert "milestones=M7" in soup.select_one(".scope-summary").text


def test_attention_view_renders_groups(jinja_env):
    config = make_config()
    config.agents = {"agent:web": make_agent_config()}
    blocked = Issue(number=210, title="Blocked merge", labels=["agent:web", "blocked-needs-human"])
    state = OrchestratorState(startup_status="complete", cached_queue_issues=[blocked])
    vm = build_dashboard_view_model(
        OrchestratorStub(state=state, config=config),
        active_tab="attention",
        e2e_status_provider=e2e_disabled,
    )

    soup = render_dashboard(jinja_env, vm)

    assert soup.select_one("#tab-attention.active") is not None
    assert soup.select_one(".attention-group") is not None


def test_history_view_renders_items(jinja_env):
    config = make_config()
    state = OrchestratorState(
        startup_status="complete",
        session_history=[
            SessionHistoryEntry(issue_number=7, title="Done issue", agent_type="agent:web", status="completed", runtime_minutes=9)
        ],
    )
    vm = build_dashboard_view_model(
        OrchestratorStub(state=state, config=config),
        active_tab="history",
        e2e_status_provider=e2e_disabled,
    )

    soup = render_dashboard(jinja_env, vm)

    assert soup.select_one("#tab-history.active") is not None
    assert soup.select_one(".history-item") is not None


def test_status_badge_shows_running(jinja_env):
    config = make_config()
    state = OrchestratorState(startup_status="complete")
    vm = build_dashboard_view_model(
        OrchestratorStub(state=state, config=config),
        active_tab="flow",
        e2e_status_provider=e2e_disabled,
    )

    soup = render_dashboard(jinja_env, vm)

    badge = soup.select_one(".status-badge")
    assert badge is not None
    assert "Running" in badge.text


def test_issue_detail_drawer_is_rendered(jinja_env):
    config = make_config()
    state = OrchestratorState(startup_status="complete")
    vm = build_dashboard_view_model(
        OrchestratorStub(state=state, config=config),
        active_tab="flow",
        e2e_status_provider=e2e_disabled,
    )
    soup = render_dashboard(jinja_env, vm)
    drawer = soup.select_one("#issueDetailDrawer")
    assert drawer is not None
    assert drawer.get("role") == "dialog"
    assert drawer.get("aria-modal") == "true"
    assert drawer.get("aria-labelledby") == "issueDetailTitle"
