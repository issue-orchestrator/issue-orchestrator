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
    PendingReview,
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
    return BeautifulSoup(html, "html.parser")


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
    pending_reviews: list[PendingReview] | None = None,
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
        pending_reviews=pending_reviews or [],
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
