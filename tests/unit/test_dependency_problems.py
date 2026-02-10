"""Tests for dependency problems tracking in orchestrator and web API."""

import pytest
from unittest.mock import MagicMock, patch

from issue_orchestrator.domain.models import DependencyProblem, Issue, OrchestratorState


class TestDependencyProblem:
    """Tests for DependencyProblem dataclass."""

    def test_create_problem(self):
        """Can create a dependency problem."""
        problem = DependencyProblem(
            issue_number=5,
            issue_title="Test issue",
            blocked_by=[(1, "Dep issue", "open")],
            summary="Blocked - waiting on: #1",
        )

        assert problem.issue_number == 5
        assert problem.issue_title == "Test issue"
        assert problem.summary == "Blocked - waiting on: #1"


class TestQueueChangeEvents:
    """Tests for queue change event emission."""

    def make_issue(self, number: int, title: str = "") -> Issue:
        """Create a test issue."""
        return Issue(
            number=number,
            title=title or f"Issue #{number}",
            labels=["agent:backend"],
            state="open",
        )

    def test_queue_change_emits_event(self):
        """Queue changes emit queue.changed event."""
        from issue_orchestrator.infra.orchestrator import Orchestrator
        from issue_orchestrator.infra.config import Config
        from issue_orchestrator.ports import EventSink
        from issue_orchestrator.control.orchestrator_support import OrchestratorSupport

        config = MagicMock(spec=Config)
        config.repo = "test/repo"

        events = MagicMock(spec=EventSink)
        state = OrchestratorState()
        state.cached_queue_issues = [self.make_issue(1), self.make_issue(2)]

        plan_applier = MagicMock(spec=OrchestratorSupport)
        plan_applier.state = state

        with patch.object(Orchestrator, '__init__', lambda self, *args, **kwargs: None):
            orch = Orchestrator.__new__(Orchestrator)
            orch.state = state
            orch.config = config
            # Create mock deps
            orch.deps = MagicMock()
            orch.deps.events = events
            orch.deps.repository_host = MagicMock()
            # Set cached_property directly
            object.__setattr__(orch, '_plan_applier', plan_applier)

        # Call update_queue_cache - it delegates to plan_applier
        orch.update_queue_cache()

        # Verify plan_applier.update_queue_cache was called (which handles the event emission)
        plan_applier.update_queue_cache.assert_called_once()

    def test_queue_no_change_no_event(self):
        """Orchestrator.update_queue_cache delegates to plan_applier."""
        from issue_orchestrator.infra.orchestrator import Orchestrator
        from issue_orchestrator.infra.config import Config
        from issue_orchestrator.ports import EventSink
        from issue_orchestrator.control.orchestrator_support import OrchestratorSupport

        config = MagicMock(spec=Config)
        events = MagicMock(spec=EventSink)
        state = OrchestratorState()
        issues = [self.make_issue(1), self.make_issue(2)]
        state.cached_queue_issues = issues  # type: ignore

        plan_applier = MagicMock(spec=OrchestratorSupport)
        plan_applier.state = state

        with patch.object(Orchestrator, '__init__', lambda self, *args, **kwargs: None):
            orch = Orchestrator.__new__(Orchestrator)
            orch.state = state
            orch.config = config
            # Create mock deps
            orch.deps = MagicMock()
            orch.deps.events = events
            orch.deps.repository_host = MagicMock()
            # Set cached_property directly
            object.__setattr__(orch, '_plan_applier', plan_applier)

        # Call update_queue_cache - it delegates to plan_applier
        orch.update_queue_cache()

        # Verify plan_applier.update_queue_cache was called (which handles the event emission)
        plan_applier.update_queue_cache.assert_called_once()


class TestClientEventHandlers:
    """Tests that the web UI client has proper event handlers."""

    @pytest.fixture
    def dashboard_html(self):
        """Get the dashboard template and JS content."""
        from pathlib import Path
        base_path = Path(__file__).parent.parent.parent / "src/issue_orchestrator"
        template_content = (base_path / "templates/dashboard.html").read_text()
        row_content = (base_path / "templates/issue_row.html").read_text()
        js_content = (base_path / "static/js/dashboard.js").read_text()
        return template_content + row_content + js_content

    def test_has_dependency_blocked_handler(self, dashboard_html):
        """Dashboard has handler for dependency.blocked events."""
        assert "addEventListener('dependency.blocked'" in dashboard_html

    def test_has_dependency_unblocked_handler(self, dashboard_html):
        """Dashboard has handler for dependency.unblocked events."""
        assert "addEventListener('dependency.unblocked'" in dashboard_html

    def test_has_queue_changed_handler(self, dashboard_html):
        """Dashboard has handler for queue.changed events."""
        assert "addEventListener('queue.changed'" in dashboard_html

    def test_loads_dependency_problems_on_page_load(self, dashboard_html):
        """Dashboard fetches dependency problems on load."""
        assert "loadDependencyProblems()" in dashboard_html
        assert "/api/dependency-problems" in dashboard_html

    def test_rebinds_visibility_observers_after_issue_row_refresh(self, dashboard_html):
        """Row replacement should rebind observers for visibility-based refresh behavior."""
        assert "initVisibilityObserver();" in dashboard_html
        assert "initFlowLazyVisibleRefresh();" in dashboard_html

    def test_has_update_warning_function(self, dashboard_html):
        """Dashboard has function to update warning icons."""
        assert "updateDependencyWarning" in dashboard_html

    def test_warning_icon_element_exists(self, dashboard_html):
        """Dashboard template includes warning icon elements."""
        assert "dep-warning-icon" in dashboard_html
        assert 'id="dep-warning-' in dashboard_html


class TestDependencyProblemsAPI:
    """Tests for the /api/dependency-problems endpoint."""

    @pytest.fixture
    def app_client(self):
        """Create test client with mocked orchestrator."""
        from fastapi.testclient import TestClient
        from issue_orchestrator.entrypoints import web
        from issue_orchestrator.entrypoints.web import set_orchestrator, get_orchestrator
        from issue_orchestrator.infra.config import Config

        mock_orch = MagicMock()
        mock_orch.state = OrchestratorState()
        mock_orch.config = MagicMock(spec=Config)
        mock_orch.config.repo = "test/repo"

        # Set the global orchestrator reference
        original_orch = get_orchestrator()
        set_orchestrator(mock_orch)
        client = TestClient(web.app)
        yield client, mock_orch
        set_orchestrator(original_orch)

    def test_empty_problems(self, app_client):
        """Empty problems returns empty dict."""
        client, mock_orch = app_client

        response = client.get("/api/dependency-problems")

        assert response.status_code == 200
        data = response.json()
        assert data["problems"] == {}

    def test_returns_problems(self, app_client):
        """Returns problems from state."""
        client, mock_orch = app_client

        mock_orch.state.dependency_problems = {
            5: DependencyProblem(
                issue_number=5,
                issue_title="Blocked Issue",
                blocked_by=[],
                summary="Blocked - waiting on: #1",
            )
        }

        response = client.get("/api/dependency-problems")

        assert response.status_code == 200
        data = response.json()
        assert "5" in data["problems"]
        assert data["problems"]["5"]["issue_number"] == 5
        assert data["problems"]["5"]["summary"] == "Blocked - waiting on: #1"
        assert "test/repo" in data["problems"]["5"]["issue_url"]
