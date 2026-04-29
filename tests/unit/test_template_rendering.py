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
from issue_orchestrator.view_models.dashboard_assets import DASHBOARD_CSS_CHUNKS
from issue_orchestrator.view_models.dashboard_assets import DASHBOARD_JS_CHUNKS
from issue_orchestrator.view_models.dashboard import build_dashboard_view_model


TEMPLATE_DIR = Path(__file__).parent.parent.parent / "src" / "issue_orchestrator" / "templates"
STATIC_JS_DIR = (
    Path(__file__).parent.parent.parent / "src" / "issue_orchestrator" / "static" / "js"
)


def read_dashboard_js_bundle() -> str:
    return "\n".join(
        (STATIC_JS_DIR / "dashboard" / chunk).read_text(encoding="utf-8")
        for chunk in DASHBOARD_JS_CHUNKS
    )


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

    active_tab = soup.select_one("#tab-dashboard.active")
    assert active_tab is not None
    assert active_tab.get("data-tab") == "kanban"
    columns = soup.select(".kanban-column")
    assert len(columns) == 5
    column_ids = [col["data-column"] for col in columns]
    assert column_ids == ["queued", "running", "blocked", "awaiting-merge", "completed"]
    assert "milestones=M7" in soup.select_one(".scope-summary").text


def test_dashboard_renders_manifest_js_chunks_in_order(jinja_env):
    config = make_config()
    state = OrchestratorState(startup_status="complete")
    vm = build_dashboard_view_model(
        OrchestratorStub(state=state, config=config),
        active_tab="flow",
        e2e_status_provider=e2e_disabled,
    )

    soup = render_dashboard(jinja_env, vm)

    script_sources = [
        script.get("src")
        for script in soup.find_all("script")
        if script.get("src")
    ]
    expected_chunks = [
        f"/static/js/dashboard/{chunk}"
        for chunk in DASHBOARD_JS_CHUNKS
    ]
    chunk_start = script_sources.index(expected_chunks[0])
    assert script_sources[chunk_start : chunk_start + len(expected_chunks)] == expected_chunks
    assert script_sources[chunk_start - 1] == "/static/vendor/xterm/addon-fit.js"
    assert script_sources[chunk_start + len(expected_chunks)] == "/static/js/dashboard.js"


def test_dashboard_renders_manifest_css_chunks_before_late_styles(jinja_env):
    config = make_config()
    state = OrchestratorState(startup_status="complete")
    vm = build_dashboard_view_model(
        OrchestratorStub(state=state, config=config),
        active_tab="flow",
        e2e_status_provider=e2e_disabled,
    )

    soup = render_dashboard(jinja_env, vm)

    stylesheet_hrefs = [
        link.get("href")
        for link in soup.find_all("link", rel="stylesheet")
        if link.get("href")
    ]
    expected_chunks = [
        f"/static/css/dashboard/{chunk}"
        for chunk in DASHBOARD_CSS_CHUNKS
    ]
    chunk_start = stylesheet_hrefs.index(expected_chunks[0])
    assert stylesheet_hrefs[chunk_start : chunk_start + len(expected_chunks)] == expected_chunks
    assert "/static/css/dashboard.css" not in stylesheet_hrefs
    assert stylesheet_hrefs[chunk_start + len(expected_chunks)] == "/static/css/ui_primitives.css"


def test_dashboard_js_compact_renderer_routes_running_cancel_to_menu():
    source = read_dashboard_js_bundle()
    assert "const hasTerminal = card.state_label === 'running' ? 'true' : 'false';" in source
    assert "data-has-terminal" in source
    assert "class=\"card-kill-btn\"" not in source
    assert "class=\"card-menu-btn\"" in source
    assert "openCompactCardActionsMenu(" in source


def test_dashboard_js_switch_tab_shows_loading_state():
    source = read_dashboard_js_bundle()
    assert "tab-nav-pending" in source
    assert "is-loading" in source
    assert "aria-busy" in source


def test_kanban_blocked_column_is_expandable(jinja_env):
    config = make_config()
    config.agents = {"agent:web": make_agent_config()}
    blocked = Issue(number=210, title="Blocked merge", labels=["agent:web", "blocked-needs-human"])
    state = OrchestratorState(startup_status="complete", cached_queue_issues=[blocked])
    vm = build_dashboard_view_model(
        OrchestratorStub(state=state, config=config),
        active_tab="kanban",
        e2e_status_provider=e2e_disabled,
    )

    soup = render_dashboard(jinja_env, vm)

    blocked_col = soup.select_one('[data-column="blocked"]')
    assert blocked_col is not None
    assert "expandable" in blocked_col.get("class", [])
    assert blocked_col.select_one(".column-expand-btn") is not None
    # Blocked column has triage filter bar
    assert blocked_col.select_one(".column-filter-bar") is not None
    filter_btns = blocked_col.select(".filter-btn")
    assert len(filter_btns) == 3
    assert [btn.text.strip() for btn in filter_btns] == ["All", "New", "Viewed"]
    badge_texts = [badge.text.strip() for badge in blocked_col.select(".card-badges .badge")]
    assert "agent:web" in badge_texts


def test_kanban_running_column_is_expandable_and_routes_cancel_to_menu(jinja_env):
    config = make_config()
    config.agents = {"agent:web": make_agent_config()}
    running_issue = Issue(number=4057, title="Running issue", labels=["agent:web", "in-progress"])
    state = OrchestratorState(startup_status="complete", active_sessions=[make_session(running_issue)])
    vm = build_dashboard_view_model(
        OrchestratorStub(state=state, config=config),
        active_tab="kanban",
        e2e_status_provider=e2e_disabled,
    )

    soup = render_dashboard(jinja_env, vm)

    running_col = soup.select_one('[data-column="running"]')
    assert running_col is not None
    assert "expandable" in running_col.get("class", [])
    assert running_col.select_one(".column-expand-btn") is not None
    assert running_col.select_one(".card-kill-btn") is None
    menu_btn = running_col.select_one(".card-menu-btn")
    assert menu_btn is not None
    assert menu_btn.get("data-has-terminal") == "true"


def test_kanban_completed_column_session_scoped(jinja_env):
    config = make_config()
    state = OrchestratorState(
        startup_status="complete",
        session_history=[
            SessionHistoryEntry(
                issue_number=7,
                title="Done issue",
                agent_type="agent:web",
                status="completed",
                runtime_minutes=9,
                pr_url="https://example.test/pr/7",
            )
        ],
    )
    vm = build_dashboard_view_model(
        OrchestratorStub(state=state, config=config),
        active_tab="kanban",
        e2e_status_provider=e2e_disabled,
    )

    soup = render_dashboard(jinja_env, vm)

    awaiting_merge_col = soup.select_one('[data-column="awaiting-merge"]')
    assert awaiting_merge_col is not None
    assert "expandable" in awaiting_merge_col.get("class", [])
    assert awaiting_merge_col.select_one(".count").text.strip() == "1"
    pr_link = awaiting_merge_col.select_one(".card-head-actions .card-gh.card-pr-link")
    assert pr_link is not None
    assert pr_link.get("href") == "https://example.test/pr/7"
    assert pr_link.text.strip() == "PR ↗"
    assert pr_link.get("title") == "Open PR on GitHub"
    menu_btn = awaiting_merge_col.select_one(".card-menu-btn")
    assert menu_btn is not None
    assert menu_btn.get("data-pr-url") == "https://example.test/pr/7"


def test_awaiting_merge_template_renders_one_pr_card_when_queue_and_history_overlap(jinja_env):
    config = make_config()
    config.agents = {"agent:web": make_agent_config()}
    state = OrchestratorState(
        startup_status="complete",
        cached_queue_issues=[
            Issue(
                number=280,
                title="Click inference",
                labels=["agent:web", "pr-pending"],
            ),
        ],
        session_history=[
            SessionHistoryEntry(
                issue_number=280,
                title="Click inference",
                agent_type="agent:web",
                status="completed",
                runtime_minutes=9,
                pr_url="https://example.test/pr/327",
            )
        ],
    )
    vm = build_dashboard_view_model(
        OrchestratorStub(state=state, config=config),
        active_tab="kanban",
        e2e_status_provider=e2e_disabled,
    )

    soup = render_dashboard(jinja_env, vm)

    awaiting_merge_col = soup.select_one('[data-column="awaiting-merge"]')
    assert awaiting_merge_col is not None
    assert awaiting_merge_col.select_one(".count").text.strip() == "1"
    cards = awaiting_merge_col.select('.column-cards .issue-card[data-issue="280"]')
    assert len(cards) == 1
    pr_link = awaiting_merge_col.select_one(".card-head-actions .card-gh.card-pr-link")
    assert pr_link is not None
    assert pr_link.get("href") == "https://example.test/pr/327"


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


def test_flow_refresh_preferences_modal_is_rendered(jinja_env):
    config = make_config()
    state = OrchestratorState(startup_status="complete")
    vm = build_dashboard_view_model(
        OrchestratorStub(state=state, config=config),
        active_tab="flow",
        e2e_status_provider=e2e_disabled,
    )
    soup = render_dashboard(jinja_env, vm)
    modal = soup.select_one("#flowRefreshPrefsModal")
    assert modal is not None
    assert "Flow refresh preferences" in soup.text
    assert soup.select_one("#flowRefreshOverrideEnabled") is not None
    assert soup.select_one("#flowFreshnessMode") is not None
    assert soup.select_one("#flowApiBudget") is not None
    assert soup.select_one("#flowAttentionPriority") is not None


def test_github_usage_pill_is_rendered(jinja_env):
    config = make_config()
    state = OrchestratorState(startup_status="complete")
    vm = build_dashboard_view_model(
        OrchestratorStub(state=state, config=config),
        active_tab="flow",
        e2e_status_provider=e2e_disabled,
    )
    soup = render_dashboard(jinja_env, vm)
    assert soup.select_one("#ghUsagePill") is not None
    assert soup.select_one("#ghUsagePanel") is not None


def test_embedded_header_elements_in_tab_bar(jinja_env):
    config = make_config()
    state = OrchestratorState(startup_status="complete")
    vm = build_dashboard_view_model(
        OrchestratorStub(state=state, config=config),
        active_tab="flow",
        e2e_status_provider=e2e_disabled,
    )
    soup = render_dashboard(jinja_env, vm)
    # Embedded header elements exist in the tab bar and are hidden by CSS
    # until the pre-paint boot state marks the page as embedded.
    tab_bar = soup.select_one(".dashboard-tabs")
    assert tab_bar.select_one("#embeddedBack") is not None
    assert tab_bar.select_one("#embeddedBackLabel") is not None
    assert tab_bar.select_one("#embeddedRepoName") is not None
    assert tab_bar.select_one("#embeddedBadge") is not None
    assert tab_bar.select_one("#embeddedScopeBtn") is not None
    assert "style" not in tab_bar.select_one("#embeddedBack").attrs


def test_starting_dashboard_renders_initializing_status(jinja_env):
    config = make_config()
    state = OrchestratorState(startup_status="starting")
    vm = build_dashboard_view_model(
        OrchestratorStub(state=state, config=config),
        active_tab="flow",
        e2e_status_provider=e2e_disabled,
    )
    soup = render_dashboard(jinja_env, vm)
    init_status = soup.select_one("#dashboardInitStatus")
    assert init_status is not None
    assert "is-active" in init_status.get("class", [])
    assert "Initializing orchestrator" in init_status.get_text(" ")


def test_e2e_tab_and_panels_render(jinja_env):
    config = make_config()
    config.e2e.enabled = True
    state = OrchestratorState(startup_status="complete")
    vm = build_dashboard_view_model(
        OrchestratorStub(state=state, config=config),
        active_tab="e2e",
        e2e_status_provider=lambda _: {
            "enabled": True,
            "running": False,
            "needs_attention": True,
            "untriaged_count": 2,
            "last_run": {"id": 9, "status": "failed", "relative_time": "3m ago"},
            "next_run": {"next_run_reason": "interval", "next_run_at": "2026-02-08T20:00:00Z"},
            "failed_tests": [],
        },
    )
    soup = render_dashboard(jinja_env, vm)
    assert soup.select_one("#tab-e2e.active") is not None
    assert soup.select_one("#panel-e2e") is not None
    assert soup.select_one("#e2eHeaderBadge") is not None
    assert soup.select_one("#e2eControls") is not None
