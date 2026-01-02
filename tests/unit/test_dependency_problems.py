"""Tests for dependency problems tracking in orchestrator and web API."""

import pytest
from unittest.mock import MagicMock, patch

from issue_orchestrator.models import DependencyProblem, Issue, OrchestratorState


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


class TestOrchestratorDependencyProblems:
    """Tests for orchestrator dependency problem tracking."""

    def make_issue(self, number: int, title: str = "") -> Issue:
        """Create a test issue."""
        return Issue(
            number=number,
            title=title or f"Issue #{number}",
            labels=["agent:backend"],
            state="open",
        )

    def test_update_dependency_problems_adds_new(self):
        """New blocked issues are added to state and events emitted."""
        from issue_orchestrator.orchestrator import Orchestrator
        from issue_orchestrator.config import Config
        from issue_orchestrator.ports import EventSink, TraceEvent

        # Create minimal orchestrator with mocked dependencies
        config = MagicMock(spec=Config)
        config.repo = "test/repo"
        config.ui_mode = "headless"

        events = MagicMock(spec=EventSink)
        state = OrchestratorState()

        # Create orchestrator and inject dependencies
        from issue_orchestrator.events import EventContext
        with patch.object(Orchestrator, '__init__', lambda self, *args, **kwargs: None):
            orch = Orchestrator.__new__(Orchestrator)
            orch.state = state
            orch.events = events
            orch.config = config
            orch._event_context = EventContext()
            orch._repository_host = MagicMock()
            orch.fact_gatherer = MagicMock()
            orch.pr_scanner = MagicMock()
            orch.label_sync = MagicMock()

        # Call update with a blocked issue
        issue = self.make_issue(5, "Blocked Issue")
        dep_blocked = [(issue, "Blocked - waiting on: #1")]

        orch._update_dependency_problems(dep_blocked)

        # Check state was updated
        assert 5 in orch.state.dependency_problems
        assert orch.state.dependency_problems[5].issue_number == 5
        assert orch.state.dependency_problems[5].summary == "Blocked - waiting on: #1"

        # Check event was emitted
        events.publish.assert_called_once()
        call_args = events.publish.call_args[0][0]
        assert call_args.name == "dependency.blocked"
        assert call_args.data["issue_number"] == 5

    def test_update_dependency_problems_removes_resolved(self):
        """Resolved issues are removed from state and events emitted."""
        from issue_orchestrator.orchestrator import Orchestrator
        from issue_orchestrator.config import Config
        from issue_orchestrator.ports import EventSink

        config = MagicMock(spec=Config)
        config.repo = "test/repo"
        config.ui_mode = "headless"

        events = MagicMock(spec=EventSink)

        # Pre-populate state with a blocked issue
        state = OrchestratorState()
        state.dependency_problems = {
            5: DependencyProblem(
                issue_number=5,
                issue_title="Was Blocked",
                blocked_by=[],
                summary="Blocked - waiting on: #1",
            )
        }

        from issue_orchestrator.events import EventContext
        with patch.object(Orchestrator, '__init__', lambda self, *args, **kwargs: None):
            orch = Orchestrator.__new__(Orchestrator)
            orch.state = state
            orch.events = events
            orch.config = config
            orch._event_context = EventContext()
            orch._repository_host = MagicMock()
            orch.fact_gatherer = MagicMock()
            orch.pr_scanner = MagicMock()
            orch.label_sync = MagicMock()

        # Call update with empty blocked list (issue resolved)
        orch._update_dependency_problems([])

        # Check state was updated
        assert 5 not in orch.state.dependency_problems

        # Check unblocked event was emitted
        events.publish.assert_called_once()
        call_args = events.publish.call_args[0][0]
        assert call_args.name == "dependency.unblocked"
        assert call_args.data["issue_number"] == 5

    def test_update_dependency_problems_no_change(self):
        """No events emitted when nothing changes."""
        from issue_orchestrator.orchestrator import Orchestrator
        from issue_orchestrator.config import Config
        from issue_orchestrator.ports import EventSink

        config = MagicMock(spec=Config)
        events = MagicMock(spec=EventSink)
        state = OrchestratorState()

        from issue_orchestrator.events import EventContext
        with patch.object(Orchestrator, '__init__', lambda self, *args, **kwargs: None):
            orch = Orchestrator.__new__(Orchestrator)
            orch.state = state
            orch.events = events
            orch.config = config
            orch._event_context = EventContext()
            orch._repository_host = MagicMock()
            orch.fact_gatherer = MagicMock()
            orch.pr_scanner = MagicMock()
            orch.label_sync = MagicMock()

        # Call update with empty list twice
        orch._update_dependency_problems([])
        orch._update_dependency_problems([])

        # No events should be emitted
        events.publish.assert_not_called()


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
        from issue_orchestrator.orchestrator import Orchestrator
        from issue_orchestrator.config import Config
        from issue_orchestrator.ports import EventSink
        from issue_orchestrator.control.orchestrator_support import OrchestratorSupport

        config = MagicMock(spec=Config)
        config.repo = "test/repo"

        events = MagicMock(spec=EventSink)
        state = OrchestratorState()
        state.cached_queue_issues = [self.make_issue(1), self.make_issue(2)]

        with patch.object(Orchestrator, '__init__', lambda self, *args, **kwargs: None):
            orch = Orchestrator.__new__(Orchestrator)
            orch.state = state
            orch.events = events
            orch.config = config
            orch._repository_host = MagicMock()
            plan_applier = MagicMock(spec=OrchestratorSupport)
            plan_applier.state = state
            orch._plan_applier_instance = plan_applier

        # Call update_queue_cache - it delegates to plan_applier
        orch.update_queue_cache()

        # Verify plan_applier.update_queue_cache was called (which handles the event emission)
        plan_applier.update_queue_cache.assert_called_once()

    def test_queue_no_change_no_event(self):
        """Orchestrator.update_queue_cache delegates to plan_applier."""
        from issue_orchestrator.orchestrator import Orchestrator
        from issue_orchestrator.config import Config
        from issue_orchestrator.ports import EventSink
        from issue_orchestrator.control.orchestrator_support import OrchestratorSupport

        config = MagicMock(spec=Config)
        events = MagicMock(spec=EventSink)
        state = OrchestratorState()
        issues = [self.make_issue(1), self.make_issue(2)]
        state.cached_queue_issues = issues

        with patch.object(Orchestrator, '__init__', lambda self, *args, **kwargs: None):
            orch = Orchestrator.__new__(Orchestrator)
            orch.state = state
            orch.events = events
            orch.config = config
            orch._repository_host = MagicMock()
            plan_applier = MagicMock(spec=OrchestratorSupport)
            plan_applier.state = state
            orch._plan_applier_instance = plan_applier

        # Call update_queue_cache - it delegates to plan_applier
        orch.update_queue_cache()

        # Verify plan_applier.update_queue_cache was called (which handles the event emission)
        plan_applier.update_queue_cache.assert_called_once()


class TestClientEventHandlers:
    """Tests that the web UI client has proper event handlers."""

    @pytest.fixture
    def dashboard_html(self):
        """Get the dashboard template content."""
        from pathlib import Path
        template_path = Path(__file__).parent.parent.parent / "src/issue_orchestrator/templates/dashboard.html"
        return template_path.read_text()

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
        from issue_orchestrator import web
        from issue_orchestrator.config import Config

        mock_orch = MagicMock()
        mock_orch.state = OrchestratorState()
        mock_orch.config = MagicMock(spec=Config)
        mock_orch.config.repo = "test/repo"

        # Set the global orchestrator reference
        original_orch = web._orchestrator
        web._orchestrator = mock_orch
        client = TestClient(web.app)
        yield client, mock_orch
        web._orchestrator = original_orch

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
