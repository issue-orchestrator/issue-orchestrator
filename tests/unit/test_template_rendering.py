"""Template rendering tests using Jinja2 + BeautifulSoup.

These tests verify that templates produce correct HTML structure
given specific view model data, without needing a browser.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from bs4 import BeautifulSoup
from jinja2 import Environment, FileSystemLoader

from issue_orchestrator.domain.issue_key import FakeIssueKey
from issue_orchestrator.domain.models import (
    AgentConfig,
    DependencyProblem,
    Issue,
    OrchestratorState,
    Session,
    SessionHistoryEntry,
)
from issue_orchestrator.domain.session_key import SessionKey, TaskKind
from issue_orchestrator.infra.config import Config
from issue_orchestrator.view_models.dashboard import build_dashboard_view_model


# -----------------------------------------------------------------------------
# Test Infrastructure
# -----------------------------------------------------------------------------


TEMPLATE_DIR = Path(__file__).parent.parent.parent / "src" / "issue_orchestrator" / "templates"


@pytest.fixture
def jinja_env() -> Environment:
    """Create Jinja2 environment for template rendering."""
    return Environment(loader=FileSystemLoader(str(TEMPLATE_DIR)))


def render_dashboard(jinja_env: Environment, view_model) -> BeautifulSoup:
    """Render dashboard template and return parsed HTML."""
    template = jinja_env.get_template("dashboard.html")
    html = template.render(**view_model.template_context())
    return BeautifulSoup(html, "lxml")


# -----------------------------------------------------------------------------
# Mock Orchestrator Setup
# -----------------------------------------------------------------------------


@dataclass
class OrchestratorStub:
    """Minimal orchestrator stub for view model building."""

    state: OrchestratorState
    config: Config
    shutdown_requested: bool = False


def make_config() -> Config:
    """Create a test config."""
    config = Config()
    config.repo = "test/repo"
    config.repo_root = Path("/tmp/repo")
    config.queue_refresh_seconds = 600
    config.terminal_adapter = "subprocess"
    config.e2e.enabled = False
    return config


def make_agent_config() -> AgentConfig:
    """Create a test agent config."""
    return AgentConfig(
        prompt_path=Path("/tmp/prompt.txt"),
        model="sonnet",
        timeout_minutes=45,
    )


def make_issue(number: int, title: str, labels: list[str] | None = None) -> Issue:
    """Create a test issue."""
    return Issue(
        number=number,
        title=title,
        labels=labels or ["agent:web"],
    )


def make_session(issue: Issue, task: TaskKind = TaskKind.CODE) -> Session:
    """Create a test session."""
    agent_config = make_agent_config()
    issue_key = FakeIssueKey(name=str(issue.number))
    session_key = SessionKey(issue=issue_key, task=task)
    terminal_id = f"review-{issue.number}" if task == TaskKind.REVIEW else f"issue-{issue.number}"
    return Session(
        key=session_key,
        issue=issue,
        agent_config=agent_config,
        terminal_id=terminal_id,
        worktree_path=Path(f"/tmp/worktree-{issue.number}"),
        branch_name=f"feature/{issue.number}",
        started_at=datetime.now() - timedelta(minutes=5),
    )


def make_orchestrator(
    *,
    paused: bool = False,
    shutdown_requested: bool = False,
    startup_status: str = "complete",
    active_sessions: list[Session] | None = None,
    queue_issues: list[Issue] | None = None,
    session_history: list[SessionHistoryEntry] | None = None,
    dependency_problems: dict[int, DependencyProblem] | None = None,
) -> OrchestratorStub:
    """Create a configured orchestrator stub."""
    config = make_config()
    config.agents = {"agent:web": make_agent_config()}

    state = OrchestratorState(
        startup_status=startup_status,
        paused=paused,
        active_sessions=active_sessions or [],
        cached_queue_issues=queue_issues or [],
        session_history=session_history or [],
        pending_reviews=[],
        dependency_problems=dependency_problems or {},
    )

    return OrchestratorStub(state=state, config=config, shutdown_requested=shutdown_requested)


def e2e_disabled(_config) -> dict:
    """E2E status provider that returns disabled state."""
    return {"enabled": False, "running": False}


# -----------------------------------------------------------------------------
# Status Badge Tests
# -----------------------------------------------------------------------------


class TestStatusBadge:
    """Tests for the main status badge in the header."""

    def test_running_badge_when_active(self, jinja_env):
        """Running badge shown when orchestrator is active."""
        orchestrator = make_orchestrator()
        vm = build_dashboard_view_model(orchestrator, e2e_status_provider=e2e_disabled)
        soup = render_dashboard(jinja_env, vm)

        badge = soup.select_one(".status-badge")
        assert badge is not None
        assert "status-running" in badge.get("class", [])
        assert "Running" in badge.text

    def test_paused_badge_when_paused(self, jinja_env):
        """Paused badge shown when orchestrator is paused."""
        orchestrator = make_orchestrator(paused=True)
        vm = build_dashboard_view_model(orchestrator, e2e_status_provider=e2e_disabled)
        soup = render_dashboard(jinja_env, vm)

        badge = soup.select_one(".status-badge")
        assert badge is not None
        assert "status-paused" in badge.get("class", [])
        assert "Paused" in badge.text

    def test_pausing_badge_when_paused_with_active_sessions(self, jinja_env):
        """Pausing... badge shown when paused but sessions still running."""
        issue = make_issue(1, "Active Issue")
        session = make_session(issue)
        orchestrator = make_orchestrator(paused=True, active_sessions=[session])
        vm = build_dashboard_view_model(orchestrator, e2e_status_provider=e2e_disabled)
        soup = render_dashboard(jinja_env, vm)

        badge = soup.select_one(".status-badge")
        assert badge is not None
        assert "status-paused" in badge.get("class", [])
        assert "Pausing" in badge.text
        assert "(1)" in badge.text

    def test_shutting_down_badge(self, jinja_env):
        """Shutting down badge shown when shutdown requested with active sessions."""
        issue = make_issue(1, "Active Issue")
        session = make_session(issue)
        orchestrator = make_orchestrator(shutdown_requested=True, active_sessions=[session])
        vm = build_dashboard_view_model(orchestrator, e2e_status_provider=e2e_disabled)
        soup = render_dashboard(jinja_env, vm)

        badge = soup.select_one(".status-badge")
        assert badge is not None
        assert "Shutting down" in badge.text

    def test_stopped_badge(self, jinja_env):
        """Stopped badge shown when shutdown requested with no active sessions."""
        orchestrator = make_orchestrator(shutdown_requested=True)
        vm = build_dashboard_view_model(orchestrator, e2e_status_provider=e2e_disabled)
        soup = render_dashboard(jinja_env, vm)

        badge = soup.select_one(".status-badge")
        assert badge is not None
        assert "Stopped" in badge.text

    def test_starting_badge(self, jinja_env):
        """Starting badge shown during startup."""
        orchestrator = make_orchestrator(startup_status="pending")
        vm = build_dashboard_view_model(orchestrator, e2e_status_provider=e2e_disabled)
        soup = render_dashboard(jinja_env, vm)

        badge = soup.select_one(".status-badge")
        assert badge is not None
        assert "status-starting" in badge.get("class", [])
        assert "Starting" in badge.text


# -----------------------------------------------------------------------------
# Tab Bar Tests
# -----------------------------------------------------------------------------


class TestTabBar:
    """Tests for the tab bar rendering."""

    def test_active_tab_has_active_class(self, jinja_env):
        """Selected tab has 'active' class."""
        orchestrator = make_orchestrator()
        vm = build_dashboard_view_model(
            orchestrator, active_tab="queue", e2e_status_provider=e2e_disabled
        )
        soup = render_dashboard(jinja_env, vm)

        queue_tab = soup.select_one("#tab-queue")
        assert queue_tab is not None
        assert "active" in queue_tab.get("class", [])

        active_tab = soup.select_one("#tab-active")
        assert active_tab is not None
        assert "active" not in active_tab.get("class", [])

    def test_tab_badges_show_counts(self, jinja_env):
        """Tab badges show correct counts."""
        issue1 = make_issue(1, "Active Issue")
        session = make_session(issue1)
        queue_issue = make_issue(2, "Queued Issue")

        orchestrator = make_orchestrator(
            active_sessions=[session],
            queue_issues=[queue_issue],
        )
        vm = build_dashboard_view_model(orchestrator, e2e_status_provider=e2e_disabled)
        soup = render_dashboard(jinja_env, vm)

        active_badge = soup.select_one("#tab-active .tab-badge")
        assert active_badge is not None
        assert "1" in active_badge.text

        queue_badge = soup.select_one("#tab-queue .tab-badge")
        assert queue_badge is not None
        assert "1" in queue_badge.text

    def test_empty_badges_have_empty_class(self, jinja_env):
        """Empty tab badges have 'empty' class."""
        orchestrator = make_orchestrator()
        vm = build_dashboard_view_model(orchestrator, e2e_status_provider=e2e_disabled)
        soup = render_dashboard(jinja_env, vm)

        active_badge = soup.select_one("#tab-active .tab-badge")
        assert active_badge is not None
        assert "empty" in active_badge.get("class", [])

    def test_blocked_tab_has_blocked_class_when_count_positive(self, jinja_env):
        """Blocked tab has special class when there are blocked issues."""
        blocked_issue = make_issue(1, "Blocked", labels=["agent:web", "blocked"])
        orchestrator = make_orchestrator(queue_issues=[blocked_issue])
        vm = build_dashboard_view_model(orchestrator, e2e_status_provider=e2e_disabled)
        soup = render_dashboard(jinja_env, vm)

        blocked_tab = soup.select_one("#tab-blocked")
        assert blocked_tab is not None
        assert "blocked-tab" in blocked_tab.get("class", [])

    def test_e2e_tab_hidden_when_disabled(self, jinja_env):
        """E2E tab not rendered when E2E is disabled."""
        orchestrator = make_orchestrator()
        vm = build_dashboard_view_model(orchestrator, e2e_status_provider=e2e_disabled)
        soup = render_dashboard(jinja_env, vm)

        e2e_tab = soup.select_one("#tab-e2e")
        assert e2e_tab is None

    def test_e2e_tab_shown_when_enabled(self, jinja_env):
        """E2E tab rendered when E2E is enabled."""
        orchestrator = make_orchestrator()

        def e2e_enabled(_config):
            return {"enabled": True, "running": False, "last_run": None}

        vm = build_dashboard_view_model(orchestrator, e2e_status_provider=e2e_enabled)
        soup = render_dashboard(jinja_env, vm)

        e2e_tab = soup.select_one("#tab-e2e")
        assert e2e_tab is not None


# -----------------------------------------------------------------------------
# Issue Row Tests
# -----------------------------------------------------------------------------


class TestIssueRowRendering:
    """Tests for individual issue row rendering."""

    def test_active_session_renders_correctly(self, jinja_env):
        """Active session row has correct structure."""
        issue = make_issue(123, "Fix the bug")
        session = make_session(issue)
        orchestrator = make_orchestrator(active_sessions=[session])
        vm = build_dashboard_view_model(orchestrator, e2e_status_provider=e2e_disabled)
        soup = render_dashboard(jinja_env, vm)

        row = soup.select_one(".issue-row")
        assert row is not None
        assert row.get("data-issue") == "123"
        assert row.get("data-status") == "active"

        issue_num = row.select_one(".issue-num")
        assert issue_num is not None
        assert "#123" in issue_num.text

        title = row.select_one(".issue-title")
        assert title is not None
        assert "Fix the bug" in title.text

    def test_active_row_has_active_class(self, jinja_env):
        """Active session row has 'active-row' class."""
        issue = make_issue(1, "Active Issue")
        session = make_session(issue)
        orchestrator = make_orchestrator(active_sessions=[session])
        vm = build_dashboard_view_model(orchestrator, e2e_status_provider=e2e_disabled)
        soup = render_dashboard(jinja_env, vm)

        row = soup.select_one(".issue-row")
        assert row is not None
        assert "active-row" in row.get("class", [])

    def test_issue_url_is_github_link(self, jinja_env):
        """Issue number links to GitHub."""
        issue = make_issue(42, "Test Issue")
        session = make_session(issue)
        orchestrator = make_orchestrator(active_sessions=[session])
        vm = build_dashboard_view_model(orchestrator, e2e_status_provider=e2e_disabled)
        soup = render_dashboard(jinja_env, vm)

        link = soup.select_one(".issue-num a")
        assert link is not None
        assert link.get("href") == "https://github.com/test/repo/issues/42"
        assert link.get("target") == "_blank"

    def test_has_terminal_button_for_active_session(self, jinja_env):
        """Active session has kill button."""
        issue = make_issue(1, "Active Issue")
        session = make_session(issue)
        orchestrator = make_orchestrator(active_sessions=[session])
        vm = build_dashboard_view_model(orchestrator, e2e_status_provider=e2e_disabled)
        soup = render_dashboard(jinja_env, vm)

        kill_btn = soup.select_one(".issue-kill-btn")
        assert kill_btn is not None

    def test_queue_issue_renders_correctly(self, jinja_env):
        """Queued issue row renders correctly."""
        issue = make_issue(456, "Queued task")
        orchestrator = make_orchestrator(queue_issues=[issue])
        vm = build_dashboard_view_model(
            orchestrator, active_tab="queue", e2e_status_provider=e2e_disabled
        )
        soup = render_dashboard(jinja_env, vm)

        row = soup.select_one(".issue-row")
        assert row is not None
        assert row.get("data-issue") == "456"
        assert "Queued task" in row.select_one(".issue-title").text


# -----------------------------------------------------------------------------
# Flow Stepper Tests
# -----------------------------------------------------------------------------


class TestFlowStepper:
    """Tests for the workflow flow stepper component."""

    def test_flow_stepper_renders_steps(self, jinja_env):
        """Flow stepper shows workflow steps."""
        issue = make_issue(1, "Test Issue")
        session = make_session(issue)
        orchestrator = make_orchestrator(active_sessions=[session])
        vm = build_dashboard_view_model(orchestrator, e2e_status_provider=e2e_disabled)
        soup = render_dashboard(jinja_env, vm)

        stepper = soup.select_one(".flow-stepper")
        assert stepper is not None

        steps = stepper.select(".flow-step")
        assert len(steps) >= 3  # At minimum: Queued, In Progress, Done

    def test_current_step_has_active_class(self, jinja_env):
        """Current workflow step has 'active' class."""
        issue = make_issue(1, "Test Issue")
        session = make_session(issue, task=TaskKind.CODE)
        orchestrator = make_orchestrator(active_sessions=[session])
        vm = build_dashboard_view_model(orchestrator, e2e_status_provider=e2e_disabled)
        soup = render_dashboard(jinja_env, vm)

        active_step = soup.select_one(".flow-step.active")
        assert active_step is not None
        assert "In Progress" in active_step.text

    def test_review_session_shows_review_step_active(self, jinja_env):
        """Review session shows Review step as active."""
        issue = make_issue(1, "Test Issue")
        session = make_session(issue, task=TaskKind.REVIEW)
        orchestrator = make_orchestrator(active_sessions=[session])
        vm = build_dashboard_view_model(orchestrator, e2e_status_provider=e2e_disabled)
        soup = render_dashboard(jinja_env, vm)

        active_step = soup.select_one(".flow-step.active")
        assert active_step is not None
        assert "Review" in active_step.text


# -----------------------------------------------------------------------------
# Blocked Items Tests
# -----------------------------------------------------------------------------


class TestBlockedItems:
    """Tests for blocked issue rendering."""

    def test_blocked_issue_has_blocked_status(self, jinja_env):
        """Blocked issue has blocked status in data attribute."""
        issue = make_issue(1, "Blocked Issue", labels=["agent:web", "blocked"])
        orchestrator = make_orchestrator(queue_issues=[issue])
        vm = build_dashboard_view_model(
            orchestrator, active_tab="blocked", e2e_status_provider=e2e_disabled
        )
        soup = render_dashboard(jinja_env, vm)

        row = soup.select_one(".issue-row")
        assert row is not None
        assert row.get("data-status") == "blocked"

    def test_blocked_badge_shown(self, jinja_env):
        """Blocked badge appears for blocked issues."""
        issue = make_issue(1, "Blocked Issue", labels=["agent:web", "blocked"])
        orchestrator = make_orchestrator(queue_issues=[issue])
        vm = build_dashboard_view_model(
            orchestrator, active_tab="blocked", e2e_status_provider=e2e_disabled
        )
        soup = render_dashboard(jinja_env, vm)

        # Look for the blocked badge inside the issue row's flow stepper, not the tab badge
        row = soup.select_one(".issue-row")
        badge = row.select_one(".flow-stepper .blocked-badge")
        assert badge is not None
        assert "Blocked" in badge.text

    def test_blocked_tab_shows_retry_button(self, jinja_env):
        """Blocked tab shows retry button for issues."""
        issue = make_issue(1, "Blocked Issue", labels=["agent:web", "blocked"])
        orchestrator = make_orchestrator(queue_issues=[issue])
        vm = build_dashboard_view_model(
            orchestrator, active_tab="blocked", e2e_status_provider=e2e_disabled
        )
        soup = render_dashboard(jinja_env, vm)

        retry_btn = soup.select_one(".retry-btn")
        assert retry_btn is not None
        assert "Retry" in retry_btn.text


# -----------------------------------------------------------------------------
# Dependency Tests
# -----------------------------------------------------------------------------


class TestDependencies:
    """Tests for dependency indicator rendering."""

    def test_dependency_icon_shown(self, jinja_env):
        """Dependency chain icon shown for issues with dependencies."""
        issue = make_issue(1, "Dependent Issue", labels=["agent:web"])
        issue.body = "Depends-on: #5"
        orchestrator = make_orchestrator(queue_issues=[issue])
        vm = build_dashboard_view_model(
            orchestrator, active_tab="queue", e2e_status_provider=e2e_disabled
        )
        soup = render_dashboard(jinja_env, vm)

        row = soup.select_one(".issue-row")
        assert row is not None
        assert row.get("data-has-dependencies") == "true"

        dep_icon = soup.select_one(".dep-icon")
        assert dep_icon is not None


# -----------------------------------------------------------------------------
# Empty State Tests
# -----------------------------------------------------------------------------


class TestEmptyStates:
    """Tests for empty state messages."""

    def test_empty_queue_message(self, jinja_env):
        """Empty queue shows appropriate message."""
        orchestrator = make_orchestrator()
        vm = build_dashboard_view_model(
            orchestrator, active_tab="queue", e2e_status_provider=e2e_disabled
        )
        soup = render_dashboard(jinja_env, vm)

        empty = soup.select_one(".empty-state")
        assert empty is not None
        assert "No issues in queue" in empty.text

    def test_empty_history_message(self, jinja_env):
        """Empty history shows appropriate message."""
        orchestrator = make_orchestrator()
        vm = build_dashboard_view_model(
            orchestrator, active_tab="history", e2e_status_provider=e2e_disabled
        )
        soup = render_dashboard(jinja_env, vm)

        empty = soup.select_one(".empty-state")
        assert empty is not None
        assert "No session history" in empty.text

    def test_startup_shows_loading(self, jinja_env):
        """During startup, loading message shown instead of empty state."""
        orchestrator = make_orchestrator(startup_status="pending")
        vm = build_dashboard_view_model(orchestrator, e2e_status_provider=e2e_disabled)
        soup = render_dashboard(jinja_env, vm)

        # Should have a row with starting message, not empty-state
        empty = soup.select_one(".empty-state")
        assert empty is None

        loading_row = soup.select_one(".issue-row")
        assert loading_row is not None
        assert "Starting" in loading_row.text or "up" in loading_row.text


# -----------------------------------------------------------------------------
# Settings Menu Tests
# -----------------------------------------------------------------------------


class TestSettingsMenu:
    """Tests for settings menu rendering."""

    def test_pause_button_shows_pause_when_running(self, jinja_env):
        """Pause button shows 'Pause' when orchestrator is running."""
        orchestrator = make_orchestrator(paused=False)
        vm = build_dashboard_view_model(orchestrator, e2e_status_provider=e2e_disabled)
        soup = render_dashboard(jinja_env, vm)

        pause_item = soup.select_one("#pauseResumeItem")
        assert pause_item is not None
        assert "Pause" in pause_item.text
        assert "Resume" not in pause_item.text

    def test_resume_button_shows_resume_when_paused(self, jinja_env):
        """Pause button shows 'Resume' when orchestrator is paused."""
        orchestrator = make_orchestrator(paused=True)
        vm = build_dashboard_view_model(orchestrator, e2e_status_provider=e2e_disabled)
        soup = render_dashboard(jinja_env, vm)

        pause_item = soup.select_one("#pauseResumeItem")
        assert pause_item is not None
        assert "Resume" in pause_item.text


# -----------------------------------------------------------------------------
# History Tab Tests
# -----------------------------------------------------------------------------


class TestHistoryTab:
    """Tests for history tab rendering."""

    def test_completed_entry_renders(self, jinja_env):
        """Completed history entry renders correctly."""
        entry = SessionHistoryEntry(
            issue_number=100,
            title="Completed Task",
            agent_type="agent:web",
            status="completed",
            runtime_minutes=15,
            pr_url="https://github.com/test/repo/pull/100",
        )
        orchestrator = make_orchestrator(session_history=[entry])
        vm = build_dashboard_view_model(
            orchestrator, active_tab="history", e2e_status_provider=e2e_disabled
        )
        soup = render_dashboard(jinja_env, vm)

        row = soup.select_one(".issue-row")
        assert row is not None
        assert row.get("data-issue") == "100"
        assert "Completed Task" in row.select_one(".issue-title").text

    def test_failed_entry_shows_in_blocked_tab(self, jinja_env):
        """Failed history entry appears in blocked tab."""
        entry = SessionHistoryEntry(
            issue_number=100,
            title="Failed Task",
            agent_type="agent:web",
            status="needs_human",
            runtime_minutes=10,
        )
        orchestrator = make_orchestrator(session_history=[entry])
        vm = build_dashboard_view_model(
            orchestrator, active_tab="blocked", e2e_status_provider=e2e_disabled
        )
        soup = render_dashboard(jinja_env, vm)

        row = soup.select_one(".issue-row")
        assert row is not None
        assert row.get("data-issue") == "100"


# -----------------------------------------------------------------------------
# E2E Tab Tests
# -----------------------------------------------------------------------------


class TestE2ETab:
    """Tests for E2E tab rendering."""

    def test_e2e_controls_shown_on_e2e_tab(self, jinja_env):
        """E2E controls shown when on E2E tab."""
        orchestrator = make_orchestrator()

        def e2e_enabled(_config):
            return {"enabled": True, "running": False, "last_run": None, "next_run": None}

        vm = build_dashboard_view_model(
            orchestrator, active_tab="e2e", e2e_status_provider=e2e_enabled
        )
        soup = render_dashboard(jinja_env, vm)

        controls = soup.select_one("#e2eControls")
        assert controls is not None

        start_btn = soup.select_one("#e2eStartBtn")
        assert start_btn is not None
        assert "Start E2E" in start_btn.text

    def test_e2e_stop_button_when_running(self, jinja_env):
        """Stop button shown when E2E is running."""
        orchestrator = make_orchestrator()

        def e2e_running(_config):
            return {"enabled": True, "running": True, "last_run": None}

        vm = build_dashboard_view_model(
            orchestrator, active_tab="e2e", e2e_status_provider=e2e_running
        )
        soup = render_dashboard(jinja_env, vm)

        stop_btn = soup.select_one("#e2eStopBtn")
        assert stop_btn is not None
        assert "Stop E2E" in stop_btn.text

    def test_e2e_header_badge_shows_status(self, jinja_env):
        """E2E header badge shows current status."""
        orchestrator = make_orchestrator()

        def e2e_passed(_config):
            return {
                "enabled": True,
                "running": False,
                "last_run": {"status": "passed"},
            }

        vm = build_dashboard_view_model(orchestrator, e2e_status_provider=e2e_passed)
        soup = render_dashboard(jinja_env, vm)

        badge = soup.select_one("#e2eHeaderBadge")
        assert badge is not None
        assert "passed" in badge.get("class", [])
        assert "✓" in badge.text

    def test_e2e_failed_badge(self, jinja_env):
        """E2E header badge shows failed status."""
        orchestrator = make_orchestrator()

        def e2e_failed(_config):
            return {
                "enabled": True,
                "running": False,
                "last_run": {"status": "failed"},
            }

        vm = build_dashboard_view_model(orchestrator, e2e_status_provider=e2e_failed)
        soup = render_dashboard(jinja_env, vm)

        badge = soup.select_one("#e2eHeaderBadge")
        assert badge is not None
        assert "failed" in badge.get("class", [])
        assert "✗" in badge.text


# -----------------------------------------------------------------------------
# Accessibility Tests
# -----------------------------------------------------------------------------


class TestAccessibility:
    """Tests for accessibility attributes."""

    def test_tabs_have_role_attributes(self, jinja_env):
        """Tabs have proper ARIA roles."""
        orchestrator = make_orchestrator()
        vm = build_dashboard_view_model(orchestrator, e2e_status_provider=e2e_disabled)
        soup = render_dashboard(jinja_env, vm)

        tab_bar = soup.select_one(".tab-bar")
        assert tab_bar is not None
        assert tab_bar.get("role") == "tablist"

        tabs = soup.select(".tab")
        for tab in tabs:
            assert tab.get("role") == "tab"

    def test_active_tab_has_aria_selected(self, jinja_env):
        """Active tab has aria-selected=true."""
        orchestrator = make_orchestrator()
        vm = build_dashboard_view_model(
            orchestrator, active_tab="queue", e2e_status_provider=e2e_disabled
        )
        soup = render_dashboard(jinja_env, vm)

        queue_tab = soup.select_one("#tab-queue")
        assert queue_tab.get("aria-selected") == "true"

        active_tab = soup.select_one("#tab-active")
        assert active_tab.get("aria-selected") == "false"

    def test_issue_rows_have_aria_labels(self, jinja_env):
        """Issue rows have descriptive aria-labels."""
        issue = make_issue(123, "Fix the bug")
        session = make_session(issue)
        orchestrator = make_orchestrator(active_sessions=[session])
        vm = build_dashboard_view_model(orchestrator, e2e_status_provider=e2e_disabled)
        soup = render_dashboard(jinja_env, vm)

        row = soup.select_one(".issue-row")
        aria_label = row.get("aria-label", "")
        assert "#123" in aria_label
        assert "Fix the bug" in aria_label

    def test_skip_link_present(self, jinja_env):
        """Skip to content link is present."""
        orchestrator = make_orchestrator()
        vm = build_dashboard_view_model(orchestrator, e2e_status_provider=e2e_disabled)
        soup = render_dashboard(jinja_env, vm)

        skip_link = soup.select_one(".skip-link")
        assert skip_link is not None
        assert skip_link.get("href") == "#issueList"


# -----------------------------------------------------------------------------
# Data Attribute Tests
# -----------------------------------------------------------------------------


class TestDataAttributes:
    """Tests for data attributes used by JavaScript."""

    def test_issue_row_has_required_data_attributes(self, jinja_env):
        """Issue rows have all required data attributes for JS."""
        issue = make_issue(42, "Test Issue")
        session = make_session(issue)
        orchestrator = make_orchestrator(active_sessions=[session])
        vm = build_dashboard_view_model(orchestrator, e2e_status_provider=e2e_disabled)
        soup = render_dashboard(jinja_env, vm)

        row = soup.select_one(".issue-row")

        # Required data attributes
        assert row.get("data-issue") == "42"
        assert row.get("data-status") == "active"
        assert row.get("data-title") == "Test Issue"
        assert row.get("data-action") is not None
        assert row.get("data-has-terminal") == "true"
        assert row.get("data-worktree-path") is not None

    def test_queue_issue_has_url_data(self, jinja_env):
        """Queued issue has URL data attributes."""
        issue = make_issue(99, "Queue Issue")
        orchestrator = make_orchestrator(queue_issues=[issue])
        vm = build_dashboard_view_model(
            orchestrator, active_tab="queue", e2e_status_provider=e2e_disabled
        )
        soup = render_dashboard(jinja_env, vm)

        row = soup.select_one(".issue-row")
        assert row.get("data-issue-url") == "https://github.com/test/repo/issues/99"


# -----------------------------------------------------------------------------
# List Population Tests
# -----------------------------------------------------------------------------


class TestListPopulation:
    """Tests that verify lists render ALL items with correct distinct data."""

    def test_multiple_active_sessions_all_render(self, jinja_env):
        """All active sessions render with correct distinct data."""
        issues = [
            make_issue(101, "First active task"),
            make_issue(202, "Second active task"),
            make_issue(303, "Third active task"),
        ]
        sessions = [make_session(issue) for issue in issues]
        orchestrator = make_orchestrator(active_sessions=sessions)
        vm = build_dashboard_view_model(
            orchestrator, active_tab="active", e2e_status_provider=e2e_disabled
        )
        soup = render_dashboard(jinja_env, vm)

        rows = soup.select(".issue-row")
        assert len(rows) == 3, f"Expected 3 rows, got {len(rows)}"

        # Verify each row has distinct, correct data
        issue_numbers = [row.get("data-issue") for row in rows]
        assert "101" in issue_numbers
        assert "202" in issue_numbers
        assert "303" in issue_numbers

        # Verify titles match their issues
        for row in rows:
            issue_num = row.get("data-issue")
            title_elem = row.select_one(".issue-title")
            if issue_num == "101":
                assert "First active task" in title_elem.text
            elif issue_num == "202":
                assert "Second active task" in title_elem.text
            elif issue_num == "303":
                assert "Third active task" in title_elem.text

    def test_multiple_queue_issues_all_render(self, jinja_env):
        """All queued issues render with correct distinct data."""
        issues = [
            make_issue(10, "Queue item A"),
            make_issue(20, "Queue item B"),
            make_issue(30, "Queue item C"),
            make_issue(40, "Queue item D"),
        ]
        orchestrator = make_orchestrator(queue_issues=issues)
        vm = build_dashboard_view_model(
            orchestrator, active_tab="queue", e2e_status_provider=e2e_disabled
        )
        soup = render_dashboard(jinja_env, vm)

        rows = soup.select(".issue-row")
        assert len(rows) == 4, f"Expected 4 rows, got {len(rows)}"

        # Collect all rendered issue numbers
        rendered_numbers = {row.get("data-issue") for row in rows}
        assert rendered_numbers == {"10", "20", "30", "40"}

        # Verify each title is present and correct
        titles = [row.select_one(".issue-title").text.strip() for row in rows]
        assert "Queue item A" in titles
        assert "Queue item B" in titles
        assert "Queue item C" in titles
        assert "Queue item D" in titles

    def test_multiple_history_entries_all_render(self, jinja_env):
        """All completed history entries render with correct distinct data.

        Note: needs_human/blocked entries go to blocked tab, not history.
        """
        entries = [
            SessionHistoryEntry(
                issue_number=1001,
                title="Completed yesterday",
                agent_type="agent:web",
                status="completed",
                runtime_minutes=10,
            ),
            SessionHistoryEntry(
                issue_number=1002,
                title="Completed today",
                agent_type="agent:web",
                status="completed",
                runtime_minutes=20,
            ),
            SessionHistoryEntry(
                issue_number=1003,
                title="Also completed",
                agent_type="agent:web",
                status="completed",
                runtime_minutes=5,
            ),
        ]
        orchestrator = make_orchestrator(session_history=entries)
        vm = build_dashboard_view_model(
            orchestrator, active_tab="history", e2e_status_provider=e2e_disabled
        )
        soup = render_dashboard(jinja_env, vm)

        rows = soup.select(".issue-row")
        assert len(rows) == 3, f"Expected 3 history rows, got {len(rows)}"

        rendered_numbers = {row.get("data-issue") for row in rows}
        assert rendered_numbers == {"1001", "1002", "1003"}

    def test_multiple_blocked_issues_all_render(self, jinja_env):
        """All blocked issues render in blocked tab."""
        issues = [
            make_issue(501, "Blocked issue 1", labels=["agent:web", "blocked"]),
            make_issue(502, "Blocked issue 2", labels=["agent:web", "blocked"]),
        ]
        orchestrator = make_orchestrator(queue_issues=issues)
        vm = build_dashboard_view_model(
            orchestrator, active_tab="blocked", e2e_status_provider=e2e_disabled
        )
        soup = render_dashboard(jinja_env, vm)

        rows = soup.select(".issue-row")
        assert len(rows) == 2, f"Expected 2 blocked rows, got {len(rows)}"

        rendered_numbers = {row.get("data-issue") for row in rows}
        assert rendered_numbers == {"501", "502"}

        # Verify all have blocked status
        for row in rows:
            assert row.get("data-status") == "blocked"

    def test_flow_stepper_all_steps_present(self, jinja_env):
        """Flow stepper renders all expected workflow steps."""
        issue = make_issue(1, "Test Issue")
        session = make_session(issue)
        orchestrator = make_orchestrator(active_sessions=[session])
        vm = build_dashboard_view_model(orchestrator, e2e_status_provider=e2e_disabled)
        soup = render_dashboard(jinja_env, vm)

        stepper = soup.select_one(".flow-stepper")
        steps = stepper.select(".flow-step")

        # Extract step labels
        step_labels = [step.text.strip() for step in steps]

        # Verify expected workflow steps are present
        assert "Queued" in step_labels
        assert "In Progress" in step_labels
        assert "Review" in step_labels
        assert "Done" in step_labels

        # Verify steps are in logical order
        queued_idx = step_labels.index("Queued")
        progress_idx = step_labels.index("In Progress")
        review_idx = step_labels.index("Review")
        done_idx = step_labels.index("Done")
        assert queued_idx < progress_idx < review_idx < done_idx

    def test_tab_badges_reflect_actual_counts(self, jinja_env):
        """Tab badges show accurate counts for multiple items."""
        # 3 active sessions
        active_issues = [make_issue(i, f"Active {i}") for i in range(1, 4)]
        active_sessions = [make_session(issue) for issue in active_issues]

        # 5 queue issues
        queue_issues = [make_issue(i, f"Queue {i}") for i in range(10, 15)]

        # 2 blocked issues
        blocked_issues = [
            make_issue(i, f"Blocked {i}", labels=["agent:web", "blocked"])
            for i in range(20, 22)
        ]

        orchestrator = make_orchestrator(
            active_sessions=active_sessions,
            queue_issues=queue_issues + blocked_issues,
        )
        vm = build_dashboard_view_model(orchestrator, e2e_status_provider=e2e_disabled)
        soup = render_dashboard(jinja_env, vm)

        # Check active badge shows 3
        active_badge = soup.select_one("#tab-active .tab-badge")
        assert active_badge is not None
        assert "3" in active_badge.text

        # Check queue badge shows 5 (non-blocked only)
        queue_badge = soup.select_one("#tab-queue .tab-badge")
        assert queue_badge is not None
        assert "5" in queue_badge.text

        # Check blocked badge shows 2
        blocked_badge = soup.select_one("#tab-blocked .tab-badge")
        assert blocked_badge is not None
        assert "2" in blocked_badge.text

    def test_needs_human_entries_go_to_blocked_tab(self, jinja_env):
        """Entries with needs_human status appear in blocked tab, not history."""
        entries = [
            SessionHistoryEntry(
                issue_number=801,
                title="Completed task",
                agent_type="agent:web",
                status="completed",
                runtime_minutes=10,
            ),
            SessionHistoryEntry(
                issue_number=802,
                title="Needs human help",
                agent_type="agent:web",
                status="needs_human",
                runtime_minutes=15,
            ),
            SessionHistoryEntry(
                issue_number=803,
                title="Blocked by something",
                agent_type="agent:web",
                status="blocked",
                runtime_minutes=5,
            ),
        ]
        orchestrator = make_orchestrator(session_history=entries)

        # History tab should only have completed entry
        vm_history = build_dashboard_view_model(
            orchestrator, active_tab="history", e2e_status_provider=e2e_disabled
        )
        soup_history = render_dashboard(jinja_env, vm_history)
        history_rows = soup_history.select(".issue-row")
        history_numbers = {row.get("data-issue") for row in history_rows}
        assert history_numbers == {"801"}, "History should only show completed entries"

        # Blocked tab should have needs_human and blocked entries
        vm_blocked = build_dashboard_view_model(
            orchestrator, active_tab="blocked", e2e_status_provider=e2e_disabled
        )
        soup_blocked = render_dashboard(jinja_env, vm_blocked)
        blocked_rows = soup_blocked.select(".issue-row")
        blocked_numbers = {row.get("data-issue") for row in blocked_rows}
        assert "802" in blocked_numbers, "needs_human should appear in blocked tab"
        assert "803" in blocked_numbers, "blocked should appear in blocked tab"

    def test_mixed_tabs_correct_separation(self, jinja_env):
        """Items appear in correct tabs, not duplicated across tabs."""
        # Create items for different tabs
        active_issue = make_issue(1, "Active Issue")
        active_session = make_session(active_issue)

        queue_issues = [make_issue(2, "Queued"), make_issue(3, "Also Queued")]
        blocked_issue = make_issue(4, "Blocked", labels=["agent:web", "blocked"])

        orchestrator = make_orchestrator(
            active_sessions=[active_session],
            queue_issues=queue_issues + [blocked_issue],
        )

        # Check active tab
        vm_active = build_dashboard_view_model(
            orchestrator, active_tab="active", e2e_status_provider=e2e_disabled
        )
        soup_active = render_dashboard(jinja_env, vm_active)
        active_rows = soup_active.select(".issue-row")
        active_numbers = {row.get("data-issue") for row in active_rows}
        assert active_numbers == {"1"}, "Active tab should only show active session"

        # Check queue tab
        vm_queue = build_dashboard_view_model(
            orchestrator, active_tab="queue", e2e_status_provider=e2e_disabled
        )
        soup_queue = render_dashboard(jinja_env, vm_queue)
        queue_rows = soup_queue.select(".issue-row")
        queue_numbers = {row.get("data-issue") for row in queue_rows}
        assert queue_numbers == {"2", "3"}, "Queue tab should only show non-blocked queue issues"

        # Check blocked tab
        vm_blocked = build_dashboard_view_model(
            orchestrator, active_tab="blocked", e2e_status_provider=e2e_disabled
        )
        soup_blocked = render_dashboard(jinja_env, vm_blocked)
        blocked_rows = soup_blocked.select(".issue-row")
        blocked_numbers = {row.get("data-issue") for row in blocked_rows}
        assert "4" in blocked_numbers, "Blocked tab should show blocked issue"


# -----------------------------------------------------------------------------
# Settings Template Tests
# -----------------------------------------------------------------------------


def render_settings(jinja_env: Environment, tabs, schemas, values) -> BeautifulSoup:
    """Render settings template and return parsed HTML."""
    import json

    template = jinja_env.get_template("settings.html")
    tabs_json = json.dumps([{"key": t["key"], "label": t["label"]} for t in tabs])
    schemas_json = json.dumps(schemas)
    html = template.render(
        tabs=tabs,
        schemas=schemas,
        values=values,
        tabs_json=tabs_json,
        schemas_json=schemas_json,
    )
    return BeautifulSoup(html, "lxml")


class TestSettingsTemplate:
    """Tests for settings.html template rendering."""

    @pytest.fixture
    def sample_tabs(self):
        """Sample tab definitions for testing."""
        return [
            {"key": "concurrency", "label": "Concurrency"},
            {"key": "filtering", "label": "Filtering"},
            {"key": "advanced", "label": "Advanced"},
        ]

    @pytest.fixture
    def sample_schemas(self):
        """Sample JSON schemas for each tab."""
        return {
            "concurrency": {
                "properties": {
                    "max_concurrent_sessions": {
                        "type": "integer",
                        "title": "Max Concurrent Sessions",
                        "description": "Maximum parallel agent sessions",
                        "minimum": 1,
                        "maximum": 20,
                    },
                    "session_timeout_minutes": {
                        "type": "integer",
                        "title": "Session Timeout (minutes)",
                        "description": "Kill sessions after this duration",
                    },
                }
            },
            "filtering": {
                "properties": {
                    "filter_label": {
                        "type": "string",
                        "title": "Filter Label",
                        "description": "Only process issues with this label",
                    },
                    "auto_queue_enabled": {
                        "type": "boolean",
                        "title": "Auto Queue Enabled",
                        "description": "Automatically queue matching issues",
                    },
                }
            },
            "advanced": {
                "properties": {
                    "terminal_adapter": {
                        "type": "string",
                        "title": "Terminal Adapter",
                        "enum": ["subprocess", "tmux", "iterm2"],
                    },
                }
            },
        }

    @pytest.fixture
    def sample_values(self):
        """Sample values for each tab."""
        return {
            "concurrency": {
                "max_concurrent_sessions": 3,
                "session_timeout_minutes": 45,
            },
            "filtering": {
                "filter_label": "ready",
                "auto_queue_enabled": True,
            },
            "advanced": {
                "terminal_adapter": "subprocess",
            },
        }

    def test_all_tabs_render(self, jinja_env, sample_tabs, sample_schemas, sample_values):
        """All tabs render in the tab bar."""
        soup = render_settings(jinja_env, sample_tabs, sample_schemas, sample_values)

        tabs = soup.select(".tab-bar .tab")
        assert len(tabs) == 3

        tab_labels = [tab.text.strip() for tab in tabs]
        assert "Concurrency" in tab_labels
        assert "Filtering" in tab_labels
        assert "Advanced" in tab_labels

    def test_first_tab_is_active(self, jinja_env, sample_tabs, sample_schemas, sample_values):
        """First tab is active by default."""
        soup = render_settings(jinja_env, sample_tabs, sample_schemas, sample_values)

        tabs = soup.select(".tab-bar .tab")
        assert "active" in tabs[0].get("class", [])
        assert "active" not in tabs[1].get("class", [])

    def test_tab_content_panels_exist(self, jinja_env, sample_tabs, sample_schemas, sample_values):
        """Each tab has a content panel."""
        soup = render_settings(jinja_env, sample_tabs, sample_schemas, sample_values)

        for tab in sample_tabs:
            panel = soup.select_one(f"#tab-{tab['key']}")
            assert panel is not None, f"Missing panel for tab {tab['key']}"

    def test_integer_field_renders_as_number_input(
        self, jinja_env, sample_tabs, sample_schemas, sample_values
    ):
        """Integer fields render as number inputs with correct value."""
        soup = render_settings(jinja_env, sample_tabs, sample_schemas, sample_values)

        input_field = soup.select_one("#concurrency__max_concurrent_sessions")
        assert input_field is not None
        assert input_field.get("type") == "number"
        assert input_field.get("value") == "3"
        assert input_field.get("data-type") == "integer"

    def test_string_field_renders_as_text_input(
        self, jinja_env, sample_tabs, sample_schemas, sample_values
    ):
        """String fields render as text inputs."""
        soup = render_settings(jinja_env, sample_tabs, sample_schemas, sample_values)

        input_field = soup.select_one("#filtering__filter_label")
        assert input_field is not None
        assert input_field.get("type") == "text"
        assert input_field.get("value") == "ready"

    def test_boolean_field_renders_as_checkbox(
        self, jinja_env, sample_tabs, sample_schemas, sample_values
    ):
        """Boolean fields render as checkboxes."""
        soup = render_settings(jinja_env, sample_tabs, sample_schemas, sample_values)

        checkbox = soup.select_one("#filtering__auto_queue_enabled")
        assert checkbox is not None
        assert checkbox.get("type") == "checkbox"
        assert checkbox.has_attr("checked")

    def test_enum_field_renders_as_select(
        self, jinja_env, sample_tabs, sample_schemas, sample_values
    ):
        """Enum fields render as select dropdowns."""
        soup = render_settings(jinja_env, sample_tabs, sample_schemas, sample_values)

        select = soup.select_one("#advanced__terminal_adapter")
        assert select is not None
        assert select.name == "select"

        options = select.select("option")
        assert len(options) == 3
        option_values = [opt.get("value") for opt in options]
        assert "subprocess" in option_values
        assert "tmux" in option_values
        assert "iterm2" in option_values

    def test_selected_enum_option(
        self, jinja_env, sample_tabs, sample_schemas, sample_values
    ):
        """Correct enum option is selected."""
        soup = render_settings(jinja_env, sample_tabs, sample_schemas, sample_values)

        select = soup.select_one("#advanced__terminal_adapter")
        selected = select.select_one("option[selected]")
        assert selected is not None
        assert selected.get("value") == "subprocess"

    def test_field_descriptions_render_as_hints(
        self, jinja_env, sample_tabs, sample_schemas, sample_values
    ):
        """Field descriptions render as hint text."""
        soup = render_settings(jinja_env, sample_tabs, sample_schemas, sample_values)

        # Find the form group containing max_concurrent_sessions
        form_groups = soup.select(".form-group")
        found_hint = False
        for group in form_groups:
            if group.select_one("#concurrency__max_concurrent_sessions"):
                hint = group.select_one(".hint")
                if hint and "Maximum parallel agent sessions" in hint.text:
                    found_hint = True
                    break
        assert found_hint, "Description should render as hint"

    def test_save_button_exists(self, jinja_env, sample_tabs, sample_schemas, sample_values):
        """Save button exists and is initially disabled."""
        soup = render_settings(jinja_env, sample_tabs, sample_schemas, sample_values)

        save_btn = soup.select_one("#saveBtn")
        assert save_btn is not None
        assert save_btn.has_attr("disabled")
        assert "Save" in save_btn.text

    def test_reset_button_exists(self, jinja_env, sample_tabs, sample_schemas, sample_values):
        """Reset button exists."""
        soup = render_settings(jinja_env, sample_tabs, sample_schemas, sample_values)

        reset_btn = soup.select_one("button[onclick='resetSettings()']")
        assert reset_btn is not None
        assert "Reset" in reset_btn.text

    def test_back_link_to_dashboard(self, jinja_env, sample_tabs, sample_schemas, sample_values):
        """Back link to dashboard exists."""
        soup = render_settings(jinja_env, sample_tabs, sample_schemas, sample_values)

        back_link = soup.select_one(".back-link")
        assert back_link is not None
        assert back_link.get("href") == "/"
        assert "Dashboard" in back_link.text

    def test_advanced_tab_has_doctor_section(
        self, jinja_env, sample_tabs, sample_schemas, sample_values
    ):
        """Advanced tab includes doctor validation section."""
        soup = render_settings(jinja_env, sample_tabs, sample_schemas, sample_values)

        advanced_panel = soup.select_one("#tab-advanced")
        doctor_btn = advanced_panel.select_one("button[onclick='runDoctor()']")
        assert doctor_btn is not None
        assert "Doctor" in doctor_btn.text

    def test_multiple_fields_all_render(
        self, jinja_env, sample_tabs, sample_schemas, sample_values
    ):
        """All fields in each tab render correctly."""
        soup = render_settings(jinja_env, sample_tabs, sample_schemas, sample_values)

        # Concurrency tab should have 2 fields
        concurrency_fields = soup.select("[data-tab='concurrency']")
        assert len(concurrency_fields) == 2

        # Filtering tab should have 2 fields
        filtering_fields = soup.select("[data-tab='filtering']")
        assert len(filtering_fields) == 2

        # Advanced tab should have 1 field
        advanced_fields = soup.select("[data-tab='advanced']")
        assert len(advanced_fields) == 1


# -----------------------------------------------------------------------------
# Dialog View Model Tests
# -----------------------------------------------------------------------------


class TestAgentsDropdown:
    """Tests for agent dropdown lists in dashboard."""

    def test_agents_dropdown_renders_all_agents(self, jinja_env):
        """Agent dropdown shows all configured agents."""
        orchestrator = make_orchestrator()
        # Add multiple agents to config
        orchestrator.config.agents = {
            "agent:web": make_agent_config(),
            "agent:backend": make_agent_config(),
            "agent:mobile": make_agent_config(),
        }
        vm = build_dashboard_view_model(orchestrator, e2e_status_provider=e2e_disabled)
        soup = render_dashboard(jinja_env, vm)

        # Find agent select dropdown
        agent_select = soup.select_one("#issueAgent")
        assert agent_select is not None

        options = agent_select.select("option")
        # First option is placeholder "Select an agent..."
        agent_options = [opt.get("value") for opt in options if opt.get("value")]

        assert "agent:web" in agent_options
        assert "agent:backend" in agent_options
        assert "agent:mobile" in agent_options
        assert len(agent_options) == 3


def render_issue_row(jinja_env: Environment, issue_data: dict, **kwargs) -> BeautifulSoup:
    """Render issue_row template directly with issue data."""
    template = jinja_env.get_template("issue_row.html")
    context = {"issue": issue_data, "active_tab": "e2e", "github_owner": "test", "github_repo": "repo"}
    context.update(kwargs)
    html = template.render(**context)
    return BeautifulSoup(html, "lxml")


class TestE2ESubIssues:
    """Tests for E2E sub-issues and failed tests lists."""

    def test_e2e_sub_issues_all_render(self, jinja_env):
        """All E2E sub-issues render with correct data."""
        issue_data = {
            "issue_number": 100,
            "title": "E2E Run with failures",
            "status": "needs_attention",
            "is_e2e": True,
            "e2e_sub_issues": [
                {"nodeid": "test_one.py::test_a", "short_name": "test_a", "resolved": True, "issue_number": 101},
                {"nodeid": "test_one.py::test_b", "short_name": "test_b", "resolved": False, "issue_number": None},
                {"nodeid": "test_two.py::test_c", "short_name": "test_c", "resolved": False, "issue_number": 102},
            ],
            "e2e_progress": {"percent": 33, "resolved": 1, "total": 3},
            "flow_steps": None,
            "has_dependencies": False,
            "has_terminal": False,
        }

        soup = render_issue_row(jinja_env, issue_data)

        sub_items = soup.select(".sub-issue-item")
        assert len(sub_items) == 3, f"Expected 3 sub-issues, got {len(sub_items)}"

        # Check that resolved/failed icons are correct
        resolved_icons = soup.select(".status-icon.resolved")
        failed_icons = soup.select(".status-icon.failed")
        assert len(resolved_icons) == 1, "Should have 1 resolved item"
        assert len(failed_icons) == 2, "Should have 2 failed items"

        # Check nodeids are rendered
        nodeids = soup.select(".sub-issue-nodeid")
        nodeid_texts = [n.text.strip() for n in nodeids]
        assert "test_a" in nodeid_texts
        assert "test_b" in nodeid_texts
        assert "test_c" in nodeid_texts

    def test_e2e_failed_tests_all_render(self, jinja_env):
        """All E2E failed tests render with correct data."""
        issue_data = {
            "issue_number": 200,
            "title": "E2E Run needs triage",
            "status": "needs_attention",
            "is_e2e": True,
            "e2e_failed_tests": [
                {"nodeid": "test_a.py::test_fail1", "short_name": "test_fail1", "outcome": "failed", "duration": 1.5},
                {"nodeid": "test_a.py::test_fail2", "short_name": "test_fail2", "outcome": "error", "duration": 0.3},
                {"nodeid": "test_b.py::test_fail3", "short_name": "test_fail3", "outcome": "failed", "duration": 2.1},
            ],
            "detail_label": "3 failures",
            "flow_steps": None,
            "has_dependencies": False,
            "has_terminal": False,
        }

        soup = render_issue_row(jinja_env, issue_data)

        sub_items = soup.select(".sub-issue-item")
        # 3 test items + 1 "click row to view" hint
        assert len(sub_items) >= 3, f"Expected at least 3 sub-items, got {len(sub_items)}"

        # Check outcome badges
        outcome_badges = soup.select(".outcome-badge")
        assert len(outcome_badges) == 3

        badge_texts = [b.text.strip() for b in outcome_badges]
        assert "failed" in badge_texts
        assert "error" in badge_texts

        # Check duration badges
        duration_badges = soup.select(".duration-badge")
        assert len(duration_badges) == 3

    def test_e2e_sub_issues_shows_linked_issue_numbers(self, jinja_env):
        """Sub-issues with linked GitHub issues show issue links."""
        issue_data = {
            "issue_number": 300,
            "title": "E2E with linked issues",
            "status": "in_progress",
            "is_e2e": True,
            "e2e_sub_issues": [
                {"nodeid": "test.py::test_linked", "short_name": "test_linked", "resolved": False, "issue_number": 456},
                {"nodeid": "test.py::test_unlinked", "short_name": "test_unlinked", "resolved": False, "issue_number": None},
            ],
            "e2e_progress": {"percent": 0, "resolved": 0, "total": 2},
            "flow_steps": None,
            "has_dependencies": False,
            "has_terminal": False,
        }

        soup = render_issue_row(jinja_env, issue_data)

        # Should have one link to issue #456
        issue_links = soup.select(".sub-issue-link")
        assert len(issue_links) == 1
        assert "#456" in issue_links[0].text


class TestDialogViewModels:
    """Tests for dialog view model builder functions."""

    def test_build_info_dialog_all_fields(self):
        """Info dialog includes all expected fields."""
        from issue_orchestrator.view_models.dialogs import build_info_dialog

        info = {
            "version": "1.2.3",
            "repo": "owner/repo",
            "ui_mode": "web",
            "terminal_backend": "tmux",
            "commit_short": "abc1234",
            "max_sessions": 5,
            "active_sessions": 2,
            "completed_today": 10,
        }

        result = build_info_dialog(info)

        assert result["title"] == "About Issue Orchestrator"
        assert len(result["rows"]) == 8

        labels = [row["label"] for row in result["rows"]]
        assert "Version" in labels
        assert "Repository" in labels
        assert "Max Sessions" in labels

        # Verify values
        version_row = next(r for r in result["rows"] if r["label"] == "Version")
        assert version_row["value"] == "1.2.3"

    def test_build_info_dialog_handles_missing_fields(self):
        """Info dialog handles missing data gracefully."""
        from issue_orchestrator.view_models.dialogs import build_info_dialog

        result = build_info_dialog({})

        assert result["title"] == "About Issue Orchestrator"
        # Should still have rows with defaults/empty values
        version_row = next(r for r in result["rows"] if r["label"] == "Version")
        assert version_row["value"] == "dev"

    def test_build_config_dialog(self):
        """Config dialog includes config text."""
        from issue_orchestrator.view_models.dialogs import build_config_dialog

        result = build_config_dialog("repo: owner/repo\nmax_sessions: 3")

        assert result["title"] == "Configuration"
        assert "repo: owner/repo" in result["config_text"]

    def test_build_debug_dialog_all_sections(self):
        """Debug dialog includes all sections."""
        from issue_orchestrator.view_models.dialogs import build_debug_dialog

        debug_data = {
            "startup_options": {
                "ui_mode": "web",
                "web_port": 8080,
                "test_mode": False,
                "filtering": {"label": "ready", "milestone": "v1"},
                "max_sessions": 3,
            },
            "paused": False,
            "priority_queue": [1, 2, 3],
            "config_path": "/path/to/config.yaml",
            "repo_root": "/path/to/repo",
            "agents": {"agent:web": {"timeout": 45}},
        }

        result = build_debug_dialog(debug_data)

        assert result["title"] == "Debug Info"
        assert len(result["sections"]) == 4  # Startup, State, Paths, Agents

        section_titles = [s["title"] for s in result["sections"]]
        assert "Startup Options" in section_titles
        assert "State" in section_titles
        assert "Paths" in section_titles
        assert "Agent Types" in section_titles

    def test_build_doctor_dialog(self):
        """Doctor dialog includes check results."""
        from issue_orchestrator.view_models.dialogs import build_doctor_dialog

        doctor_data = {
            "overall": "ok",
            "checks": [
                {"name": "Git", "status": "ok", "detail": "Git available"},
                {"name": "Claude", "status": "error", "detail": "Claude not found"},
            ],
        }

        result = build_doctor_dialog(doctor_data)

        assert result["title"] == "Doctor"
        assert result["overall"] == "ok"
        assert len(result["checks"]) == 2
        assert result["checks"][0]["name"] == "Git"
        assert result["checks"][1]["status"] == "error"

    def test_build_session_diagnostics_dialog(self):
        """Session diagnostics dialog includes session info and actions."""
        from issue_orchestrator.view_models.dialogs import build_session_diagnostics_dialog

        manifest = {
            "manifest": {
                "session_name": "issue-123",
                "started_at": "2024-01-01T10:00:00",
                "run_id": "abc123",
                "backend": "tmux",
                "agent_label": "agent:web",
                "claude_session_id": "sess_123",
                "worktree": "/tmp/worktree-123",
            },
            "run_dir": "/tmp/run-123",
        }

        result = build_session_diagnostics_dialog(123, manifest)

        assert result["title"] == "Session Diagnostics #123"
        assert len(result["rows"]) == 7

        labels = [r["label"] for r in result["rows"]]
        assert "Session" in labels
        assert "Backend" in labels
        assert "Worktree" in labels

        # Should have actions
        assert len(result["actions"]) >= 2
        action_types = [a["type"] for a in result["actions"]]
        assert "open_path" in action_types or "open_agent_log" in action_types

    def test_build_blocked_issues_dialog(self):
        """Blocked issues dialog includes issue list."""
        from issue_orchestrator.view_models.dialogs import build_blocked_issues_dialog

        payload = {
            "blocked_issues": [
                {"number": 1, "title": "Issue 1"},
                {"number": 2, "title": "Issue 2"},
            ]
        }

        result = build_blocked_issues_dialog(payload)

        assert result["title"] == "Blocked Issues"
        assert len(result["blocked_issues"]) == 2

    def test_build_phase_dialog(self):
        """Phase dialog includes phase info."""
        from issue_orchestrator.view_models.dialogs import build_phase_dialog

        phases_payload = {
            "phases": [
                {"name": "coding-1", "display_name": "Coding Session 1"},
                {"name": "review-1", "display_name": "Code Review 1"},
            ]
        }

        result = build_phase_dialog(phases_payload, 123, "in_progress")

        assert result["issue_number"] == 123
        assert result["phase"] is not None
        assert len(result["phases"]) == 2

    def test_build_phase_dialog_selects_correct_phase(self):
        """Phase dialog selects the appropriate phase based on key."""
        from issue_orchestrator.view_models.dialogs import build_phase_dialog

        phases_payload = {
            "phases": [
                {"name": "coding-1", "display_name": "Coding Session 1"},
                {"name": "review-1", "display_name": "Code Review 1"},
            ]
        }

        # in_progress should select coding phase
        result = build_phase_dialog(phases_payload, 123, "in_progress")
        assert result["phase"]["name"] == "coding-1"

        # review should select review phase
        result = build_phase_dialog(phases_payload, 123, "review")
        assert result["phase"]["name"] == "review-1"
