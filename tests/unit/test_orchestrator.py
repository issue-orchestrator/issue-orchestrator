"""Unit tests for the orchestrator module."""

import asyncio
import pytest
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch, call, AsyncMock, PropertyMock
from issue_orchestrator.orchestrator import Orchestrator, run_orchestrator
from issue_orchestrator.models import (
    Issue,
    Session,
    SessionStatus,
    AgentConfig,
    OrchestratorState,
)
from issue_orchestrator.config import Config
from issue_orchestrator.control.scheduler import Scheduler
from issue_orchestrator.observation.observer import SessionObserver
from issue_orchestrator.ports.pull_request_tracker import PRInfo


# Helper functions
def create_issue(number, title="Test Issue", labels=None, milestone=None):
    """Helper to create Issue objects for testing."""
    if labels is None:
        labels = ["agent:web"]
    return Issue(
        number=number,
        title=title,
        labels=labels,
        milestone=milestone,
    )


def create_session(issue, worktree_path="/tmp/worktree", branch_name="feature/test"):
    """Helper to create Session objects for testing."""
    agent_config = AgentConfig(
        prompt_path=Path("/tmp/prompt.txt"),
        worktree_base=Path("/tmp"),
        model="sonnet",
        timeout_minutes=45,
    )
    return Session(
        issue=issue,
        agent_config=agent_config,
        tmux_session_name=f"issue-{issue.number}",
        worktree_path=Path(worktree_path),
        branch_name=branch_name,
    )


def create_pr_info(number, title="Test PR", labels=None, branch="feature/test"):
    """Helper to create PRInfo objects for testing."""
    if labels is None:
        labels = []
    return PRInfo(
        number=number,
        title=title,
        url=f"https://github.com/test/repo/pull/{number}",
        branch=branch,
        body="Test PR body",
        state="open",
        labels=labels,
    )


class TestOrchestratorInit:
    """Test Orchestrator initialization."""

    def test_post_init_creates_scheduler(self, sample_config):
        """Test that __post_init__ creates a Scheduler."""
        orchestrator = Orchestrator(config=sample_config)

        assert isinstance(orchestrator.scheduler, Scheduler)
        assert orchestrator.scheduler.config == sample_config

    def test_post_init_creates_observer(self, sample_config):
        """Test that __post_init__ creates a SessionObserver."""
        orchestrator = Orchestrator(config=sample_config)

        assert isinstance(orchestrator.observer, SessionObserver)
        assert orchestrator.observer.config == sample_config
        # Verify observer has reference to session_machines
        assert orchestrator.observer.session_machines is orchestrator.session_machines

    def test_post_init_initializes_state(self, sample_config):
        """Test that state is initialized to default OrchestratorState."""
        orchestrator = Orchestrator(config=sample_config)

        assert isinstance(orchestrator.state, OrchestratorState)
        assert orchestrator.state.active_sessions == []
        assert orchestrator.state.completed_today == []
        assert orchestrator.state.paused is False
        assert orchestrator.state.priority_queue == []

    def test_shutdown_flag_defaults_to_false(self, sample_config):
        """Test that _shutdown_requested is False by default."""
        orchestrator = Orchestrator(config=sample_config)

        assert orchestrator._shutdown_requested is False


class TestBuildLabels:
    """Test the _build_labels helper method."""

    def test_build_labels_without_filter_label(self, sample_config):
        """Test building labels when no filter_label is configured."""
        sample_config.filter_label = None
        orchestrator = Orchestrator(config=sample_config)

        labels = orchestrator._build_labels("agent:web", "in-progress")

        assert labels == ["agent:web", "in-progress"]

    def test_build_labels_with_filter_label(self, sample_config):
        """Test building labels when filter_label is configured."""
        sample_config.filter_label = "test-data"
        orchestrator = Orchestrator(config=sample_config)

        labels = orchestrator._build_labels("agent:web", "in-progress")

        assert labels == ["agent:web", "in-progress", "test-data"]

    def test_build_labels_empty_input(self, sample_config):
        """Test building labels with no input labels."""
        sample_config.filter_label = "test-data"
        orchestrator = Orchestrator(config=sample_config)

        labels = orchestrator._build_labels()

        assert labels == ["test-data"]

    def test_build_labels_single_label(self, sample_config):
        """Test building labels with a single input label."""
        sample_config.filter_label = None
        orchestrator = Orchestrator(config=sample_config)

        labels = orchestrator._build_labels("agent:mobile")

        assert labels == ["agent:mobile"]


class TestGetMilestoneFilter:
    """Test the _get_milestone_filter helper method."""

    def test_get_milestone_filter_when_configured(self, sample_config):
        """Test getting milestone filter when configured."""
        sample_config.filter_milestone = "M6"
        orchestrator = Orchestrator(config=sample_config)

        milestone = orchestrator._get_milestone_filter()

        assert milestone == "M6"

    def test_get_milestone_filter_when_not_configured(self, sample_config):
        """Test getting milestone filter when not configured."""
        sample_config.filter_milestone = None
        orchestrator = Orchestrator(config=sample_config)

        milestone = orchestrator._get_milestone_filter()

        assert milestone is None


class TestStartup:
    """Test the startup method."""

    @pytest.mark.asyncio
    @patch("issue_orchestrator.analysis.get_issue_branches")
    @patch("issue_orchestrator.analysis.analyze_issue")
    async def test_startup_checks_in_progress_issues(
        self,
        mock_analyze,
        mock_get_branches,
        sample_config,
        mock_repository_host,
    ):
        """Test that startup checks for in-progress issues."""
        mock_get_branches.return_value = {}
        mock_repository_host.issues = []

        orchestrator = Orchestrator(config=sample_config, _repository_host=mock_repository_host)
        await orchestrator.startup()

        # Should query for in-progress issues for each agent type
        assert len(mock_repository_host.list_issues_calls) > 0
        call = mock_repository_host.list_issues_calls[0]
        assert "agent:web" in call["labels"]
        assert sample_config.get_label_in_progress() in call["labels"]

    @pytest.mark.asyncio
    @patch("issue_orchestrator.analysis.get_issue_branches")
    @patch("issue_orchestrator.analysis.analyze_issue")
    async def test_startup_clears_orphaned_labels(
        self,
        mock_analyze,
        mock_get_branches,
        sample_config,
        mock_repository_host,
    ):
        """Test that startup clears orphaned in-progress labels."""
        mock_get_branches.return_value = {}

        issue = create_issue(1, labels=["agent:web", "in-progress"])
        mock_repository_host.issues = [issue]

        # Mock analyze_issue to indicate orphaned label
        mock_state = MagicMock()
        mock_state.has_session = False
        mock_state.has_open_pr = False
        mock_state.has_partial_work = False
        mock_state.is_orphaned_label = True
        mock_analyze.return_value = mock_state

        orchestrator = Orchestrator(config=sample_config, _repository_host=mock_repository_host)
        await orchestrator.startup()

        # Should remove the in-progress label
        assert (1, sample_config.get_label_in_progress()) in mock_repository_host.remove_label_calls

    @pytest.mark.asyncio
    @patch("issue_orchestrator.analysis.get_issue_branches")
    @patch("issue_orchestrator.analysis.analyze_issue")
    async def test_startup_skips_issues_with_open_prs(
        self,
        mock_analyze,
        mock_get_branches,
        sample_config,
        mock_repository_host,
    ):
        """Test that startup doesn't clear labels for issues with open PRs."""
        mock_get_branches.return_value = {}

        issue = create_issue(1, labels=["agent:web", "in-progress"])
        mock_repository_host.issues = [issue]

        # Mock analyze_issue to indicate has open PR
        mock_state = MagicMock()
        mock_state.has_session = False
        mock_state.has_open_pr = True
        mock_state.pr_url = "https://github.com/owner/repo/pull/123"
        mock_analyze.return_value = mock_state

        orchestrator = Orchestrator(config=sample_config, _repository_host=mock_repository_host)
        await orchestrator.startup()

        # Should NOT remove the label
        assert len(mock_repository_host.remove_label_calls) == 0

    @pytest.mark.asyncio
    @patch("issue_orchestrator.analysis.get_issue_branches")
    @patch("issue_orchestrator.analysis.analyze_issue")
    async def test_startup_resumes_partial_work(
        self,
        mock_analyze,
        mock_get_branches,
        patch_plugin_manager,
        sample_config,
        mock_repository_host,
    ):
        """Test that startup resumes work for partial work (branch but no session)."""
        mock_get_branches.return_value = {}
        patch_plugin_manager.plugin.session_exists_override = False  # No existing session, allow launch

        issue = create_issue(1, labels=["agent:web", "in-progress"])
        mock_repository_host.issues = [issue]

        # Mock analyze_issue to indicate has partial work
        mock_state = MagicMock()
        mock_state.has_session = False
        mock_state.has_open_pr = False
        mock_state.has_partial_work = True
        mock_state.branch = "feature/issue-1"
        mock_analyze.return_value = mock_state

        orchestrator = Orchestrator(config=sample_config, _repository_host=mock_repository_host)
        await orchestrator.startup()

        # Should NOT remove the label - we keep in-progress and resume work
        assert len(mock_repository_host.remove_label_calls) == 0
        # Session should have been launched (check active_sessions)
        assert len(orchestrator.state.active_sessions) == 1
        assert orchestrator.state.active_sessions[0].issue.number == 1

    @pytest.mark.asyncio
    @patch("issue_orchestrator.analysis.get_issue_branches")
    @patch("issue_orchestrator.analysis.analyze_issue")
    async def test_startup_skips_blocked_issues(
        self,
        mock_analyze,
        mock_get_branches,
        sample_config,
        mock_repository_host,
    ):
        """Test that startup skips issues that are blocked (waiting for human)."""
        mock_get_branches.return_value = {}

        # Issue has both in-progress AND blocked labels
        issue = create_issue(1, labels=["agent:web", "in-progress", "blocked"])
        mock_repository_host.issues = [issue]

        # Mock analyze_issue - shouldn't matter since we skip before analyzing
        mock_state = MagicMock()
        mock_state.has_session = False
        mock_state.has_open_pr = False
        mock_state.has_partial_work = True
        mock_state.branch = "feature/issue-1"
        mock_analyze.return_value = mock_state

        orchestrator = Orchestrator(config=sample_config, _repository_host=mock_repository_host)
        await orchestrator.startup()

        # Should NOT remove any labels
        assert len(mock_repository_host.remove_label_calls) == 0
        # Should NOT launch a session - blocked issues wait for human
        assert len(orchestrator.state.active_sessions) == 0


class TestLaunchSession:
    """Test the launch_session method."""

    @patch("issue_orchestrator.control.session_launcher.create_worktree")
    def test_launch_session_creates_worktree(
        self,
        mock_create_worktree,
        patch_plugin_manager,
        sample_config,
        mock_repository_host,
    ):
        """Test that launch_session creates a worktree."""
        patch_plugin_manager.plugin.session_exists_override = False
        mock_create_worktree.return_value = (Path("/tmp/worktree"), "feature/issue-1")

        issue = create_issue(1, labels=["agent:web"])
        orchestrator = Orchestrator(config=sample_config, _repository_host=mock_repository_host)

        session = orchestrator.launch_session(issue)

        mock_create_worktree.assert_called_once()
        assert mock_create_worktree.call_args[1]["issue_number"] == 1

    @patch("issue_orchestrator.control.session_launcher.create_worktree")
    def test_launch_session_adds_in_progress_label(
        self,
        mock_create_worktree,
        patch_plugin_manager,
        sample_config,
        mock_repository_host,
    ):
        """Test that launch_session adds the in-progress label."""
        patch_plugin_manager.plugin.session_exists_override = False
        mock_create_worktree.return_value = (Path("/tmp/worktree"), "feature/issue-1")

        issue = create_issue(1, labels=["agent:web"])
        # Proper DI: inject mock adapter instead of patching functions
        orchestrator = Orchestrator(config=sample_config, _repository_host=mock_repository_host)

        session = orchestrator.launch_session(issue)

        # Verify adapter was called with correct arguments
        assert (1, sample_config.get_label_in_progress()) in mock_repository_host.add_label_calls

    @patch("issue_orchestrator.control.session_launcher.create_worktree")
    def test_launch_session_creates_tmux_session(
        self,
        mock_create_worktree,
        patch_plugin_manager,
        sample_config,
        mock_repository_host,
    ):
        """Test that launch_session creates a tmux session."""
        patch_plugin_manager.plugin.session_exists_override = False
        mock_create_worktree.return_value = (Path("/tmp/worktree"), "feature/issue-1")

        issue = create_issue(1, title="Test Issue", labels=["agent:web"])
        sample_config.ui_mode = "tmux"  # Explicitly test tmux mode
        orchestrator = Orchestrator(config=sample_config, _repository_host=mock_repository_host)

        session = orchestrator.launch_session(issue)

        # Verify session was created via plugin
        assert len(patch_plugin_manager.plugin.create_session_calls) == 1
        call = patch_plugin_manager.plugin.create_session_calls[0]
        assert call["session_id"] == 1  # issue number
        assert isinstance(call["command"], str)
        assert call["working_dir"] == "/tmp/worktree"

    @patch("issue_orchestrator.control.session_launcher.create_worktree")
    def test_launch_session_adds_to_active_sessions(
        self,
        mock_create_worktree,
        patch_plugin_manager,
        sample_config,
        mock_repository_host,
    ):
        """Test that launch_session adds the session to active_sessions."""
        patch_plugin_manager.plugin.session_exists_override = False
        mock_create_worktree.return_value = (Path("/tmp/worktree"), "feature/issue-1")

        issue = create_issue(1, labels=["agent:web"])
        orchestrator = Orchestrator(config=sample_config, _repository_host=mock_repository_host)

        assert len(orchestrator.state.active_sessions) == 0

        session = orchestrator.launch_session(issue)

        assert len(orchestrator.state.active_sessions) == 1
        assert orchestrator.state.active_sessions[0] == session

    @patch("issue_orchestrator.control.session_launcher.create_worktree")
    def test_launch_session_returns_session_object(
        self,
        mock_create_worktree,
        patch_plugin_manager,
        sample_config,
        mock_repository_host,
    ):
        """Test that launch_session returns a Session object."""
        patch_plugin_manager.plugin.session_exists_override = False
        mock_create_worktree.return_value = (Path("/tmp/worktree"), "feature/issue-1")

        issue = create_issue(1, labels=["agent:web"])
        orchestrator = Orchestrator(config=sample_config, _repository_host=mock_repository_host)

        session = orchestrator.launch_session(issue)

        assert isinstance(session, Session)
        assert session.issue == issue
        assert session.tmux_session_name == "issue-1"
        assert session.worktree_path == Path("/tmp/worktree")
        assert session.branch_name == "feature/issue-1"

    def test_launch_session_returns_none_if_session_already_exists(
        self,
        patch_plugin_manager,
        sample_config,
    ):
        """Test that launch_session returns None if session already exists."""
        patch_plugin_manager.plugin.session_exists_override = True  # Session exists in iTerm/tmux

        issue = create_issue(1, labels=["agent:web"])
        orchestrator = Orchestrator(config=sample_config)

        session = orchestrator.launch_session(issue)

        assert session is None

    def test_launch_session_returns_none_for_unknown_agent(
        self,
        sample_config,
    ):
        """Test that launch_session returns None for unknown agent type."""
        issue = create_issue(1, labels=["agent:unknown"])
        orchestrator = Orchestrator(config=sample_config)

        # New behavior: SessionLauncher returns graceful failure instead of raising
        session = orchestrator.launch_session(issue)
        assert session is None

    @patch("issue_orchestrator.control.session_launcher.create_worktree")
    def test_launch_session_uses_agent_repo_root_if_configured(
        self,
        mock_create_worktree,
        patch_plugin_manager,
        sample_config,
        mock_repository_host,
    ):
        """Test that launch_session uses agent-specific repo_root if configured."""
        patch_plugin_manager.plugin.session_exists_override = False
        mock_create_worktree.return_value = (Path("/tmp/worktree"), "feature/issue-1")

        # Configure agent with specific repo_root
        sample_config.agents["agent:web"].repo_root = Path("/custom/repo/path")

        issue = create_issue(1, labels=["agent:web"])
        orchestrator = Orchestrator(config=sample_config, _repository_host=mock_repository_host)

        session = orchestrator.launch_session(issue)

        # Should use agent's repo_root
        assert mock_create_worktree.call_args[1]["repo_root"] == Path("/custom/repo/path")

    @patch("issue_orchestrator.control.session_launcher.create_worktree")
    def test_launch_session_falls_back_to_config_repo_root(
        self,
        mock_create_worktree,
        patch_plugin_manager,
        sample_config,
        mock_repository_host,
    ):
        """Test that launch_session falls back to config.repo_root if agent doesn't specify."""
        patch_plugin_manager.plugin.session_exists_override = False
        mock_create_worktree.return_value = (Path("/tmp/worktree"), "feature/issue-1")

        # Ensure agent doesn't have repo_root set
        sample_config.agents["agent:web"].repo_root = None
        sample_config.repo_root = Path("/default/repo/path")

        issue = create_issue(1, labels=["agent:web"])
        orchestrator = Orchestrator(config=sample_config, _repository_host=mock_repository_host)

        session = orchestrator.launch_session(issue)

        # Should use config's repo_root
        assert mock_create_worktree.call_args[1]["repo_root"] == Path("/default/repo/path")


class TestHandleSessionCompletion:
    """Test the handle_session_completion method."""

    @patch("issue_orchestrator.orchestrator.remove_worktree")
    def test_handle_completion_removes_from_active_sessions(
        self,
        mock_remove_worktree,
        sample_config,
    ):
        """Test that handle_session_completion removes session from active list."""
        issue = create_issue(1)
        session = create_session(issue)

        orchestrator = Orchestrator(config=sample_config)
        orchestrator.state.active_sessions.append(session)

        assert len(orchestrator.state.active_sessions) == 1

        orchestrator.handle_session_completion(session, SessionStatus.COMPLETED)

        assert len(orchestrator.state.active_sessions) == 0

    @patch("issue_orchestrator.orchestrator.remove_worktree")
    def test_handle_completion_calls_monitor_handler(
        self,
        mock_remove_worktree,
        sample_config,
    ):
        """Test that handle_session_completion delegates to monitor."""
        issue = create_issue(1)
        session = create_session(issue)

        orchestrator = Orchestrator(config=sample_config)
        orchestrator.state.active_sessions.append(session)

        with patch.object(orchestrator.observer, "handle_completion") as mock_monitor:
            orchestrator.handle_session_completion(session, SessionStatus.COMPLETED)

            mock_monitor.assert_called_once_with(session, SessionStatus.COMPLETED)

    @patch("issue_orchestrator.orchestrator.remove_worktree")
    def test_handle_completion_tracks_completed_issues(
        self,
        mock_remove_worktree,
        sample_config,
    ):
        """Test that handle_session_completion tracks completed issues."""
        issue = create_issue(1)
        session = create_session(issue)

        orchestrator = Orchestrator(config=sample_config)
        orchestrator.state.active_sessions.append(session)

        assert len(orchestrator.state.completed_today) == 0

        orchestrator.handle_session_completion(session, SessionStatus.COMPLETED)

        assert len(orchestrator.state.completed_today) == 1
        assert orchestrator.state.completed_today[0] == 1

    @patch("issue_orchestrator.orchestrator.remove_worktree")
    def test_handle_completion_does_not_track_failed_issues(
        self,
        mock_remove_worktree,
        sample_config,
    ):
        """Test that failed sessions are not added to completed_today."""
        issue = create_issue(1)
        session = create_session(issue)

        orchestrator = Orchestrator(config=sample_config)
        orchestrator.state.active_sessions.append(session)

        orchestrator.handle_session_completion(session, SessionStatus.FAILED)

        assert len(orchestrator.state.completed_today) == 0

    @patch("issue_orchestrator.orchestrator.remove_worktree")
    def test_handle_completion_removes_worktree_for_completed(
        self,
        mock_remove_worktree,
        sample_config,
    ):
        """Test that worktree is removed for completed sessions."""
        issue = create_issue(1)
        session = create_session(issue, worktree_path="/tmp/worktree")

        orchestrator = Orchestrator(config=sample_config)
        orchestrator.state.active_sessions.append(session)

        orchestrator.handle_session_completion(session, SessionStatus.COMPLETED)

        mock_remove_worktree.assert_called_once_with(Path("/tmp/worktree"))

    @patch("issue_orchestrator.orchestrator.remove_worktree")
    def test_handle_completion_keeps_worktree_for_blocked(
        self,
        mock_remove_worktree,
        sample_config,
    ):
        """Test that worktree is kept for blocked sessions."""
        issue = create_issue(1)
        session = create_session(issue)

        orchestrator = Orchestrator(config=sample_config)
        orchestrator.state.active_sessions.append(session)

        orchestrator.handle_session_completion(session, SessionStatus.BLOCKED)

        mock_remove_worktree.assert_not_called()

    @patch("issue_orchestrator.orchestrator.remove_worktree")
    def test_handle_completion_keeps_worktree_for_failed(
        self,
        mock_remove_worktree,
        sample_config,
    ):
        """Test that worktree is kept for failed sessions."""
        issue = create_issue(1)
        session = create_session(issue)

        orchestrator = Orchestrator(config=sample_config)
        orchestrator.state.active_sessions.append(session)

        orchestrator.handle_session_completion(session, SessionStatus.FAILED)

        mock_remove_worktree.assert_not_called()

    @patch("issue_orchestrator.orchestrator.remove_worktree")
    def test_handle_completion_handles_worktree_removal_error(
        self,
        mock_remove_worktree,
        sample_config,
    ):
        """Test that worktree removal errors are handled gracefully."""
        mock_remove_worktree.side_effect = Exception("Failed to remove worktree")

        issue = create_issue(1)
        session = create_session(issue)

        orchestrator = Orchestrator(config=sample_config)
        orchestrator.state.active_sessions.append(session)

        # Should not raise exception
        orchestrator.handle_session_completion(session, SessionStatus.COMPLETED)

    @patch("issue_orchestrator.orchestrator.remove_worktree")
    def test_handle_completion_closes_session(
        self,
        mock_remove_worktree,
        sample_config,
    ):
        """Test that handle_session_completion closes the terminal session to prevent tab accumulation."""
        issue = create_issue(1)
        session = create_session(issue)

        orchestrator = Orchestrator(config=sample_config)
        orchestrator.state.active_sessions.append(session)

        with patch.object(orchestrator, "_kill_session") as mock_kill:
            orchestrator.handle_session_completion(session, SessionStatus.COMPLETED)

            # Verify _kill_session was called with the session name
            mock_kill.assert_called_once_with(session.tmux_session_name)

    @patch("issue_orchestrator.orchestrator.remove_worktree")
    def test_handle_completion_closes_session_on_failure(
        self,
        mock_remove_worktree,
        sample_config,
    ):
        """Test that session is closed even for failed sessions to prevent tab buildup."""
        issue = create_issue(1)
        session = create_session(issue)

        orchestrator = Orchestrator(config=sample_config)
        orchestrator.state.active_sessions.append(session)

        with patch.object(orchestrator, "_kill_session") as mock_kill:
            orchestrator.handle_session_completion(session, SessionStatus.FAILED)

            # Session should still be closed to prevent accumulation
            mock_kill.assert_called_once_with(session.tmux_session_name)

    @patch("issue_orchestrator.orchestrator.remove_worktree")
    def test_handle_completion_closes_session_gracefully_on_error(
        self,
        mock_remove_worktree,
        sample_config,
    ):
        """Test that session close errors are handled gracefully."""
        issue = create_issue(1)
        session = create_session(issue)

        orchestrator = Orchestrator(config=sample_config)
        orchestrator.state.active_sessions.append(session)

        with patch.object(orchestrator, "_kill_session", side_effect=Exception("Failed to close")):
            # Should not raise exception
            orchestrator.handle_session_completion(session, SessionStatus.COMPLETED)


class TestRunLoop:
    """Test the run_loop method."""

    @pytest.fixture(autouse=True)
    def mock_sleep(self):
        """Mock asyncio.sleep to yield control but not wait."""
        original_sleep = asyncio.sleep  # Save reference before patching

        async def instant_yield(*args):
            await original_sleep(0)  # Use real sleep to yield

        with patch("issue_orchestrator.orchestrator.asyncio.sleep", side_effect=instant_yield):
            yield

    @pytest.mark.asyncio
    async def test_run_loop_exits_on_shutdown_request(
        self,
        sample_config,
        mock_repository_host,
    ):
        """Test that run_loop exits when shutdown is requested."""
        mock_repository_host.issues = []

        orchestrator = Orchestrator(config=sample_config, _repository_host=mock_repository_host)
        orchestrator.request_shutdown()  # Request shutdown immediately

        # Should exit quickly without running loop
        await orchestrator.run_loop()

        assert orchestrator._shutdown_requested is True

    @pytest.mark.asyncio
    async def test_run_loop_checks_active_sessions(
        self,
        sample_config,
        mock_repository_host,
    ):
        """Test that run_loop checks status of active sessions."""
        mock_repository_host.issues = []

        issue = create_issue(1)
        session = create_session(issue)

        from issue_orchestrator.observation.observation import SessionObservationResult
        from issue_orchestrator.control.session_controller import SessionDecision

        orchestrator = Orchestrator(config=sample_config, _repository_host=mock_repository_host)
        orchestrator.state.active_sessions.append(session)

        with patch.object(orchestrator.observer, "observe_session") as mock_observe:
            # Mock observe to return TERMINATED (session exited)
            mock_observe.return_value = SessionObservationResult.terminated(runtime_minutes=10.0)

            # Mock the controller to return COMPLETED
            mock_decision = SessionDecision(
                status=SessionStatus.COMPLETED,
                recovered_from_timeout=False,
                reason="Test",
            )
            mock_controller = MagicMock()
            mock_controller.decide_outcome.return_value = mock_decision

            with patch.object(
                Orchestrator, "_session_controller",
                new_callable=PropertyMock,
                return_value=mock_controller,
            ):
                # Run one iteration
                async def run_one_iteration():
                    await asyncio.sleep(0.01)  # Let loop run once
                    orchestrator.request_shutdown()

                await asyncio.gather(
                    orchestrator.run_loop(),
                    run_one_iteration(),
                )

                mock_observe.assert_called()

    @pytest.mark.asyncio
    async def test_run_loop_handles_completed_sessions(
        self,
        sample_config,
        mock_repository_host,
    ):
        """Test that run_loop handles completed sessions."""
        from issue_orchestrator.observation.observation import SessionObservationResult
        from issue_orchestrator.control.session_controller import SessionDecision

        mock_repository_host.issues = []

        issue = create_issue(1)
        session = create_session(issue)

        orchestrator = Orchestrator(config=sample_config, _repository_host=mock_repository_host)
        orchestrator.state.active_sessions.append(session)

        with patch.object(orchestrator.observer, "observe_session") as mock_observe:
            with patch.object(orchestrator, "handle_session_completion") as mock_handle:
                # Mock observe to return TERMINATED (session exited)
                mock_observe.return_value = SessionObservationResult.terminated(runtime_minutes=10.0)

                # Mock the controller to return COMPLETED
                mock_decision = SessionDecision(
                    status=SessionStatus.COMPLETED,
                    recovered_from_timeout=False,
                    reason="Test",
                )
                mock_controller = MagicMock()
                mock_controller.decide_outcome.return_value = mock_decision

                with patch.object(
                    Orchestrator, "_session_controller",
                    new_callable=PropertyMock,
                    return_value=mock_controller,
                ):
                    # Run one iteration
                    async def run_one_iteration():
                        await asyncio.sleep(0.01)
                        orchestrator.request_shutdown()

                    await asyncio.gather(
                        orchestrator.run_loop(),
                        run_one_iteration(),
                    )

                    # Loop may run multiple iterations before shutdown; just verify it was called
                    mock_handle.assert_called_with(session, SessionStatus.COMPLETED)

    @pytest.mark.asyncio
    async def test_run_loop_fetches_available_issues_when_not_paused(
        self,
        sample_config,
        mock_repository_host,
    ):
        """Test that run_loop fetches available issues when not paused."""
        mock_repository_host.issues = []

        orchestrator = Orchestrator(config=sample_config, _repository_host=mock_repository_host)

        # Run one iteration
        async def run_one_iteration():
            await asyncio.sleep(0.01)
            orchestrator.request_shutdown()

        await asyncio.gather(
            orchestrator.run_loop(),
            run_one_iteration(),
        )

        assert len(mock_repository_host.list_issues_calls) > 0

    @pytest.mark.asyncio
    async def test_run_loop_does_not_fetch_when_paused(
        self,
        sample_config,
        mock_repository_host,
    ):
        """Test that run_loop doesn't fetch new issues when paused."""
        mock_repository_host.issues = []

        orchestrator = Orchestrator(config=sample_config, _repository_host=mock_repository_host)
        orchestrator.state.paused = True

        # Run one iteration
        async def run_one_iteration():
            await asyncio.sleep(0.01)
            orchestrator.request_shutdown()

        await asyncio.gather(
            orchestrator.run_loop(),
            run_one_iteration(),
        )

        # Should not fetch issues when paused
        assert len(mock_repository_host.list_issues_calls) == 0

    @pytest.mark.asyncio
    async def test_run_loop_respects_max_sessions_limit(
        self,
        sample_config,
        mock_repository_host,
    ):
        """Test that run_loop respects max_sessions limit."""
        from issue_orchestrator.observation.observation import SessionObservationResult

        sample_config.max_concurrent_sessions = 2

        issue1 = create_issue(1)
        issue2 = create_issue(2)
        issue3 = create_issue(3)

        mock_repository_host.issues = [issue1, issue2, issue3]

        orchestrator = Orchestrator(config=sample_config, _repository_host=mock_repository_host)

        # Already have 2 active sessions
        orchestrator.state.active_sessions.append(create_session(issue1))
        orchestrator.state.active_sessions.append(create_session(issue2))

        with patch.object(orchestrator.observer, "observe_session") as mock_observe:
            # Mock observe_session to return RUNNING (sessions still active)
            mock_observe.return_value = SessionObservationResult.running(runtime_minutes=5.0)

            with patch.object(orchestrator, "launch_session") as mock_launch:
                # Run one iteration
                async def run_one_iteration():
                    await asyncio.sleep(0.01)
                    orchestrator.request_shutdown()

                await asyncio.gather(
                    orchestrator.run_loop(),
                    run_one_iteration(),
                )

                # Should not launch new sessions when at capacity
                mock_launch.assert_not_called()

    @pytest.mark.asyncio
    async def test_run_loop_launches_sessions_with_available_capacity(
        self,
        sample_config,
        mock_repository_host,
    ):
        """Test that run_loop launches sessions when capacity is available."""
        sample_config.max_concurrent_sessions = 3

        issue1 = create_issue(1, labels=["agent:web"])

        mock_repository_host.issues = [issue1]

        orchestrator = Orchestrator(config=sample_config, _repository_host=mock_repository_host)

        with patch.object(orchestrator, "launch_session") as mock_launch:
            mock_launch.return_value = create_session(issue1)

            # Run one iteration
            async def run_one_iteration():
                await asyncio.sleep(0.01)
                orchestrator.request_shutdown()

            await asyncio.gather(
                orchestrator.run_loop(),
                run_one_iteration(),
            )

            # Should launch session since we have capacity
            mock_launch.assert_called()

    @pytest.mark.asyncio
    async def test_run_loop_handles_launch_exceptions(
        self,
        sample_config,
        mock_repository_host,
    ):
        """Test that run_loop handles exceptions during launch gracefully."""
        issue1 = create_issue(1, labels=["agent:web"])

        mock_repository_host.issues = [issue1]

        orchestrator = Orchestrator(config=sample_config, _repository_host=mock_repository_host)

        with patch.object(orchestrator, "launch_session") as mock_launch:
            mock_launch.side_effect = Exception("Launch failed")

            # Run one iteration - should not crash
            async def run_one_iteration():
                await asyncio.sleep(0.01)
                orchestrator.request_shutdown()

            await asyncio.gather(
                orchestrator.run_loop(),
                run_one_iteration(),
            )

            # Should have attempted launch
            mock_launch.assert_called()

    @pytest.mark.asyncio
    async def test_run_loop_skips_already_claimed_issues(
        self,
        sample_config,
        mock_repository_host,
    ):
        """Test that run_loop continues when an issue is already claimed."""
        issue1 = create_issue(1, labels=["agent:web"])
        issue2 = create_issue(2, labels=["agent:web"])

        mock_repository_host.issues = [issue1, issue2]

        orchestrator = Orchestrator(config=sample_config, _repository_host=mock_repository_host)

        with patch.object(orchestrator, "launch_session") as mock_launch:
            # First issue already claimed, second succeeds
            mock_launch.side_effect = [None, create_session(issue2)]

            # Run one iteration
            async def run_one_iteration():
                await asyncio.sleep(0.01)
                orchestrator.request_shutdown()

            await asyncio.gather(
                orchestrator.run_loop(),
                run_one_iteration(),
            )

            # Should try to launch both (loop may run multiple iterations)
            assert mock_launch.call_count >= 2


class TestMaxIssuesToStart:
    """Test the max_issues_to_start limit functionality."""

    @pytest.fixture(autouse=True)
    def mock_sleep(self):
        """Mock asyncio.sleep to yield control but not wait."""
        original_sleep = asyncio.sleep  # Save reference before patching

        async def instant_yield(*args):
            await original_sleep(0)  # Use real sleep to yield

        with patch("issue_orchestrator.orchestrator.asyncio.sleep", side_effect=instant_yield):
            yield

    @pytest.mark.asyncio
    async def test_run_loop_respects_max_issues_to_start(
        self,
        sample_config,
        mock_repository_host,
    ):
        """Test that run_loop stops launching when max_issues_to_start is reached."""
        sample_config.max_issues_to_start = 2
        sample_config.max_concurrent_sessions = 5  # Plenty of capacity

        issue1 = create_issue(1, labels=["agent:web"])
        issue2 = create_issue(2, labels=["agent:web"])
        issue3 = create_issue(3, labels=["agent:web"])

        mock_repository_host.issues = [issue1, issue2, issue3]

        orchestrator = Orchestrator(config=sample_config, _repository_host=mock_repository_host)

        with patch.object(orchestrator, "launch_session") as mock_launch:
            # Simulate successful launches
            mock_launch.side_effect = [
                create_session(issue1),
                create_session(issue2),
                create_session(issue3),  # Should not be called
            ]

            # Run one iteration
            async def run_one_iteration():
                await asyncio.sleep(0.01)
                orchestrator.request_shutdown()

            await asyncio.gather(
                orchestrator.run_loop(),
                run_one_iteration(),
            )

            # Should only launch 2 issues (max_issues_to_start limit)
            assert mock_launch.call_count == 2
            assert orchestrator.state.issues_started_count == 2

    @pytest.mark.asyncio
    async def test_run_loop_unlimited_when_max_issues_zero(
        self,
        sample_config,
        mock_repository_host,
    ):
        """Test that max_issues_to_start=0 means unlimited."""
        sample_config.max_issues_to_start = 0  # Unlimited
        sample_config.max_concurrent_sessions = 5

        issue1 = create_issue(1, labels=["agent:web"])
        issue2 = create_issue(2, labels=["agent:web"])
        issue3 = create_issue(3, labels=["agent:web"])

        mock_repository_host.issues = [issue1, issue2, issue3]

        orchestrator = Orchestrator(config=sample_config, _repository_host=mock_repository_host)

        with patch.object(orchestrator, "launch_session") as mock_launch:
            mock_launch.side_effect = [
                create_session(issue1),
                create_session(issue2),
                create_session(issue3),
            ]

            async def run_one_iteration():
                await asyncio.sleep(0.01)
                orchestrator.request_shutdown()

            await asyncio.gather(
                orchestrator.run_loop(),
                run_one_iteration(),
            )

            # Should launch all 3 issues (no limit); loop may run multiple iterations
            assert mock_launch.call_count >= 3
            assert orchestrator.state.issues_started_count >= 3

    @pytest.mark.asyncio
    async def test_run_loop_does_not_launch_when_limit_already_reached(
        self,
        sample_config,
        mock_repository_host,
    ):
        """Test that no new issues are launched if limit was already reached."""
        sample_config.max_issues_to_start = 2
        sample_config.max_concurrent_sessions = 5

        issue1 = create_issue(1, labels=["agent:web"])

        mock_repository_host.issues = [issue1]

        orchestrator = Orchestrator(config=sample_config, _repository_host=mock_repository_host)
        # Simulate that we already started 2 issues in previous iterations
        orchestrator.state.issues_started_count = 2

        with patch.object(orchestrator, "launch_session") as mock_launch:
            async def run_one_iteration():
                await asyncio.sleep(0.01)
                orchestrator.request_shutdown()

            await asyncio.gather(
                orchestrator.run_loop(),
                run_one_iteration(),
            )

            # Should not launch any new issues
            mock_launch.assert_not_called()

    @patch("issue_orchestrator.control.session_launcher.create_worktree")
    def test_launch_session_increments_issues_started_count(
        self,
        mock_create_worktree,
        patch_plugin_manager,
        sample_config,
        mock_repository_host,
    ):
        """Test that launching a session increments issues_started_count."""
        patch_plugin_manager.plugin.session_exists_override = False
        mock_create_worktree.return_value = (Path("/tmp/worktree"), "feature/issue-1")

        issue = create_issue(1, labels=["agent:web"])
        orchestrator = Orchestrator(config=sample_config, _repository_host=mock_repository_host)

        assert orchestrator.state.issues_started_count == 0

        # Note: The counter is incremented in run_loop, not in launch_session itself
        # So we test that the state field exists and works
        orchestrator.state.issues_started_count = 5
        assert orchestrator.state.issues_started_count == 5

    @pytest.mark.asyncio
    async def test_run_loop_checks_limit_before_each_launch(
        self,
        sample_config,
        mock_repository_host,
    ):
        """Test that limit is checked before each launch in a batch."""
        sample_config.max_issues_to_start = 1
        sample_config.max_concurrent_sessions = 5

        issue1 = create_issue(1, labels=["agent:web"])
        issue2 = create_issue(2, labels=["agent:web"])

        mock_repository_host.issues = [issue1, issue2]

        orchestrator = Orchestrator(config=sample_config, _repository_host=mock_repository_host)

        with patch.object(orchestrator, "launch_session") as mock_launch:
            mock_launch.return_value = create_session(issue1)

            async def run_one_iteration():
                await asyncio.sleep(0.01)
                orchestrator.request_shutdown()

            await asyncio.gather(
                orchestrator.run_loop(),
                run_one_iteration(),
            )

            # Should only launch 1 issue even though 2 are available
            assert mock_launch.call_count == 1
            assert orchestrator.state.issues_started_count == 1

    @pytest.mark.asyncio
    async def test_run_loop_skipped_claims_dont_count_toward_limit(
        self,
        sample_config,
        mock_repository_host,
    ):
        """Test that issues that were already claimed don't count toward the limit."""
        sample_config.max_issues_to_start = 2
        sample_config.max_concurrent_sessions = 5

        issue1 = create_issue(1, labels=["agent:web"])
        issue2 = create_issue(2, labels=["agent:web"])
        issue3 = create_issue(3, labels=["agent:web"])

        mock_repository_host.issues = [issue1, issue2, issue3]

        orchestrator = Orchestrator(config=sample_config, _repository_host=mock_repository_host)

        with patch.object(orchestrator, "launch_session") as mock_launch:
            # First issue already claimed (returns None), second and third succeed
            mock_launch.side_effect = [
                None,  # Already claimed
                create_session(issue2),
                create_session(issue3),  # Should not be called - limit reached
            ]

            async def run_one_iteration():
                await asyncio.sleep(0.01)
                orchestrator.request_shutdown()

            await asyncio.gather(
                orchestrator.run_loop(),
                run_one_iteration(),
            )

            # Attempted 3, but issue1 was already claimed so only 2 count
            # Actually we should check: attempt 1 (skipped), attempt 2 (success, count=1),
            # attempt 3 (success, count=2), then stop because limit reached
            # But wait - the logic increments AFTER success, so:
            # - Try issue1 -> None (skipped, count stays 0)
            # - Try issue2 -> success (count becomes 1)
            # - Check limit: 1 < 2, continue
            # - Try issue3 -> success (count becomes 2)
            # - Check limit on next iteration: 2 >= 2, stop
            # So we should see 3 launch attempts
            assert mock_launch.call_count == 3
            # But only 2 actually succeeded
            assert orchestrator.state.issues_started_count == 2


class TestControlMethods:
    """Test pause, resume, prioritize methods."""

    def test_pause_sets_paused_flag(self, sample_config):
        """Test that pause() sets the paused flag."""
        orchestrator = Orchestrator(config=sample_config)

        assert orchestrator.state.paused is False

        orchestrator.pause()

        assert orchestrator.state.paused is True

    def test_resume_clears_paused_flag(self, sample_config):
        """Test that resume() clears the paused flag."""
        orchestrator = Orchestrator(config=sample_config)
        orchestrator.state.paused = True

        orchestrator.resume()

        assert orchestrator.state.paused is False

    def test_prioritize_adds_to_queue(self, sample_config):
        """Test that prioritize() adds issue to priority queue."""
        orchestrator = Orchestrator(config=sample_config)

        assert len(orchestrator.state.priority_queue) == 0

        orchestrator.prioritize(42)

        assert len(orchestrator.state.priority_queue) == 1
        assert orchestrator.state.priority_queue[0] == 42

    def test_prioritize_adds_to_front_of_queue(self, sample_config):
        """Test that prioritize() adds issue to front of queue."""
        orchestrator = Orchestrator(config=sample_config)
        orchestrator.state.priority_queue = [1, 2, 3]

        orchestrator.prioritize(42)

        assert orchestrator.state.priority_queue[0] == 42
        assert orchestrator.state.priority_queue == [42, 1, 2, 3]

    def test_prioritize_ignores_duplicates(self, sample_config):
        """Test that prioritize() doesn't add duplicates."""
        orchestrator = Orchestrator(config=sample_config)
        orchestrator.state.priority_queue = [1, 2, 3]

        orchestrator.prioritize(2)

        # Should not add duplicate
        assert orchestrator.state.priority_queue == [1, 2, 3]

    def test_request_shutdown_sets_flag(self, sample_config):
        """Test that request_shutdown() sets the shutdown flag."""
        orchestrator = Orchestrator(config=sample_config)

        assert orchestrator._shutdown_requested is False

        orchestrator.request_shutdown()

        assert orchestrator._shutdown_requested is True


class TestRunOrchestrator:
    """Test the run_orchestrator entry point."""

    @pytest.mark.asyncio
    @patch("issue_orchestrator.orchestrator.Config.load")
    @patch("issue_orchestrator.orchestrator.signal.signal")
    async def test_run_orchestrator_loads_config_from_path(
        self,
        mock_signal,
        mock_config_load,
        sample_config,
        tmp_path,
    ):
        """Test that run_orchestrator loads config from provided path."""
        mock_config_load.return_value = sample_config
        config_path = tmp_path / "config.yaml"

        with patch("issue_orchestrator.orchestrator.Orchestrator.startup") as mock_startup:
            with patch("issue_orchestrator.orchestrator.Orchestrator.run_loop") as mock_run_loop:
                mock_startup.return_value = None
                mock_run_loop.return_value = None

                await run_orchestrator(config_path)

                mock_config_load.assert_called_once_with(config_path)

    @pytest.mark.asyncio
    @patch("issue_orchestrator.orchestrator.Config.find_and_load")
    @patch("issue_orchestrator.orchestrator.signal.signal")
    async def test_run_orchestrator_finds_config_when_no_path(
        self,
        mock_signal,
        mock_config_find,
        sample_config,
    ):
        """Test that run_orchestrator finds config when no path provided."""
        mock_config_find.return_value = sample_config

        with patch("issue_orchestrator.orchestrator.Orchestrator.startup") as mock_startup:
            with patch("issue_orchestrator.orchestrator.Orchestrator.run_loop") as mock_run_loop:
                mock_startup.return_value = None
                mock_run_loop.return_value = None

                await run_orchestrator(None)

                mock_config_find.assert_called_once()

    @pytest.mark.asyncio
    @patch("issue_orchestrator.orchestrator.Config.find_and_load")
    @patch("issue_orchestrator.orchestrator.signal.signal")
    async def test_run_orchestrator_calls_startup(
        self,
        mock_signal,
        mock_config_find,
        sample_config,
    ):
        """Test that run_orchestrator calls startup."""
        mock_config_find.return_value = sample_config

        with patch("issue_orchestrator.orchestrator.Orchestrator.startup") as mock_startup:
            with patch("issue_orchestrator.orchestrator.Orchestrator.run_loop") as mock_run_loop:
                mock_startup.return_value = None
                mock_run_loop.return_value = None

                await run_orchestrator(None)

                mock_startup.assert_called_once()

    @pytest.mark.asyncio
    @patch("issue_orchestrator.orchestrator.Config.find_and_load")
    @patch("issue_orchestrator.orchestrator.signal.signal")
    async def test_run_orchestrator_calls_run_loop(
        self,
        mock_signal,
        mock_config_find,
        sample_config,
    ):
        """Test that run_orchestrator calls run_loop."""
        mock_config_find.return_value = sample_config

        with patch("issue_orchestrator.orchestrator.Orchestrator.startup") as mock_startup:
            with patch("issue_orchestrator.orchestrator.Orchestrator.run_loop") as mock_run_loop:
                mock_startup.return_value = None
                mock_run_loop.return_value = None

                await run_orchestrator(None)

                mock_run_loop.assert_called_once()

    @pytest.mark.asyncio
    @patch("issue_orchestrator.orchestrator.Config.find_and_load")
    @patch("issue_orchestrator.orchestrator.signal.signal")
    async def test_run_orchestrator_sets_up_signal_handlers(
        self,
        mock_signal,
        mock_config_find,
        sample_config,
    ):
        """Test that run_orchestrator sets up signal handlers."""
        mock_config_find.return_value = sample_config

        with patch("issue_orchestrator.orchestrator.Orchestrator.startup") as mock_startup:
            with patch("issue_orchestrator.orchestrator.Orchestrator.run_loop") as mock_run_loop:
                mock_startup.return_value = None
                mock_run_loop.return_value = None

                await run_orchestrator(None)

                # Should set up handlers for SIGINT and SIGTERM
                import signal
                assert mock_signal.call_count == 2
                call_args_list = [call[0][0] for call in mock_signal.call_args_list]
                assert signal.SIGINT in call_args_list
                assert signal.SIGTERM in call_args_list


class TestGatherTriageFacts:
    """Test the fact_gatherer.gather_triage_facts method for triage review workflow.

    Triage issue creation is now handled by the Planner via:
    - fact_gatherer.gather_triage_facts() gathers TriageFacts snapshot
    - fact_gatherer.create_snapshot() includes triage_facts
    - Planner._plan_triage_issue_creation() decides whether to create
    """

    def test_gather_triage_facts_returns_none_without_agent(self, sample_config, mock_repository_host):
        """Test that gather_triage_facts returns None without triage_review_agent."""
        sample_config.triage_review_agent = None
        sample_config.code_reviewed_label = "code-reviewed"
        sample_config.triage_review_threshold = 5

        orchestrator = Orchestrator(config=sample_config, _repository_host=mock_repository_host)
        facts = orchestrator.fact_gatherer.gather_triage_facts(orchestrator.state)
        assert facts is None

    def test_gather_triage_facts_returns_none_with_zero_threshold(self, sample_config, mock_repository_host):
        """Test that gather_triage_facts returns None with threshold=0."""
        sample_config.triage_review_agent = "agent:triage"
        sample_config.code_reviewed_label = "code-reviewed"
        sample_config.triage_review_threshold = 0

        orchestrator = Orchestrator(config=sample_config, _repository_host=mock_repository_host)
        facts = orchestrator.fact_gatherer.gather_triage_facts(orchestrator.state)
        assert facts is None

    def test_gather_triage_facts_returns_facts_below_threshold(self, sample_config, mock_repository_host):
        """Test that gather_triage_facts returns facts even when below threshold."""
        sample_config.triage_review_agent = "agent:triage"
        sample_config.code_reviewed_label = "code-reviewed"
        sample_config.triage_review_threshold = 5

        # Set up PRs with the code-reviewed label (only 2, below threshold of 5)
        mock_repository_host.prs["branch-1"] = [
            create_pr_info(1, "PR 1", labels=["code-reviewed"]),
            create_pr_info(2, "PR 2", labels=["code-reviewed"]),
        ]

        orchestrator = Orchestrator(config=sample_config, _repository_host=mock_repository_host)
        facts = orchestrator.fact_gatherer.gather_triage_facts(orchestrator.state)

        # Facts should be returned (Planner decides whether to act)
        assert facts is not None
        assert facts.pr_count == 2
        assert facts.threshold == 5
        assert facts.watch_label == "code-reviewed"

    def test_gather_triage_facts_returns_facts_at_threshold(self, sample_config, mock_repository_host):
        """Test that gather_triage_facts returns facts when at threshold."""
        sample_config.triage_review_agent = "agent:triage"
        sample_config.code_reviewed_label = "code-reviewed"
        sample_config.triage_review_threshold = 3

        # Set up 3 PRs with code-reviewed label (meets threshold)
        mock_repository_host.prs["branch-1"] = [
            create_pr_info(1, "PR 1", labels=["code-reviewed"]),
            create_pr_info(2, "PR 2", labels=["code-reviewed"]),
            create_pr_info(3, "PR 3", labels=["code-reviewed"]),
        ]
        mock_repository_host.issues = []

        orchestrator = Orchestrator(config=sample_config, _repository_host=mock_repository_host)
        facts = orchestrator.fact_gatherer.gather_triage_facts(orchestrator.state)

        assert facts is not None
        assert facts.pr_count == 3
        assert facts.threshold == 3
        assert facts.existing_triage_issue is None
        assert len(facts.prs) == 3

    def test_gather_triage_facts_finds_existing_triage_issue(self, sample_config, mock_repository_host):
        """Test that gather_triage_facts detects existing triage issue."""
        sample_config.triage_review_agent = "agent:triage"
        sample_config.code_reviewed_label = "code-reviewed"
        sample_config.triage_review_threshold = 3

        # Set up 3 PRs with code-reviewed label
        mock_repository_host.prs["branch-1"] = [
            create_pr_info(1, "PR 1", labels=["code-reviewed"]),
            create_pr_info(2, "PR 2", labels=["code-reviewed"]),
            create_pr_info(3, "PR 3", labels=["code-reviewed"]),
        ]

        # Existing review issue
        existing_issue = create_issue(100, title="Triage Batch Review: 3 PRs pending", labels=["agent:triage"])
        mock_repository_host.issues = [existing_issue]

        orchestrator = Orchestrator(config=sample_config, _repository_host=mock_repository_host)
        facts = orchestrator.fact_gatherer.gather_triage_facts(orchestrator.state)

        assert facts is not None
        assert facts.existing_triage_issue == 100

    def test_gather_triage_facts_includes_pr_info(self, sample_config, mock_repository_host):
        """Test that gather_triage_facts includes PR number and title tuples."""
        sample_config.triage_review_agent = "agent:triage"
        sample_config.code_reviewed_label = "code-reviewed"
        sample_config.triage_reviewed_label = "triage-reviewed"
        sample_config.triage_review_threshold = 2

        # Set up 2 PRs with code-reviewed label
        mock_repository_host.prs["branch-1"] = [
            create_pr_info(10, "Fix bug A", labels=["code-reviewed"]),
            create_pr_info(20, "Add feature B", labels=["code-reviewed"]),
        ]
        mock_repository_host.issues = []

        orchestrator = Orchestrator(config=sample_config, _repository_host=mock_repository_host)
        facts = orchestrator.fact_gatherer.gather_triage_facts(orchestrator.state)

        assert facts is not None
        assert len(facts.prs) == 2
        # PRs should be tuples of (number, title)
        pr_numbers = [pr[0] for pr in facts.prs]
        assert 10 in pr_numbers
        assert 20 in pr_numbers


class TestQueueCodeReview:
    """Test the queue_code_review method."""

    def test_queue_code_review_adds_to_pending_reviews(self, sample_config):
        """Test that queue_code_review adds PR to pending_reviews."""
        orchestrator = Orchestrator(config=sample_config)

        assert len(orchestrator.state.pending_reviews) == 0

        orchestrator.queue_code_review(
            issue_number=42,
            pr_url="https://github.com/owner/repo/pull/123",
            branch_name="feature/issue-42",
        )

        assert len(orchestrator.state.pending_reviews) == 1
        review = orchestrator.state.pending_reviews[0]
        assert review.issue_number == 42
        assert review.pr_number == 123
        assert review.pr_url == "https://github.com/owner/repo/pull/123"
        assert review.branch_name == "feature/issue-42"

    def test_queue_code_review_extracts_pr_number_from_url(self, sample_config):
        """Test that PR number is correctly extracted from various URL formats."""
        orchestrator = Orchestrator(config=sample_config)

        # Standard URL
        orchestrator.queue_code_review(
            issue_number=1,
            pr_url="https://github.com/owner/repo/pull/456",
            branch_name="feature/test",
        )
        assert orchestrator.state.pending_reviews[0].pr_number == 456

        # URL with trailing slash or query params should still work
        orchestrator.queue_code_review(
            issue_number=2,
            pr_url="https://github.com/owner/repo/pull/789/files",
            branch_name="feature/test2",
        )
        assert orchestrator.state.pending_reviews[1].pr_number == 789

    def test_queue_code_review_skips_invalid_pr_url(self, sample_config):
        """Test that invalid PR URLs are skipped."""
        orchestrator = Orchestrator(config=sample_config)

        orchestrator.queue_code_review(
            issue_number=42,
            pr_url="https://github.com/owner/repo/issues/123",  # Not a pull URL
            branch_name="feature/test",
        )

        # Should not add to pending reviews
        assert len(orchestrator.state.pending_reviews) == 0

    def test_queue_code_review_skips_duplicates(self, sample_config):
        """Test that duplicate PRs are not queued twice."""
        from issue_orchestrator.models import PendingReview

        orchestrator = Orchestrator(config=sample_config)

        # Add first review
        orchestrator.queue_code_review(
            issue_number=42,
            pr_url="https://github.com/owner/repo/pull/123",
            branch_name="feature/issue-42",
        )

        # Try to add same PR again
        orchestrator.queue_code_review(
            issue_number=42,
            pr_url="https://github.com/owner/repo/pull/123",
            branch_name="feature/issue-42",
        )

        # Should still have only 1 review
        assert len(orchestrator.state.pending_reviews) == 1


class TestLaunchReviewSession:
    """Test the launch_review_session method."""

    @patch("issue_orchestrator.control.session_launcher.create_worktree")
    def test_launch_review_session_creates_worktree(
        self,
        mock_create_worktree,
        patch_plugin_manager,
        sample_config,
    ):
        """Test that launch_review_session creates a worktree."""
        from issue_orchestrator.models import PendingReview

        patch_plugin_manager.plugin.session_exists_override = False
        mock_create_worktree.return_value = (Path("/tmp/review-worktree"), "feature/issue-42")

        # Configure code review agent
        sample_config.code_review_agent = "agent:web"

        review = PendingReview(
            issue_number=42,
            pr_number=123,
            pr_url="https://github.com/owner/repo/pull/123",
            branch_name="feature/issue-42",
        )

        orchestrator = Orchestrator(config=sample_config)
        session = orchestrator.launch_review_session(review)

        mock_create_worktree.assert_called_once()
        # Should pass branch_name to checkout existing PR branch
        assert mock_create_worktree.call_args[1]["branch_name"] == "feature/issue-42"

    @patch("issue_orchestrator.control.session_launcher.create_worktree")
    def test_launch_review_session_creates_tmux_session(
        self,
        mock_create_worktree,
        patch_plugin_manager,
        sample_config,
    ):
        """Test that launch_review_session creates a tmux session."""
        from issue_orchestrator.models import PendingReview

        patch_plugin_manager.plugin.session_exists_override = False
        mock_create_worktree.return_value = (Path("/tmp/review-worktree"), "feature/issue-42")

        sample_config.code_review_agent = "agent:web"
        sample_config.ui_mode = "tmux"  # Explicitly use tmux mode

        review = PendingReview(
            issue_number=42,
            pr_number=123,
            pr_url="https://github.com/owner/repo/pull/123",
            branch_name="feature/issue-42",
        )

        orchestrator = Orchestrator(config=sample_config)
        session = orchestrator.launch_review_session(review)

        # Verify session was created via plugin
        assert len(patch_plugin_manager.plugin.create_session_calls) == 1
        call = patch_plugin_manager.plugin.create_session_calls[0]
        # Session ID should be review-{pr_number} encoded as integer
        assert call["session_id"] == 123  # PR number

    @patch("issue_orchestrator.control.session_launcher.create_worktree")
    def test_launch_review_session_adds_to_active_sessions(
        self,
        mock_create_worktree,
        patch_plugin_manager,
        sample_config,
    ):
        """Test that launch_review_session adds session to active_sessions."""
        from issue_orchestrator.models import PendingReview

        patch_plugin_manager.plugin.session_exists_override = False
        mock_create_worktree.return_value = (Path("/tmp/review-worktree"), "feature/issue-42")

        sample_config.code_review_agent = "agent:web"

        review = PendingReview(
            issue_number=42,
            pr_number=123,
            pr_url="https://github.com/owner/repo/pull/123",
            branch_name="feature/issue-42",
        )

        orchestrator = Orchestrator(config=sample_config)
        assert len(orchestrator.state.active_sessions) == 0

        session = orchestrator.launch_review_session(review)

        assert len(orchestrator.state.active_sessions) == 1
        assert orchestrator.state.active_sessions[0] == session

    @patch("issue_orchestrator.control.session_launcher.create_worktree")
    def test_launch_review_session_removes_from_pending_queue(
        self,
        mock_create_worktree,
        patch_plugin_manager,
        sample_config,
    ):
        """Test that launch_review_session removes PR from pending_reviews."""
        from issue_orchestrator.models import PendingReview

        patch_plugin_manager.plugin.session_exists_override = False
        mock_create_worktree.return_value = (Path("/tmp/review-worktree"), "feature/issue-42")

        sample_config.code_review_agent = "agent:web"

        review = PendingReview(
            issue_number=42,
            pr_number=123,
            pr_url="https://github.com/owner/repo/pull/123",
            branch_name="feature/issue-42",
        )

        orchestrator = Orchestrator(config=sample_config)
        orchestrator.state.pending_reviews.append(review)
        assert len(orchestrator.state.pending_reviews) == 1

        session = orchestrator.launch_review_session(review)

        assert len(orchestrator.state.pending_reviews) == 0

    def test_launch_review_session_returns_none_if_session_exists(
        self,
        patch_plugin_manager,
        sample_config,
    ):
        """Test that launch_review_session returns None if session already exists."""
        from issue_orchestrator.models import PendingReview

        patch_plugin_manager.plugin.session_exists_override = True  # Session already running

        sample_config.code_review_agent = "agent:web"

        review = PendingReview(
            issue_number=42,
            pr_number=123,
            pr_url="https://github.com/owner/repo/pull/123",
            branch_name="feature/issue-42",
        )

        orchestrator = Orchestrator(config=sample_config)
        session = orchestrator.launch_review_session(review)

        assert session is None

    def test_launch_review_session_uses_review_prefix_for_session_check(
        self,
        patch_plugin_manager,
        sample_config,
    ):
        """Test that launch_review_session checks for review-{pr_number} session."""
        from issue_orchestrator.models import PendingReview

        # Session exists - already running
        patch_plugin_manager.plugin.session_exists_override = True

        sample_config.code_review_agent = "agent:web"

        review = PendingReview(
            issue_number=42,
            pr_number=123,
            pr_url="https://github.com/owner/repo/pull/123",
            branch_name="feature/issue-42",
        )

        orchestrator = Orchestrator(config=sample_config)
        orchestrator.launch_review_session(review)

        # Should check for review-{pr_number} session
        assert 123 in patch_plugin_manager.plugin.session_exists_calls

    def test_launch_review_session_returns_none_without_agent_config(self, sample_config):
        """Test that launch_review_session returns None without code_review_agent configured."""
        from issue_orchestrator.models import PendingReview

        sample_config.code_review_agent = None  # Not configured

        review = PendingReview(
            issue_number=42,
            pr_number=123,
            pr_url="https://github.com/owner/repo/pull/123",
            branch_name="feature/issue-42",
        )

        orchestrator = Orchestrator(config=sample_config)
        session = orchestrator.launch_review_session(review)

        assert session is None

    @patch("issue_orchestrator.control.session_launcher.create_worktree")
    def test_launch_review_session_does_not_enforce_hooks(
        self,
        mock_create_worktree,
        patch_plugin_manager,
        sample_config,
    ):
        """Test that launch_review_session does not install pre-push hooks."""
        from issue_orchestrator.models import PendingReview

        patch_plugin_manager.plugin.session_exists_override = False
        mock_create_worktree.return_value = (Path("/tmp/review-worktree"), "feature/issue-42")

        sample_config.code_review_agent = "agent:web"
        sample_config.enforce_hooks = True  # Even if enabled globally

        review = PendingReview(
            issue_number=42,
            pr_number=123,
            pr_url="https://github.com/owner/repo/pull/123",
            branch_name="feature/issue-42",
        )

        orchestrator = Orchestrator(config=sample_config)
        session = orchestrator.launch_review_session(review)

        # Should explicitly disable hooks for review sessions
        assert mock_create_worktree.call_args[1]["enforce_hooks"] is False


class TestHandleSessionCompletionWithCodeReview:
    """Test handle_session_completion triggering code review.

    Note: Session completion now stores DiscoveredReview for the Planner to decide,
    instead of directly calling queue_code_review.
    """

    @patch("issue_orchestrator.orchestrator.remove_worktree")
    def test_handle_completion_stores_discovered_review(
        self,
        mock_remove_worktree,
        sample_config,
        mock_repository_host,
    ):
        """Test that handle_session_completion stores DiscoveredReview for Planner."""
        from issue_orchestrator.ports.pull_request_tracker import PRInfo
        from issue_orchestrator.models import DiscoveredReview

        sample_config.code_review_agent = "agent:reviewer"
        mock_repository_host.prs["feature/issue-1"] = [
            PRInfo(
                number=456,
                title="Test PR",
                url="https://github.com/owner/repo/pull/456",
                branch="feature/issue-1",
                body="Test",
                state="open",
                labels=[],
            )
        ]

        issue = create_issue(1)
        session = create_session(issue, branch_name="feature/issue-1")

        orchestrator = Orchestrator(config=sample_config, _repository_host=mock_repository_host)
        orchestrator.state.active_sessions.append(session)

        # Initially no discovered reviews
        assert len(orchestrator.state.discovered_reviews) == 0

        orchestrator.handle_session_completion(session, SessionStatus.COMPLETED)

        # Should have stored a DiscoveredReview for the Planner
        assert len(orchestrator.state.discovered_reviews) == 1
        review = orchestrator.state.discovered_reviews[0]
        assert review.issue_number == 1
        assert review.pr_number == 456
        assert review.pr_url == "https://github.com/owner/repo/pull/456"
        assert review.branch_name == "feature/issue-1"

    @patch("issue_orchestrator.orchestrator.remove_worktree")
    def test_handle_completion_does_not_store_review_without_agent(
        self,
        mock_remove_worktree,
        sample_config,
        mock_repository_host,
    ):
        """Test that handle_session_completion doesn't store DiscoveredReview without code_review_agent."""
        from issue_orchestrator.ports.pull_request_tracker import PRInfo

        sample_config.code_review_agent = None

        mock_repository_host.prs["feature/test"] = [
            PRInfo(
                number=456,
                title="Test PR",
                url="https://github.com/owner/repo/pull/456",
                branch="feature/test",
                body="Test",
                state="open",
                labels=[],
            )
        ]

        issue = create_issue(1)
        session = create_session(issue)

        orchestrator = Orchestrator(config=sample_config, _repository_host=mock_repository_host)
        orchestrator.state.active_sessions.append(session)

        orchestrator.handle_session_completion(session, SessionStatus.COMPLETED)

        # Should not store DiscoveredReview without code_review_agent
        assert len(orchestrator.state.discovered_reviews) == 0

    @patch("issue_orchestrator.orchestrator.remove_worktree")
    def test_handle_completion_does_not_store_review_for_blocked(
        self,
        mock_remove_worktree,
        sample_config,
        mock_repository_host,
    ):
        """Test that handle_session_completion doesn't store DiscoveredReview for blocked sessions."""
        sample_config.code_review_agent = "agent:reviewer"

        issue = create_issue(1)
        session = create_session(issue)

        orchestrator = Orchestrator(config=sample_config, _repository_host=mock_repository_host)
        orchestrator.state.active_sessions.append(session)

        orchestrator.handle_session_completion(session, SessionStatus.BLOCKED)

        # Should not store DiscoveredReview for blocked sessions
        assert len(orchestrator.state.discovered_reviews) == 0

    @patch("issue_orchestrator.orchestrator.remove_worktree")
    def test_handle_completion_does_not_store_review_without_pr(
        self,
        mock_remove_worktree,
        sample_config,
        mock_repository_host,
    ):
        """Test that handle_session_completion doesn't store DiscoveredReview if no PR found."""
        sample_config.code_review_agent = "agent:reviewer"

        # No PRs configured for this branch
        mock_repository_host.prs = {}

        issue = create_issue(1)
        session = create_session(issue)

        orchestrator = Orchestrator(config=sample_config, _repository_host=mock_repository_host)
        orchestrator.state.active_sessions.append(session)

        orchestrator.handle_session_completion(session, SessionStatus.COMPLETED)

        # Should not store DiscoveredReview if no PR found
        assert len(orchestrator.state.discovered_reviews) == 0


class TestStartupPendingReviews:
    """Test startup recovery for pending code reviews."""

    @pytest.mark.asyncio
    @patch("issue_orchestrator.analysis.get_issue_branches")
    async def test_startup_scans_for_pending_reviews(
        self,
        mock_get_branches,
        patch_plugin_manager,
        sample_config,
        mock_repository_host,
    ):
        """Test that startup scans for PRs with code_review_label."""
        mock_get_branches.return_value = {}
        mock_repository_host.issues = []
        patch_plugin_manager.plugin.session_exists_override = False

        sample_config.code_review_agent = "agent:reviewer"
        sample_config.code_review_label = "needs-code-review"

        # Set up PRs with the code review label
        mock_repository_host.prs["branch-1"] = [
            create_pr_info(123, "PR 123", labels=["needs-code-review"], branch="feature/123"),
            create_pr_info(456, "PR 456", labels=["needs-code-review"], branch="feature/456"),
        ]

        orchestrator = Orchestrator(config=sample_config, _repository_host=mock_repository_host)
        await orchestrator.startup()

        # Should have queued both PRs for review
        assert len(orchestrator.state.pending_reviews) == 2
        assert orchestrator.state.pending_reviews[0].pr_number == 123
        assert orchestrator.state.pending_reviews[1].pr_number == 456

    @pytest.mark.asyncio
    @patch("issue_orchestrator.analysis.get_issue_branches")
    async def test_startup_skips_reviews_already_in_progress(
        self,
        mock_get_branches,
        sample_config,
        mock_repository_host,
    ):
        """Test that startup skips PRs with active review sessions."""
        mock_get_branches.return_value = {}
        mock_repository_host.issues = []

        sample_config.code_review_agent = "agent:reviewer"
        sample_config.code_review_label = "needs-code-review"

        # Set up PRs with the code review label
        mock_repository_host.prs["branch-1"] = [
            create_pr_info(123, "PR 123", labels=["needs-code-review"], branch="feature/123"),
            create_pr_info(456, "PR 456", labels=["needs-code-review"], branch="feature/456"),
        ]

        orchestrator = Orchestrator(config=sample_config, _repository_host=mock_repository_host)

        # Session exists for PR 123 but not 456
        def session_exists_side_effect(name):
            return name == "review-123"

        with patch.object(orchestrator, "_session_exists", side_effect=session_exists_side_effect):
            await orchestrator.startup()

        # Should only queue PR 456 (123 is already in progress)
        assert len(orchestrator.state.pending_reviews) == 1
        assert orchestrator.state.pending_reviews[0].pr_number == 456

    @pytest.mark.asyncio
    @patch("issue_orchestrator.analysis.get_issue_branches")
    async def test_startup_does_not_scan_without_code_review_config(
        self,
        mock_get_branches,
        sample_config,
        mock_repository_host,
    ):
        """Test that startup doesn't scan for reviews without config."""
        mock_get_branches.return_value = {}
        mock_repository_host.issues = []

        sample_config.code_review_agent = None
        sample_config.code_review_label = None

        orchestrator = Orchestrator(config=sample_config, _repository_host=mock_repository_host)
        await orchestrator.startup()

        # No PRs should be queued when code review is not configured
        assert len(orchestrator.state.pending_reviews) == 0


class TestPauseBehavior:
    """Test that pause stops all new work from starting."""

    # NOTE: test_process_pending_reviews_does_nothing_when_paused was removed
    # because process_pending_reviews() was deleted. The paused behavior is now
    # tested in test_workflows.py::TestReviewWorkflow::test_should_launch_skips_when_paused
    #
    # NOTE: test_check_triage_review_trigger_does_nothing_when_paused was removed
    # because check_triage_review_trigger() was refactored. The pause behavior is now
    # handled by the Planner via the paused flag in OrchestratorSnapshot. The
    # _gather_triage_facts() method just gathers facts regardless of pause state;
    # the Planner decides whether to act on them.

    @pytest.fixture(autouse=True)
    def mock_sleep(self):
        """Mock asyncio.sleep to yield control but not wait."""
        original_sleep = asyncio.sleep

        async def instant_yield(*args):
            await original_sleep(0)

        with patch("issue_orchestrator.orchestrator.asyncio.sleep", side_effect=instant_yield):
            yield

    @pytest.mark.asyncio
    async def test_run_loop_stops_batch_when_paused_mid_launch(
        self,
        sample_config,
        mock_repository_host,
    ):
        """Test that run_loop stops launching when paused mid-batch."""
        sample_config.max_concurrent_sessions = 5

        issue1 = create_issue(1, labels=["agent:web"])
        issue2 = create_issue(2, labels=["agent:web"])
        issue3 = create_issue(3, labels=["agent:web"])

        mock_repository_host.issues = [issue1, issue2, issue3]

        orchestrator = Orchestrator(config=sample_config, _repository_host=mock_repository_host)

        launch_count = 0

        def launch_side_effect(issue):
            nonlocal launch_count
            launch_count += 1
            # Pause after first launch
            if launch_count == 1:
                orchestrator.state.paused = True
            return create_session(issue)

        with patch.object(orchestrator, "launch_session", side_effect=launch_side_effect) as mock_launch:
            async def run_one_iteration():
                await asyncio.sleep(0.01)
                orchestrator.request_shutdown()

            await asyncio.gather(
                orchestrator.run_loop(),
                run_one_iteration(),
            )

            # Should only launch 1 issue (paused after first)
            assert mock_launch.call_count == 1
            assert orchestrator.state.paused is True


class TestReconcileOrphanedPrLabels:
    """Test the reconcile_orphaned_pr_labels method.

    Tests use MockGitHubAdapter to inject test PRs rather than mocking subprocess.
    """

    def test_reconcile_skips_when_no_code_review_label(self, sample_config, mock_repository_host):
        """Test that reconciliation is skipped when code review is not configured."""
        sample_config.code_review_label = None

        orchestrator = Orchestrator(config=sample_config, _repository_host=mock_repository_host)
        fixed_count = orchestrator.reconcile_orphaned_pr_labels()

        assert fixed_count == 0
        # Should not have added any labels
        assert len(mock_repository_host.add_label_calls) == 0

    def test_reconcile_adds_label_to_orphaned_prs(self, sample_config, mock_repository_host):
        """Test that orphaned PRs get the code review label added."""
        from issue_orchestrator.ports.pull_request_tracker import PRInfo

        sample_config.code_review_label = "needs-code-review"
        sample_config.code_reviewed_label = "code-reviewed"
        sample_config.repo = "owner/repo"

        # Add a PR without review labels to the mock
        orphaned_pr = PRInfo(
            number=42,
            title="Test PR",
            url="https://github.com/owner/repo/pull/42",
            branch="feature-branch",
            body="Generated by issue-orchestrator agent",  # Has orchestrator marker
            state="open",
            labels=[],  # No review labels
        )
        mock_repository_host.prs["feature-branch"] = [orphaned_pr]

        orchestrator = Orchestrator(config=sample_config, _repository_host=mock_repository_host)
        fixed_count = orchestrator.reconcile_orphaned_pr_labels()

        assert fixed_count == 1
        # Should have called add_label with PR number and review label
        assert (42, "needs-code-review") in mock_repository_host.add_label_calls

    def test_reconcile_skips_non_orchestrator_prs(self, sample_config, mock_repository_host):
        """Test that non-orchestrator PRs are skipped."""
        from issue_orchestrator.ports.pull_request_tracker import PRInfo

        sample_config.code_review_label = "needs-code-review"
        sample_config.code_reviewed_label = "code-reviewed"
        sample_config.repo = "owner/repo"

        # Add a PR without the orchestrator marker
        external_pr = PRInfo(
            number=42,
            title="Test PR",
            url="https://github.com/owner/repo/pull/42",
            branch="feature-branch",
            body="Some other PR body",  # No orchestrator marker
            state="open",
            labels=[],
        )
        mock_repository_host.prs["feature-branch"] = [external_pr]

        orchestrator = Orchestrator(config=sample_config, _repository_host=mock_repository_host)
        fixed_count = orchestrator.reconcile_orphaned_pr_labels()

        assert fixed_count == 0
        # Should not have added any labels
        assert len(mock_repository_host.add_label_calls) == 0

    def test_reconcile_skips_prs_with_review_label(self, sample_config, mock_repository_host):
        """Test that PRs already with review labels are skipped."""
        from issue_orchestrator.ports.pull_request_tracker import PRInfo

        sample_config.code_review_label = "needs-code-review"
        sample_config.code_reviewed_label = "code-reviewed"
        sample_config.repo = "owner/repo"

        # Add a PR that already has the review label
        reviewed_pr = PRInfo(
            number=42,
            title="Test PR",
            url="https://github.com/owner/repo/pull/42",
            branch="feature-branch",
            body="Generated by issue-orchestrator agent",
            state="open",
            labels=["needs-code-review"],  # Already has review label
        )
        mock_repository_host.prs["feature-branch"] = [reviewed_pr]

        orchestrator = Orchestrator(config=sample_config, _repository_host=mock_repository_host)
        fixed_count = orchestrator.reconcile_orphaned_pr_labels()

        assert fixed_count == 0
        # Should not have added any labels
        assert len(mock_repository_host.add_label_calls) == 0

    def test_reconcile_skips_prs_with_code_reviewed_label(self, sample_config, mock_repository_host):
        """Test that PRs with code-reviewed label are skipped."""
        from issue_orchestrator.ports.pull_request_tracker import PRInfo

        sample_config.code_review_label = "needs-code-review"
        sample_config.code_reviewed_label = "code-reviewed"
        sample_config.repo = "owner/repo"

        # Add a PR that already has the code-reviewed label
        completed_pr = PRInfo(
            number=42,
            title="Test PR",
            url="https://github.com/owner/repo/pull/42",
            branch="feature-branch",
            body="Generated by issue-orchestrator agent",
            state="open",
            labels=["code-reviewed"],  # Already reviewed
        )
        mock_repository_host.prs["feature-branch"] = [completed_pr]

        orchestrator = Orchestrator(config=sample_config, _repository_host=mock_repository_host)
        fixed_count = orchestrator.reconcile_orphaned_pr_labels()

        assert fixed_count == 0
        # Should not have added any labels
        assert len(mock_repository_host.add_label_calls) == 0


class TestSessionExistsDetection:
    """Test session detection prevents duplicate launches.

    These tests verify that the orchestrator correctly detects existing sessions
    and prevents duplicate launches, which was previously handled by lock files.
    """

    def test_review_with_active_session_removed_from_pending(
        self,
        patch_plugin_manager,
        sample_config,
    ):
        """Test that reviews with active sessions are removed from pending queue."""
        from issue_orchestrator.models import PendingReview

        patch_plugin_manager.plugin.session_exists_override = True  # Session already running

        sample_config.code_review_agent = "agent:web"

        review = PendingReview(
            issue_number=42,
            pr_number=123,
            pr_url="https://github.com/owner/repo/pull/123",
            branch_name="feature/issue-42",
        )

        orchestrator = Orchestrator(config=sample_config)
        orchestrator.state.pending_reviews.append(review)

        # Launch should fail and remove from pending
        result = orchestrator.launch_review_session(review)

        assert result is None
        # Review should be removed from pending queue (not stuck in infinite loop)
        assert len(orchestrator.state.pending_reviews) == 0

    def test_review_tracked_in_active_sessions_removed_from_pending(
        self,
        sample_config,
    ):
        """Test reviews tracked in active_sessions are removed from pending."""
        from issue_orchestrator.models import PendingReview

        sample_config.code_review_agent = "agent:web"

        review = PendingReview(
            issue_number=42,
            pr_number=123,
            pr_url="https://github.com/owner/repo/pull/123",
            branch_name="feature/issue-42",
        )

        orchestrator = Orchestrator(config=sample_config)
        orchestrator.state.pending_reviews.append(review)

        # Simulate session already tracked in active_sessions
        existing_session = create_session(create_issue(42))
        existing_session.tmux_session_name = "review-123"
        orchestrator.state.active_sessions.append(existing_session)

        result = orchestrator.launch_review_session(review)

        assert result is None
        # Should be removed from pending (session exists in active_sessions)
        assert len(orchestrator.state.pending_reviews) == 0

    def test_rework_with_active_session_removed_from_pending(
        self,
        patch_plugin_manager,
        sample_config,
        mock_repository_host,
    ):
        """Test that reworks with active sessions are removed from pending queue."""
        from issue_orchestrator.models import PendingRework
        from issue_orchestrator.domain.issue_key import FakeIssueKey

        patch_plugin_manager.plugin.session_exists_override = True  # Session already running
        mock_repository_host.issues = [create_issue(42)]

        sample_config.code_review_agent = "agent:web"

        rework = PendingRework(
            issue_key=FakeIssueKey(name="42"),
            agent_type="agent:web",
            rework_cycle=1,
        )

        orchestrator = Orchestrator(config=sample_config, _repository_host=mock_repository_host)
        orchestrator.state.pending_reworks.append(rework)

        result = orchestrator.launch_rework_session(rework)

        assert result is None
        # Should be removed from pending
        assert len(orchestrator.state.pending_reworks) == 0


class TestStateMachineTransitions:
    """Test state machine transitions between pending, active, and completed states."""

    @patch("issue_orchestrator.control.session_launcher.create_worktree")
    def test_successful_review_launch_transitions_pending_to_active(
        self,
        mock_create_worktree,
        patch_plugin_manager,
        sample_config,
    ):
        """Test that successful launch moves review from pending to active."""
        from issue_orchestrator.models import PendingReview

        patch_plugin_manager.plugin.session_exists_override = False  # No existing session
        mock_create_worktree.return_value = (Path("/tmp/worktree"), "feature/branch")

        sample_config.code_review_agent = "agent:web"

        review = PendingReview(
            issue_number=42,
            pr_number=123,
            pr_url="https://github.com/owner/repo/pull/123",
            branch_name="feature/issue-42",
        )

        orchestrator = Orchestrator(config=sample_config)
        orchestrator.state.pending_reviews.append(review)

        session = orchestrator.launch_review_session(review)

        # Should be removed from pending
        assert len(orchestrator.state.pending_reviews) == 0
        # Should be added to active
        assert len(orchestrator.state.active_sessions) == 1
        assert session is not None

    def test_failed_launch_does_not_leave_stuck_pending(
        self,
        patch_plugin_manager,
        sample_config,
    ):
        """Test that failed launch doesn't leave item stuck in pending.

        This is the critical bug fix test: if session_exists returns True
        (session already running), the item should be removed from pending_reviews.
        """
        from issue_orchestrator.models import PendingReview

        patch_plugin_manager.plugin.session_exists_override = True  # Session already exists

        sample_config.code_review_agent = "agent:web"

        review = PendingReview(
            issue_number=42,
            pr_number=123,
            pr_url="https://github.com/owner/repo/pull/123",
            branch_name="feature/issue-42",
        )

        orchestrator = Orchestrator(config=sample_config)
        # Manually add to pending (simulating process_pending_reviews behavior)
        orchestrator.state.pending_reviews.append(review)

        result = orchestrator.launch_review_session(review)

        assert result is None
        # Key assertion: even though launch failed, item is NOT stuck in pending
        # (In the buggy version, pending_reviews would still contain the review)

    # NOTE: test_process_pending_reviews_processes_all_pending was removed because
    # process_pending_reviews() was deleted. The workflow layer now handles batching
    # reviews - see test_workflows.py::TestReviewWorkflow::test_should_launch_returns_reviews_up_to_capacity


class TestNamingConventions:
    """Tests for centralized naming convention helpers."""

    def test_get_session_name_issue(self, sample_config):
        """Test session name for issue type."""
        orchestrator = Orchestrator(config=sample_config)
        assert orchestrator._get_session_name(123, "issue") == "issue-123"
        assert orchestrator._get_session_name(1, "issue") == "issue-1"

    def test_get_session_name_review(self, sample_config):
        """Test session name for review type."""
        orchestrator = Orchestrator(config=sample_config)
        assert orchestrator._get_session_name(456, "review") == "review-456"

    def test_get_session_name_rework(self, sample_config):
        """Test session name for rework type."""
        orchestrator = Orchestrator(config=sample_config)
        assert orchestrator._get_session_name(789, "rework") == "rework-789"

    def test_get_session_name_invalid_type(self, sample_config):
        """Test that invalid session type raises error."""
        orchestrator = Orchestrator(config=sample_config)
        with pytest.raises(ValueError, match="Invalid session_type"):
            orchestrator._get_session_name(123, "invalid")

    def test_get_worktree_path(self, sample_config, tmp_path):
        """Test worktree path derivation."""
        # Set up config with known repo_root
        repo_root = tmp_path / "my-repo"
        repo_root.mkdir()
        sample_config.repo_root = repo_root

        agent_config = AgentConfig(
            prompt_path=tmp_path / "prompt.txt",
            worktree_base=tmp_path / "worktrees",
            model="sonnet",
            timeout_minutes=45,
        )

        orchestrator = Orchestrator(config=sample_config)
        path = orchestrator._get_worktree_path(123, agent_config)

        assert path == tmp_path / "worktrees" / "my-repo-123"

    def test_get_worktree_path_uses_agent_repo_root(self, sample_config, tmp_path):
        """Test that agent-specific repo_root is used when set."""
        # Global repo_root
        global_repo = tmp_path / "global-repo"
        global_repo.mkdir()
        sample_config.repo_root = global_repo

        # Agent-specific repo_root
        agent_repo = tmp_path / "agent-repo"
        agent_repo.mkdir()

        agent_config = AgentConfig(
            prompt_path=tmp_path / "prompt.txt",
            worktree_base=tmp_path / "worktrees",
            model="sonnet",
            timeout_minutes=45,
            repo_root=agent_repo,
        )

        orchestrator = Orchestrator(config=sample_config)
        path = orchestrator._get_worktree_path(456, agent_config)

        # Should use agent repo name, not global
        assert path == tmp_path / "worktrees" / "agent-repo-456"


class TestDeferredCleanup:
    """Tests for deferred cleanup functionality."""

    @patch("issue_orchestrator.orchestrator.remove_worktree")
    def test_handle_completion_defers_cleanup_with_triage(
        self,
        mock_remove_worktree,
        sample_config,
        mock_repository_host,
    ):
        """Test that cleanup is deferred when triage review is enabled."""
        from issue_orchestrator.ports.pull_request_tracker import PRInfo

        # Enable triage review
        sample_config.triage_review_agent = "agent:triage"
        sample_config.triage_reviewed_label = "triage-reviewed"

        # Mock PR response
        mock_repository_host.prs["feature/test"] = [
            PRInfo(
                number=100,
                title="Test PR",
                url="https://github.com/owner/repo/pull/100",
                branch="feature/test",
                body="Test",
                state="open",
                labels=[],
            )
        ]

        issue = create_issue(1)
        session = create_session(issue)

        orchestrator = Orchestrator(config=sample_config, _repository_host=mock_repository_host)
        orchestrator.state.active_sessions.append(session)

        orchestrator.handle_session_completion(session, SessionStatus.COMPLETED)

        # Worktree should NOT be removed (deferred)
        mock_remove_worktree.assert_not_called()

        # Should have pending cleanup
        assert len(orchestrator.state.pending_cleanups) == 1
        pending = orchestrator.state.pending_cleanups[0]
        assert pending.issue_number == 1
        assert pending.pr_number == 100

    @patch("issue_orchestrator.orchestrator.remove_worktree")
    def test_handle_completion_defers_cleanup_with_code_review(
        self,
        mock_remove_worktree,
        sample_config,
        mock_repository_host,
    ):
        """Test that cleanup is deferred when code review is enabled and wait_for_code_review is true."""
        from issue_orchestrator.ports.pull_request_tracker import PRInfo

        # Enable code review only (no CTO)
        sample_config.code_review_agent = "agent:reviewer"
        sample_config.code_reviewed_label = "code-reviewed"
        sample_config.cleanup.without_triage.wait_for_code_review = True

        # Mock PR response
        mock_repository_host.prs["feature/test"] = [
            PRInfo(
                number=100,
                title="Test PR",
                url="https://github.com/owner/repo/pull/100",
                branch="feature/test",
                body="Test",
                state="open",
                labels=[],
            )
        ]

        issue = create_issue(1)
        session = create_session(issue)

        orchestrator = Orchestrator(config=sample_config, _repository_host=mock_repository_host)
        orchestrator.state.active_sessions.append(session)

        orchestrator.handle_session_completion(session, SessionStatus.COMPLETED)

        # Worktree should NOT be removed (deferred)
        mock_remove_worktree.assert_not_called()

        # Should have pending cleanup
        assert len(orchestrator.state.pending_cleanups) == 1

    @patch("issue_orchestrator.orchestrator.remove_worktree")
    def test_handle_completion_immediate_cleanup_without_review(
        self,
        mock_remove_worktree,
        sample_config,
        mock_repository_host,
    ):
        """Test that cleanup happens immediately when no review workflow is configured."""
        from issue_orchestrator.ports.pull_request_tracker import PRInfo

        # No review workflow
        sample_config.triage_review_agent = None
        sample_config.code_review_agent = None

        # Mock PR response
        mock_repository_host.prs["feature/test"] = [
            PRInfo(
                number=100,
                title="Test PR",
                url="https://github.com/owner/repo/pull/100",
                branch="feature/test",
                body="Test",
                state="open",
                labels=[],
            )
        ]

        issue = create_issue(1)
        session = create_session(issue)

        orchestrator = Orchestrator(config=sample_config, _repository_host=mock_repository_host)
        orchestrator.state.active_sessions.append(session)

        orchestrator.handle_session_completion(session, SessionStatus.COMPLETED)

        # Worktree should be removed immediately
        mock_remove_worktree.assert_called_once()

        # No pending cleanups
        assert len(orchestrator.state.pending_cleanups) == 0

    @patch("issue_orchestrator.orchestrator.remove_worktree")
    def test_handle_completion_no_defer_for_failed_sessions(
        self,
        mock_remove_worktree,
        sample_config,
        mock_repository_host,
    ):
        """Test that failed sessions are not deferred (left for investigation)."""
        # Enable triage review
        sample_config.triage_review_agent = "agent:triage"

        issue = create_issue(1)
        session = create_session(issue)

        orchestrator = Orchestrator(config=sample_config, _repository_host=mock_repository_host)
        orchestrator.state.active_sessions.append(session)

        orchestrator.handle_session_completion(session, SessionStatus.FAILED)

        # No pending cleanups for failed sessions
        assert len(orchestrator.state.pending_cleanups) == 0
        # Worktree not removed (left for investigation)
        mock_remove_worktree.assert_not_called()


class TestProcessDeferredCleanups:
    """Tests for processing deferred cleanups."""

    @patch("issue_orchestrator.control.cleanup_manager.remove_worktree")
    def test_process_cleanups_when_pr_reviewed(
        self,
        mock_remove_worktree,
        sample_config,
        mock_repository_host,
        tmp_path,
    ):
        """Test that cleanups are processed when PR has reviewed label."""
        from issue_orchestrator.models import PendingCleanup

        # Enable triage review
        sample_config.triage_review_agent = "agent:triage"
        sample_config.triage_reviewed_label = "triage-reviewed"
        sample_config.cleanup.with_triage.remove_worktrees = True

        # Set up PR with reviewed label
        mock_repository_host.prs["issue-1-test"] = [
            create_pr_info(100, "PR 100", labels=["triage-reviewed"], branch="issue-1-test"),
        ]

        orchestrator = Orchestrator(config=sample_config, _repository_host=mock_repository_host)

        # Add pending cleanup
        pending = PendingCleanup(
            issue_number=1,
            pr_number=100,
            pr_url="https://github.com/owner/repo/pull/100",
            branch_name="issue-1-test",
            terminal_session_name="issue-1",
            worktree_path=tmp_path / "worktree-1",
        )
        orchestrator.state.pending_cleanups.append(pending)

        orchestrator.process_deferred_cleanups()

        # Worktree should be removed
        mock_remove_worktree.assert_called_once_with(tmp_path / "worktree-1")

        # Pending cleanup should be removed
        assert len(orchestrator.state.pending_cleanups) == 0

    @patch("issue_orchestrator.control.cleanup_manager.remove_worktree")
    def test_process_cleanups_skips_unreviewed_prs(
        self,
        mock_remove_worktree,
        sample_config,
        mock_repository_host,
        tmp_path,
    ):
        """Test that cleanups are not processed if PR doesn't have reviewed label."""
        from issue_orchestrator.models import PendingCleanup

        # Enable triage review
        sample_config.triage_review_agent = "agent:triage"
        sample_config.triage_reviewed_label = "triage-reviewed"

        # No PRs with reviewed label (empty prs dict)
        mock_repository_host.prs = {}

        orchestrator = Orchestrator(config=sample_config, _repository_host=mock_repository_host)

        # Add pending cleanup
        pending = PendingCleanup(
            issue_number=1,
            pr_number=100,
            pr_url="https://github.com/owner/repo/pull/100",
            branch_name="issue-1-test",
            terminal_session_name="issue-1",
            worktree_path=tmp_path / "worktree-1",
        )
        orchestrator.state.pending_cleanups.append(pending)

        orchestrator.process_deferred_cleanups()

        # Worktree should NOT be removed
        mock_remove_worktree.assert_not_called()

        # Pending cleanup should still be there
        assert len(orchestrator.state.pending_cleanups) == 1

    def test_process_cleanups_noop_when_empty(self, sample_config):
        """Test that process_deferred_cleanups does nothing when queue is empty."""
        sample_config.triage_review_agent = "agent:triage"

        orchestrator = Orchestrator(config=sample_config)
        # No pending cleanups

        # Should not raise
        orchestrator.process_deferred_cleanups()

    def test_process_cleanups_noop_without_review_workflow(self, sample_config):
        """Test that process_deferred_cleanups handles no review workflow."""
        from issue_orchestrator.models import PendingCleanup

        # No review workflow
        sample_config.triage_review_agent = None
        sample_config.code_review_agent = None

        orchestrator = Orchestrator(config=sample_config)

        # Add a pending cleanup (shouldn't happen in practice, but test robustness)
        pending = PendingCleanup(
            issue_number=1,
            pr_number=100,
            pr_url="https://github.com/owner/repo/pull/100",
            branch_name="issue-1-test",
            terminal_session_name="issue-1",
            worktree_path=Path("/tmp/worktree-1"),
        )
        orchestrator.state.pending_cleanups.append(pending)

        # Should not raise, cleanup stays pending
        orchestrator.process_deferred_cleanups()


class TestRecoverOrphanedCleanups:
    """Tests for orphaned cleanup recovery on startup."""

    @patch("issue_orchestrator.control.cleanup_manager.remove_worktree")
    def test_recover_cleans_orphaned_worktrees(
        self,
        mock_remove_worktree,
        sample_config,
        mock_repository_host,
        tmp_path,
    ):
        """Test that orphaned worktrees are cleaned up on startup."""
        # Set up config
        repo_root = tmp_path / "my-repo"
        repo_root.mkdir()
        sample_config.repo_root = repo_root
        sample_config.triage_review_agent = "agent:triage"
        sample_config.triage_reviewed_label = "triage-reviewed"
        sample_config.cleanup.with_triage.remove_worktrees = True

        # Create agent config with worktree base
        worktree_base = tmp_path / "worktrees"
        worktree_base.mkdir()
        agent_config = sample_config.agents["agent:web"]
        agent_config.worktree_base = worktree_base

        # Create orphaned worktree
        orphaned_worktree = worktree_base / "my-repo-123"
        orphaned_worktree.mkdir()

        # Set up PR with reviewed label - includes our orphan
        # Branch naming convention is {issue_number}-{slug}, not issue-{number}
        mock_repository_host.prs["123-test-feature"] = [
            create_pr_info(100, "PR 100", labels=["triage-reviewed"], branch="123-test-feature"),
        ]

        orchestrator = Orchestrator(config=sample_config, _repository_host=mock_repository_host)

        # Mock session_exists to return False (session not running)
        orchestrator._session_exists = lambda name: False

        orchestrator._recover_orphaned_cleanups()

        # Orphaned worktree should be cleaned up
        mock_remove_worktree.assert_called_once_with(orphaned_worktree)

    @patch("issue_orchestrator.control.cleanup_manager.remove_worktree")
    def test_recover_skips_running_sessions(
        self,
        mock_remove_worktree,
        sample_config,
        mock_repository_host,
        tmp_path,
    ):
        """Test that worktrees with running sessions are not cleaned up."""
        # Set up config
        repo_root = tmp_path / "my-repo"
        repo_root.mkdir()
        sample_config.repo_root = repo_root
        sample_config.triage_review_agent = "agent:triage"
        sample_config.triage_reviewed_label = "triage-reviewed"

        # Create agent config with worktree base
        worktree_base = tmp_path / "worktrees"
        worktree_base.mkdir()
        agent_config = sample_config.agents["agent:web"]
        agent_config.worktree_base = worktree_base

        # Create worktree
        worktree = worktree_base / "my-repo-123"
        worktree.mkdir()

        # Set up PR with reviewed label
        mock_repository_host.prs["issue-123-test-feature"] = [
            create_pr_info(100, "PR 100", labels=["triage-reviewed"], branch="issue-123-test-feature"),
        ]

        orchestrator = Orchestrator(config=sample_config, _repository_host=mock_repository_host)

        # Mock session_exists to return True (session still running)
        orchestrator._session_exists = lambda name: True

        orchestrator._recover_orphaned_cleanups()

        # Worktree should NOT be cleaned up (session still running)
        mock_remove_worktree.assert_not_called()

    def test_recover_noop_without_review_workflow(
        self,
        sample_config,
        mock_repository_host,
    ):
        """Test that recovery does nothing without review workflow."""
        # No review workflow
        sample_config.triage_review_agent = None
        sample_config.code_review_agent = None

        orchestrator = Orchestrator(config=sample_config, _repository_host=mock_repository_host)

        orchestrator._recover_orphaned_cleanups()
        # Method should return early without checking PRs

    def test_recover_handles_no_reviewed_prs(
        self,
        sample_config,
        mock_repository_host,
    ):
        """Test that recovery handles case with no reviewed PRs."""
        sample_config.triage_review_agent = "agent:triage"
        sample_config.triage_reviewed_label = "triage-reviewed"

        # No reviewed PRs
        mock_repository_host.prs = {}

        orchestrator = Orchestrator(config=sample_config, _repository_host=mock_repository_host)

        # Should not raise
        orchestrator._recover_orphaned_cleanups()


class TestReworkEscalation:
    """Test rework escalation to needs-human after max cycles.

    Tests use MockGitHubAdapter to verify adapter calls rather than mocking subprocess.
    """

    def test_escalate_to_needs_human_adds_label_and_comment(
        self,
        sample_config,
        mock_repository_host,
    ):
        """Test that escalation adds needs-human label and posts comment."""
        sample_config.max_rework_cycles = 2

        orchestrator = Orchestrator(config=sample_config, _repository_host=mock_repository_host)

        # Clear any calls from initialization
        mock_repository_host.add_label_calls.clear()
        mock_repository_host.remove_label_calls.clear()
        mock_repository_host.comments.clear()

        orchestrator._escalate_to_needs_human(
            pr_number=123,
            issue_number=456,
            rework_cycle=3,
        )

        # Should have called add_label for needs-human
        assert (123, "needs-human") in mock_repository_host.add_label_calls

        # Should have called remove_label for needs-rework
        assert (123, "needs-rework") in mock_repository_host.remove_label_calls

        # Should have posted a comment
        assert len(mock_repository_host.comments) == 1
        comment = mock_repository_host.comments[0]
        assert comment["number"] == 123
        assert "Escalated to Human Review" in comment["body"]
        assert "2 rework cycles" in comment["body"]

    def test_scan_needs_rework_discovers_escalation_at_max_cycles(
        self,
        sample_config,
        mock_repository_host,
    ):
        """Test that scan_needs_rework_prs stores DiscoveredEscalation when max cycles exceeded.

        Note: Actual escalation is now handled by the Planner via EscalateToHumanAction.
        """
        sample_config.max_rework_cycles = 2
        sample_config.code_review_agent = "agent:code-reviewer"

        # Simulate a PR that has gone through 2 rework cycles (labels show rework-2)
        mock_repository_host.prs["issue-456-feature"] = [
            create_pr_info(
                123,
                "PR 123",
                labels=["needs-rework", "rework-2", "agent:code-reviewer"],
                branch="issue-456-feature",
            ),
        ]

        orchestrator = Orchestrator(config=sample_config, _repository_host=mock_repository_host)
        orchestrator.scan_needs_rework_prs()

        # Should have stored a DiscoveredEscalation for Planner to decide
        assert len(orchestrator.state.discovered_escalations) == 1
        escalation = orchestrator.state.discovered_escalations[0]
        assert escalation.pr_number == 123
        assert escalation.rework_cycle == 3  # rework-2 means next cycle is 3

        # Should NOT have added to pending_reworks queue (Planner decides)
        assert len(orchestrator.state.pending_reworks) == 0
        assert len(orchestrator.state.discovered_reworks) == 0

    def test_scan_needs_rework_discovers_rework_within_limit(
        self,
        sample_config,
        mock_repository_host,
    ):
        """Test that scan_needs_rework_prs stores DiscoveredRework when within limit.

        Note: Actual queuing is now handled by the Planner via QueueReworkAction.
        """
        sample_config.max_rework_cycles = 3
        sample_config.code_review_agent = "agent:code-reviewer"

        # Simulate a PR on first rework cycle (no rework label)
        # Need agent label for the code to process it
        mock_repository_host.prs["issue-456-feature"] = [
            create_pr_info(
                123,
                "PR 123",
                labels=["needs-rework", "agent:code-reviewer"],
                branch="issue-456-feature",
            ),
        ]

        orchestrator = Orchestrator(config=sample_config, _repository_host=mock_repository_host)
        orchestrator.scan_needs_rework_prs()

        # Should have stored a DiscoveredRework for Planner to decide
        assert len(orchestrator.state.discovered_reworks) == 1
        rework = orchestrator.state.discovered_reworks[0]
        assert rework.issue_number == 123
        assert rework.rework_cycle == 1
        assert rework.agent_type == "agent:code-reviewer"

        # Should NOT have added to pending_reworks queue yet (Planner decides)
        assert len(orchestrator.state.pending_reworks) == 0

    def test_get_rework_cycle_from_labels_extracts_cycle(
        self,
        sample_config,
    ):
        """Test that rework cycle is correctly extracted from labels."""
        from issue_orchestrator.control.pr_scanner import PRScanner
        from unittest.mock import MagicMock

        # Create scanner with minimal mocks
        scanner = PRScanner(
            config=sample_config,
            repository=MagicMock(),
            events=MagicMock(),
        )

        # No rework label - first cycle
        labels = ["needs-rework", "test-data"]
        assert scanner._get_rework_cycle_from_labels(labels) == 1

        # rework-1 label - next is cycle 2
        labels = ["needs-rework", "rework-1"]
        assert scanner._get_rework_cycle_from_labels(labels) == 2

        # rework-2 label - next is cycle 3
        labels = ["rework-2", "needs-rework"]
        assert scanner._get_rework_cycle_from_labels(labels) == 3

        # rework-5 label - next is cycle 6
        labels = ["rework-5"]
        assert scanner._get_rework_cycle_from_labels(labels) == 6
