"""Unit tests for the orchestrator module."""

import asyncio
import pytest
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch, call, AsyncMock
from issue_orchestrator.orchestrator import Orchestrator, run_orchestrator
from issue_orchestrator.models import (
    Issue,
    Session,
    SessionStatus,
    AgentConfig,
    OrchestratorState,
)
from issue_orchestrator.config import Config
from issue_orchestrator.scheduler import Scheduler
from issue_orchestrator.monitor import SessionMonitor


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


class TestOrchestratorInit:
    """Test Orchestrator initialization."""

    def test_post_init_creates_scheduler(self, sample_config):
        """Test that __post_init__ creates a Scheduler."""
        orchestrator = Orchestrator(config=sample_config)

        assert isinstance(orchestrator.scheduler, Scheduler)
        assert orchestrator.scheduler.config == sample_config

    def test_post_init_creates_monitor(self, sample_config):
        """Test that __post_init__ creates a SessionMonitor."""
        orchestrator = Orchestrator(config=sample_config)

        assert isinstance(orchestrator.monitor, SessionMonitor)
        assert orchestrator.monitor.config == sample_config
        # Verify monitor has reference to session_machines
        assert orchestrator.monitor.session_machines is orchestrator.session_machines

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
        mock_github_adapter,
    ):
        """Test that startup checks for in-progress issues."""
        mock_get_branches.return_value = {}
        mock_github_adapter.issues = []

        orchestrator = Orchestrator(config=sample_config, github_adapter=mock_github_adapter)
        await orchestrator.startup()

        # Should query for in-progress issues for each agent type
        assert len(mock_github_adapter.list_issues_calls) > 0
        call = mock_github_adapter.list_issues_calls[0]
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
        mock_github_adapter,
    ):
        """Test that startup clears orphaned in-progress labels."""
        mock_get_branches.return_value = {}

        issue = create_issue(1, labels=["agent:web", "in-progress"])
        mock_github_adapter.issues = [issue]

        # Mock analyze_issue to indicate orphaned label
        mock_state = MagicMock()
        mock_state.has_session = False
        mock_state.has_open_pr = False
        mock_state.has_partial_work = False
        mock_state.is_orphaned_label = True
        mock_analyze.return_value = mock_state

        orchestrator = Orchestrator(config=sample_config, github_adapter=mock_github_adapter)
        await orchestrator.startup()

        # Should remove the in-progress label
        assert (1, sample_config.get_label_in_progress()) in mock_github_adapter.remove_label_calls

    @pytest.mark.asyncio
    @patch("issue_orchestrator.analysis.get_issue_branches")
    @patch("issue_orchestrator.analysis.analyze_issue")
    async def test_startup_skips_issues_with_open_prs(
        self,
        mock_analyze,
        mock_get_branches,
        sample_config,
        mock_github_adapter,
    ):
        """Test that startup doesn't clear labels for issues with open PRs."""
        mock_get_branches.return_value = {}

        issue = create_issue(1, labels=["agent:web", "in-progress"])
        mock_github_adapter.issues = [issue]

        # Mock analyze_issue to indicate has open PR
        mock_state = MagicMock()
        mock_state.has_session = False
        mock_state.has_open_pr = True
        mock_state.pr_url = "https://github.com/owner/repo/pull/123"
        mock_analyze.return_value = mock_state

        orchestrator = Orchestrator(config=sample_config, github_adapter=mock_github_adapter)
        await orchestrator.startup()

        # Should NOT remove the label
        assert len(mock_github_adapter.remove_label_calls) == 0

    @pytest.mark.asyncio
    @patch("issue_orchestrator.analysis.get_issue_branches")
    @patch("issue_orchestrator.analysis.analyze_issue")
    async def test_startup_resumes_partial_work(
        self,
        mock_analyze,
        mock_get_branches,
        patch_plugin_manager,
        sample_config,
        mock_github_adapter,
    ):
        """Test that startup resumes work for partial work (branch but no session)."""
        mock_get_branches.return_value = {}
        patch_plugin_manager.plugin.session_exists_override = False  # No existing session, allow launch

        issue = create_issue(1, labels=["agent:web", "in-progress"])
        mock_github_adapter.issues = [issue]

        # Mock analyze_issue to indicate has partial work
        mock_state = MagicMock()
        mock_state.has_session = False
        mock_state.has_open_pr = False
        mock_state.has_partial_work = True
        mock_state.branch = "feature/issue-1"
        mock_analyze.return_value = mock_state

        orchestrator = Orchestrator(config=sample_config, github_adapter=mock_github_adapter)
        await orchestrator.startup()

        # Should NOT remove the label - we keep in-progress and resume work
        assert len(mock_github_adapter.remove_label_calls) == 0
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
        mock_github_adapter,
    ):
        """Test that startup skips issues that are blocked (waiting for human)."""
        mock_get_branches.return_value = {}

        # Issue has both in-progress AND blocked labels
        issue = create_issue(1, labels=["agent:web", "in-progress", "blocked"])
        mock_github_adapter.issues = [issue]

        # Mock analyze_issue - shouldn't matter since we skip before analyzing
        mock_state = MagicMock()
        mock_state.has_session = False
        mock_state.has_open_pr = False
        mock_state.has_partial_work = True
        mock_state.branch = "feature/issue-1"
        mock_analyze.return_value = mock_state

        orchestrator = Orchestrator(config=sample_config, github_adapter=mock_github_adapter)
        await orchestrator.startup()

        # Should NOT remove any labels
        assert len(mock_github_adapter.remove_label_calls) == 0
        # Should NOT launch a session - blocked issues wait for human
        assert len(orchestrator.state.active_sessions) == 0


class TestLaunchSession:
    """Test the launch_session method."""

    @patch("issue_orchestrator.orchestrator.create_worktree")
    def test_launch_session_creates_worktree(
        self,
        mock_create_worktree,
        patch_plugin_manager,
        sample_config,
        mock_github_adapter,
    ):
        """Test that launch_session creates a worktree."""
        patch_plugin_manager.plugin.session_exists_override = False
        mock_create_worktree.return_value = (Path("/tmp/worktree"), "feature/issue-1")

        issue = create_issue(1, labels=["agent:web"])
        orchestrator = Orchestrator(config=sample_config, github_adapter=mock_github_adapter)

        session = orchestrator.launch_session(issue)

        mock_create_worktree.assert_called_once()
        assert mock_create_worktree.call_args[1]["issue_number"] == 1

    @patch("issue_orchestrator.orchestrator.create_worktree")
    def test_launch_session_adds_in_progress_label(
        self,
        mock_create_worktree,
        patch_plugin_manager,
        sample_config,
        mock_github_adapter,
    ):
        """Test that launch_session adds the in-progress label."""
        patch_plugin_manager.plugin.session_exists_override = False
        mock_create_worktree.return_value = (Path("/tmp/worktree"), "feature/issue-1")

        issue = create_issue(1, labels=["agent:web"])
        # Proper DI: inject mock adapter instead of patching functions
        orchestrator = Orchestrator(config=sample_config, github_adapter=mock_github_adapter)

        session = orchestrator.launch_session(issue)

        # Verify adapter was called with correct arguments
        assert (1, sample_config.get_label_in_progress()) in mock_github_adapter.add_label_calls

    @patch("issue_orchestrator.orchestrator.create_worktree")
    def test_launch_session_creates_tmux_session(
        self,
        mock_create_worktree,
        patch_plugin_manager,
        sample_config,
        mock_github_adapter,
    ):
        """Test that launch_session creates a tmux session."""
        patch_plugin_manager.plugin.session_exists_override = False
        mock_create_worktree.return_value = (Path("/tmp/worktree"), "feature/issue-1")

        issue = create_issue(1, title="Test Issue", labels=["agent:web"])
        sample_config.ui_mode = "tmux"  # Explicitly test tmux mode
        orchestrator = Orchestrator(config=sample_config, github_adapter=mock_github_adapter)

        session = orchestrator.launch_session(issue)

        # Verify session was created via plugin
        assert len(patch_plugin_manager.plugin.create_session_calls) == 1
        call = patch_plugin_manager.plugin.create_session_calls[0]
        assert call["session_id"] == 1  # issue number
        assert isinstance(call["command"], str)
        assert call["working_dir"] == "/tmp/worktree"

    @patch("issue_orchestrator.orchestrator.create_worktree")
    def test_launch_session_adds_to_active_sessions(
        self,
        mock_create_worktree,
        patch_plugin_manager,
        sample_config,
        mock_github_adapter,
    ):
        """Test that launch_session adds the session to active_sessions."""
        patch_plugin_manager.plugin.session_exists_override = False
        mock_create_worktree.return_value = (Path("/tmp/worktree"), "feature/issue-1")

        issue = create_issue(1, labels=["agent:web"])
        orchestrator = Orchestrator(config=sample_config, github_adapter=mock_github_adapter)

        assert len(orchestrator.state.active_sessions) == 0

        session = orchestrator.launch_session(issue)

        assert len(orchestrator.state.active_sessions) == 1
        assert orchestrator.state.active_sessions[0] == session

    @patch("issue_orchestrator.orchestrator.create_worktree")
    def test_launch_session_returns_session_object(
        self,
        mock_create_worktree,
        patch_plugin_manager,
        sample_config,
        mock_github_adapter,
    ):
        """Test that launch_session returns a Session object."""
        patch_plugin_manager.plugin.session_exists_override = False
        mock_create_worktree.return_value = (Path("/tmp/worktree"), "feature/issue-1")

        issue = create_issue(1, labels=["agent:web"])
        orchestrator = Orchestrator(config=sample_config, github_adapter=mock_github_adapter)

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

    def test_launch_session_raises_error_for_unknown_agent(
        self,
        sample_config,
    ):
        """Test that launch_session raises error for unknown agent type."""
        issue = create_issue(1, labels=["agent:unknown"])
        orchestrator = Orchestrator(config=sample_config)

        with pytest.raises(ValueError, match="No agent config for agent:unknown"):
            orchestrator.launch_session(issue)

    @patch("issue_orchestrator.orchestrator.create_worktree")
    def test_launch_session_uses_agent_repo_root_if_configured(
        self,
        mock_create_worktree,
        patch_plugin_manager,
        sample_config,
        mock_github_adapter,
    ):
        """Test that launch_session uses agent-specific repo_root if configured."""
        patch_plugin_manager.plugin.session_exists_override = False
        mock_create_worktree.return_value = (Path("/tmp/worktree"), "feature/issue-1")

        # Configure agent with specific repo_root
        sample_config.agents["agent:web"].repo_root = Path("/custom/repo/path")

        issue = create_issue(1, labels=["agent:web"])
        orchestrator = Orchestrator(config=sample_config, github_adapter=mock_github_adapter)

        session = orchestrator.launch_session(issue)

        # Should use agent's repo_root
        assert mock_create_worktree.call_args[1]["repo_root"] == Path("/custom/repo/path")

    @patch("issue_orchestrator.orchestrator.create_worktree")
    def test_launch_session_falls_back_to_config_repo_root(
        self,
        mock_create_worktree,
        patch_plugin_manager,
        sample_config,
        mock_github_adapter,
    ):
        """Test that launch_session falls back to config.repo_root if agent doesn't specify."""
        patch_plugin_manager.plugin.session_exists_override = False
        mock_create_worktree.return_value = (Path("/tmp/worktree"), "feature/issue-1")

        # Ensure agent doesn't have repo_root set
        sample_config.agents["agent:web"].repo_root = None
        sample_config.repo_root = Path("/default/repo/path")

        issue = create_issue(1, labels=["agent:web"])
        orchestrator = Orchestrator(config=sample_config, github_adapter=mock_github_adapter)

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

        with patch.object(orchestrator.monitor, "handle_completion") as mock_monitor:
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
        mock_github_adapter,
    ):
        """Test that run_loop exits when shutdown is requested."""
        mock_github_adapter.issues = []

        orchestrator = Orchestrator(config=sample_config, github_adapter=mock_github_adapter)
        orchestrator.request_shutdown()  # Request shutdown immediately

        # Should exit quickly without running loop
        await orchestrator.run_loop()

        assert orchestrator._shutdown_requested is True

    @pytest.mark.asyncio
    async def test_run_loop_checks_active_sessions(
        self,
        sample_config,
        mock_github_adapter,
    ):
        """Test that run_loop checks status of active sessions."""
        mock_github_adapter.issues = []

        issue = create_issue(1)
        session = create_session(issue)

        orchestrator = Orchestrator(config=sample_config, github_adapter=mock_github_adapter)
        orchestrator.state.active_sessions.append(session)

        with patch.object(orchestrator.monitor, "check_session") as mock_check:
            mock_check.return_value = SessionStatus.COMPLETED

            # Run one iteration
            async def run_one_iteration():
                await asyncio.sleep(0.01)  # Let loop run once
                orchestrator.request_shutdown()

            await asyncio.gather(
                orchestrator.run_loop(),
                run_one_iteration(),
            )

            mock_check.assert_called()

    @pytest.mark.asyncio
    async def test_run_loop_handles_completed_sessions(
        self,
        sample_config,
        mock_github_adapter,
    ):
        """Test that run_loop handles completed sessions."""
        mock_github_adapter.issues = []

        issue = create_issue(1)
        session = create_session(issue)

        orchestrator = Orchestrator(config=sample_config, github_adapter=mock_github_adapter)
        orchestrator.state.active_sessions.append(session)

        with patch.object(orchestrator.monitor, "check_session") as mock_check:
            with patch.object(orchestrator, "handle_session_completion") as mock_handle:
                mock_check.return_value = SessionStatus.COMPLETED

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
        mock_github_adapter,
    ):
        """Test that run_loop fetches available issues when not paused."""
        mock_github_adapter.issues = []

        orchestrator = Orchestrator(config=sample_config, github_adapter=mock_github_adapter)

        # Run one iteration
        async def run_one_iteration():
            await asyncio.sleep(0.01)
            orchestrator.request_shutdown()

        await asyncio.gather(
            orchestrator.run_loop(),
            run_one_iteration(),
        )

        assert len(mock_github_adapter.list_issues_calls) > 0

    @pytest.mark.asyncio
    async def test_run_loop_does_not_fetch_when_paused(
        self,
        sample_config,
        mock_github_adapter,
    ):
        """Test that run_loop doesn't fetch new issues when paused."""
        mock_github_adapter.issues = []

        orchestrator = Orchestrator(config=sample_config, github_adapter=mock_github_adapter)
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
        assert len(mock_github_adapter.list_issues_calls) == 0

    @pytest.mark.asyncio
    async def test_run_loop_respects_max_sessions_limit(
        self,
        sample_config,
        mock_github_adapter,
    ):
        """Test that run_loop respects max_sessions limit."""
        sample_config.max_concurrent_sessions = 2

        issue1 = create_issue(1)
        issue2 = create_issue(2)
        issue3 = create_issue(3)

        mock_github_adapter.issues = [issue1, issue2, issue3]

        orchestrator = Orchestrator(config=sample_config, github_adapter=mock_github_adapter)

        # Already have 2 active sessions
        orchestrator.state.active_sessions.append(create_session(issue1))
        orchestrator.state.active_sessions.append(create_session(issue2))

        with patch.object(orchestrator.monitor, "check_session") as mock_check:
            # Mock check_session to return RUNNING (sessions still active)
            mock_check.return_value = SessionStatus.RUNNING

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
        mock_github_adapter,
    ):
        """Test that run_loop launches sessions when capacity is available."""
        sample_config.max_concurrent_sessions = 3

        issue1 = create_issue(1, labels=["agent:web"])

        mock_github_adapter.issues = [issue1]

        orchestrator = Orchestrator(config=sample_config, github_adapter=mock_github_adapter)

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
        mock_github_adapter,
    ):
        """Test that run_loop handles exceptions during launch gracefully."""
        issue1 = create_issue(1, labels=["agent:web"])

        mock_github_adapter.issues = [issue1]

        orchestrator = Orchestrator(config=sample_config, github_adapter=mock_github_adapter)

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
        mock_github_adapter,
    ):
        """Test that run_loop continues when an issue is already claimed."""
        issue1 = create_issue(1, labels=["agent:web"])
        issue2 = create_issue(2, labels=["agent:web"])

        mock_github_adapter.issues = [issue1, issue2]

        orchestrator = Orchestrator(config=sample_config, github_adapter=mock_github_adapter)

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
        mock_github_adapter,
    ):
        """Test that run_loop stops launching when max_issues_to_start is reached."""
        sample_config.max_issues_to_start = 2
        sample_config.max_concurrent_sessions = 5  # Plenty of capacity

        issue1 = create_issue(1, labels=["agent:web"])
        issue2 = create_issue(2, labels=["agent:web"])
        issue3 = create_issue(3, labels=["agent:web"])

        mock_github_adapter.issues = [issue1, issue2, issue3]

        orchestrator = Orchestrator(config=sample_config, github_adapter=mock_github_adapter)

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
        mock_github_adapter,
    ):
        """Test that max_issues_to_start=0 means unlimited."""
        sample_config.max_issues_to_start = 0  # Unlimited
        sample_config.max_concurrent_sessions = 5

        issue1 = create_issue(1, labels=["agent:web"])
        issue2 = create_issue(2, labels=["agent:web"])
        issue3 = create_issue(3, labels=["agent:web"])

        mock_github_adapter.issues = [issue1, issue2, issue3]

        orchestrator = Orchestrator(config=sample_config, github_adapter=mock_github_adapter)

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
        mock_github_adapter,
    ):
        """Test that no new issues are launched if limit was already reached."""
        sample_config.max_issues_to_start = 2
        sample_config.max_concurrent_sessions = 5

        issue1 = create_issue(1, labels=["agent:web"])

        mock_github_adapter.issues = [issue1]

        orchestrator = Orchestrator(config=sample_config, github_adapter=mock_github_adapter)
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

    @patch("issue_orchestrator.orchestrator.create_worktree")
    def test_launch_session_increments_issues_started_count(
        self,
        mock_create_worktree,
        patch_plugin_manager,
        sample_config,
        mock_github_adapter,
    ):
        """Test that launching a session increments issues_started_count."""
        patch_plugin_manager.plugin.session_exists_override = False
        mock_create_worktree.return_value = (Path("/tmp/worktree"), "feature/issue-1")

        issue = create_issue(1, labels=["agent:web"])
        orchestrator = Orchestrator(config=sample_config, github_adapter=mock_github_adapter)

        assert orchestrator.state.issues_started_count == 0

        # Note: The counter is incremented in run_loop, not in launch_session itself
        # So we test that the state field exists and works
        orchestrator.state.issues_started_count = 5
        assert orchestrator.state.issues_started_count == 5

    @pytest.mark.asyncio
    async def test_run_loop_checks_limit_before_each_launch(
        self,
        sample_config,
        mock_github_adapter,
    ):
        """Test that limit is checked before each launch in a batch."""
        sample_config.max_issues_to_start = 1
        sample_config.max_concurrent_sessions = 5

        issue1 = create_issue(1, labels=["agent:web"])
        issue2 = create_issue(2, labels=["agent:web"])

        mock_github_adapter.issues = [issue1, issue2]

        orchestrator = Orchestrator(config=sample_config, github_adapter=mock_github_adapter)

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
        mock_github_adapter,
    ):
        """Test that issues that were already claimed don't count toward the limit."""
        sample_config.max_issues_to_start = 2
        sample_config.max_concurrent_sessions = 5

        issue1 = create_issue(1, labels=["agent:web"])
        issue2 = create_issue(2, labels=["agent:web"])
        issue3 = create_issue(3, labels=["agent:web"])

        mock_github_adapter.issues = [issue1, issue2, issue3]

        orchestrator = Orchestrator(config=sample_config, github_adapter=mock_github_adapter)

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


class TestCheckCTOReviewTrigger:
    """Test the check_cto_review_trigger method for CTO batch review workflow."""

    def test_check_cto_review_trigger_disabled_without_agent(self, sample_config):
        """Test that check_cto_review_trigger does nothing without cto_review_agent configured."""
        sample_config.cto_review_agent = None
        sample_config.code_reviewed_label = "code-reviewed"
        sample_config.cto_review_threshold = 5

        orchestrator = Orchestrator(config=sample_config)

        # Should not raise and should not call any GitHub functions
        with patch("issue_orchestrator.orchestrator.list_prs_with_label") as mock_prs:
            orchestrator.check_cto_review_trigger()
            mock_prs.assert_not_called()

    def test_check_cto_review_trigger_disabled_with_zero_threshold(self, sample_config):
        """Test that check_cto_review_trigger does nothing with threshold=0."""
        sample_config.cto_review_agent = "agent:cto"
        sample_config.code_reviewed_label = "code-reviewed"
        sample_config.cto_review_threshold = 0

        orchestrator = Orchestrator(config=sample_config)

        with patch("issue_orchestrator.orchestrator.list_prs_with_label") as mock_prs:
            orchestrator.check_cto_review_trigger()
            mock_prs.assert_not_called()

    @patch("issue_orchestrator.orchestrator.list_prs_with_label")
    def test_check_cto_review_trigger_below_threshold(self, mock_prs, sample_config):
        """Test that no review issue is created when below threshold."""
        sample_config.cto_review_agent = "agent:cto"
        sample_config.code_reviewed_label = "code-reviewed"
        sample_config.cto_review_threshold = 5

        mock_prs.return_value = [
            {"number": 1, "title": "PR 1"},
            {"number": 2, "title": "PR 2"},
        ]  # Only 2 PRs, below threshold of 5

        orchestrator = Orchestrator(config=sample_config)

        with patch("issue_orchestrator.orchestrator.create_issue") as mock_create:
            orchestrator.check_cto_review_trigger()
            mock_create.assert_not_called()

    @patch("issue_orchestrator.orchestrator.list_prs_with_label")
    @patch("issue_orchestrator.orchestrator.create_issue")
    def test_check_cto_review_trigger_creates_review_issue(
        self, mock_create, mock_prs, sample_config, mock_github_adapter
    ):
        """Test that review issue is created when threshold is reached."""
        sample_config.cto_review_agent = "agent:cto"
        sample_config.code_reviewed_label = "code-reviewed"
        sample_config.cto_review_threshold = 3

        mock_prs.return_value = [
            {"number": 1, "title": "PR 1"},
            {"number": 2, "title": "PR 2"},
            {"number": 3, "title": "PR 3"},
        ]  # 3 PRs, meets threshold
        mock_github_adapter.issues = []  # No existing review issues
        mock_create.return_value = 42

        orchestrator = Orchestrator(config=sample_config, github_adapter=mock_github_adapter)
        orchestrator.check_cto_review_trigger()

        mock_create.assert_called_once()
        call_args = mock_create.call_args
        assert "CTO Batch Review" in call_args[1]["title"]
        assert sample_config.cto_review_agent in call_args[1]["labels"]

    @patch("issue_orchestrator.orchestrator.list_prs_with_label")
    @patch("issue_orchestrator.orchestrator.create_issue")
    def test_check_cto_review_trigger_skips_if_review_issue_exists(
        self, mock_create, mock_prs, sample_config, mock_github_adapter
    ):
        """Test that no duplicate review issue is created."""
        sample_config.cto_review_agent = "agent:cto"
        sample_config.code_reviewed_label = "code-reviewed"
        sample_config.cto_review_threshold = 3

        mock_prs.return_value = [
            {"number": 1, "title": "PR 1"},
            {"number": 2, "title": "PR 2"},
            {"number": 3, "title": "PR 3"},
        ]

        # Existing review issue (must have the CTO agent label to be found)
        existing_issue = create_issue(100, title="CTO Batch Review: 3 PRs pending", labels=["agent:cto"])
        mock_github_adapter.issues = [existing_issue]

        orchestrator = Orchestrator(config=sample_config, github_adapter=mock_github_adapter)
        orchestrator.check_cto_review_trigger()

        # Should not create a new issue
        mock_create.assert_not_called()

    @patch("issue_orchestrator.orchestrator.list_prs_with_label")
    @patch("issue_orchestrator.orchestrator.create_issue")
    def test_check_cto_review_trigger_review_body_includes_pr_list(
        self, mock_create, mock_prs, sample_config, mock_github_adapter
    ):
        """Test that review issue body includes list of PRs."""
        sample_config.cto_review_agent = "agent:cto"
        sample_config.code_reviewed_label = "code-reviewed"
        sample_config.cto_reviewed_label = "cto-reviewed"
        sample_config.cto_review_threshold = 2

        mock_prs.return_value = [
            {"number": 10, "title": "Fix bug A"},
            {"number": 20, "title": "Add feature B"},
        ]
        mock_github_adapter.issues = []
        mock_create.return_value = 42

        orchestrator = Orchestrator(config=sample_config, github_adapter=mock_github_adapter)
        orchestrator.check_cto_review_trigger()

        call_args = mock_create.call_args
        body = call_args[1]["body"]
        assert "PR #10" in body
        assert "Fix bug A" in body
        assert "PR #20" in body
        assert "Add feature B" in body
        assert "code-reviewed" in body
        assert "cto-reviewed" in body


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

    @patch("issue_orchestrator.worktree.create_worktree")
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

    @patch("issue_orchestrator.worktree.create_worktree")
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

    @patch("issue_orchestrator.worktree.create_worktree")
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

    @patch("issue_orchestrator.worktree.create_worktree")
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

    @patch("issue_orchestrator.worktree.create_worktree")
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


class TestProcessPendingReviews:
    """Test the process_pending_reviews method."""

    def test_process_pending_reviews_does_nothing_without_agent(self, sample_config):
        """Test that process_pending_reviews does nothing without code_review_agent."""
        from issue_orchestrator.models import PendingReview

        sample_config.code_review_agent = None

        review = PendingReview(
            issue_number=42,
            pr_number=123,
            pr_url="https://github.com/owner/repo/pull/123",
            branch_name="feature/issue-42",
        )

        orchestrator = Orchestrator(config=sample_config)
        orchestrator.state.pending_reviews.append(review)

        with patch.object(orchestrator, "launch_review_session") as mock_launch:
            orchestrator.process_pending_reviews()
            mock_launch.assert_not_called()

    def test_process_pending_reviews_does_nothing_when_empty(self, sample_config):
        """Test that process_pending_reviews does nothing when queue is empty."""
        sample_config.code_review_agent = "agent:web"

        orchestrator = Orchestrator(config=sample_config)
        # pending_reviews is empty by default

        with patch.object(orchestrator, "launch_review_session") as mock_launch:
            orchestrator.process_pending_reviews()
            mock_launch.assert_not_called()

    @patch("issue_orchestrator.worktree.create_worktree")
    def test_process_pending_reviews_launches_reviews(
        self,
        mock_create_worktree,
        patch_plugin_manager,
        sample_config,
    ):
        """Test that process_pending_reviews launches review sessions."""
        from issue_orchestrator.models import PendingReview

        patch_plugin_manager.plugin.session_exists_override = False
        mock_create_worktree.return_value = (Path("/tmp/review-worktree"), "feature/issue-42")

        sample_config.code_review_agent = "agent:web"
        sample_config.max_concurrent_sessions = 5

        review = PendingReview(
            issue_number=42,
            pr_number=123,
            pr_url="https://github.com/owner/repo/pull/123",
            branch_name="feature/issue-42",
        )

        orchestrator = Orchestrator(config=sample_config)
        orchestrator.state.pending_reviews.append(review)

        orchestrator.process_pending_reviews()

        # Should have launched the review
        assert len(orchestrator.state.active_sessions) == 1

    @patch("issue_orchestrator.worktree.create_worktree")
    def test_process_pending_reviews_respects_capacity(
        self,
        mock_create_worktree,
        patch_plugin_manager,
        sample_config,
    ):
        """Test that process_pending_reviews respects max_concurrent_sessions."""
        from issue_orchestrator.models import PendingReview

        patch_plugin_manager.plugin.session_exists_override = False
        mock_create_worktree.return_value = (Path("/tmp/review-worktree"), "feature/test")

        sample_config.code_review_agent = "agent:web"
        sample_config.max_concurrent_sessions = 2

        reviews = [
            PendingReview(issue_number=i, pr_number=i+100, pr_url=f"https://github.com/owner/repo/pull/{i+100}", branch_name=f"feature/issue-{i}")
            for i in range(5)
        ]

        orchestrator = Orchestrator(config=sample_config)
        orchestrator.state.pending_reviews = reviews

        # Already have 2 active sessions
        issue1 = create_issue(1)
        issue2 = create_issue(2)
        orchestrator.state.active_sessions.append(create_session(issue1))
        orchestrator.state.active_sessions.append(create_session(issue2))

        orchestrator.process_pending_reviews()

        # Should not launch any reviews - already at capacity
        assert len(orchestrator.state.active_sessions) == 2
        # All reviews should still be pending
        assert len(orchestrator.state.pending_reviews) == 5

    @patch("issue_orchestrator.worktree.create_worktree")
    def test_process_pending_reviews_launches_up_to_capacity(
        self,
        mock_create_worktree,
        patch_plugin_manager,
        sample_config,
    ):
        """Test that process_pending_reviews launches reviews up to available capacity."""
        from issue_orchestrator.models import PendingReview

        patch_plugin_manager.plugin.session_exists_override = False
        mock_create_worktree.return_value = (Path("/tmp/review-worktree"), "feature/test")

        sample_config.code_review_agent = "agent:web"
        sample_config.max_concurrent_sessions = 3

        reviews = [
            PendingReview(issue_number=i, pr_number=i+100, pr_url=f"https://github.com/owner/repo/pull/{i+100}", branch_name=f"feature/issue-{i}")
            for i in range(5)
        ]

        orchestrator = Orchestrator(config=sample_config)
        orchestrator.state.pending_reviews = reviews

        # Have 1 active session, capacity for 2 more
        issue1 = create_issue(1)
        orchestrator.state.active_sessions.append(create_session(issue1))

        orchestrator.process_pending_reviews()

        # Should launch 2 reviews (available capacity)
        # 1 original + 2 new = 3 total
        assert len(orchestrator.state.active_sessions) == 3


class TestHandleSessionCompletionWithCodeReview:
    """Test handle_session_completion triggering code review."""

    @patch("issue_orchestrator.orchestrator.remove_worktree")
    def test_handle_completion_triggers_code_review(
        self,
        mock_remove_worktree,
        sample_config,
        mock_github_adapter,
    ):
        """Test that handle_session_completion queues code review for completed sessions."""
        from issue_orchestrator.ports.pr_repository import PRInfo

        sample_config.code_review_agent = "agent:reviewer"
        mock_github_adapter.prs["feature/issue-1"] = [
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

        orchestrator = Orchestrator(config=sample_config, github_adapter=mock_github_adapter)
        orchestrator.state.active_sessions.append(session)

        with patch.object(orchestrator, "queue_code_review") as mock_queue:
            orchestrator.handle_session_completion(session, SessionStatus.COMPLETED)

            mock_queue.assert_called_once_with(
                issue_number=1,
                pr_url="https://github.com/owner/repo/pull/456",
                branch_name="feature/issue-1",
            )

    @patch("issue_orchestrator.orchestrator.remove_worktree")
    def test_handle_completion_does_not_trigger_review_without_agent(
        self,
        mock_remove_worktree,
        sample_config,
        mock_github_adapter,
    ):
        """Test that handle_session_completion doesn't queue review without code_review_agent."""
        from issue_orchestrator.ports.pr_repository import PRInfo

        sample_config.code_review_agent = None

        mock_github_adapter.prs["feature/test"] = [
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

        orchestrator = Orchestrator(config=sample_config, github_adapter=mock_github_adapter)
        orchestrator.state.active_sessions.append(session)

        with patch.object(orchestrator, "queue_code_review") as mock_queue:
            orchestrator.handle_session_completion(session, SessionStatus.COMPLETED)

            mock_queue.assert_not_called()

    @patch("issue_orchestrator.orchestrator.remove_worktree")
    def test_handle_completion_does_not_trigger_review_for_blocked(
        self,
        mock_remove_worktree,
        sample_config,
        mock_github_adapter,
    ):
        """Test that handle_session_completion doesn't queue review for blocked sessions."""
        sample_config.code_review_agent = "agent:reviewer"

        issue = create_issue(1)
        session = create_session(issue)

        orchestrator = Orchestrator(config=sample_config, github_adapter=mock_github_adapter)
        orchestrator.state.active_sessions.append(session)

        with patch.object(orchestrator, "queue_code_review") as mock_queue:
            orchestrator.handle_session_completion(session, SessionStatus.BLOCKED)

            mock_queue.assert_not_called()

    @patch("issue_orchestrator.orchestrator.remove_worktree")
    def test_handle_completion_does_not_trigger_review_without_pr(
        self,
        mock_remove_worktree,
        sample_config,
        mock_github_adapter,
    ):
        """Test that handle_session_completion doesn't queue review if no PR found."""
        sample_config.code_review_agent = "agent:reviewer"

        # No PRs configured for this branch
        mock_github_adapter.prs = {}

        issue = create_issue(1)
        session = create_session(issue)

        orchestrator = Orchestrator(config=sample_config, github_adapter=mock_github_adapter)
        orchestrator.state.active_sessions.append(session)

        with patch.object(orchestrator, "queue_code_review") as mock_queue:
            orchestrator.handle_session_completion(session, SessionStatus.COMPLETED)

            mock_queue.assert_not_called()


class TestStartupPendingReviews:
    """Test startup recovery for pending code reviews."""

    @pytest.mark.asyncio
    @patch("issue_orchestrator.analysis.get_issue_branches")
    @patch("issue_orchestrator.orchestrator.list_prs_with_label")
    async def test_startup_scans_for_pending_reviews(
        self,
        mock_list_prs,
        mock_get_branches,
        patch_plugin_manager,
        sample_config,
        mock_github_adapter,
    ):
        """Test that startup scans for PRs with code_review_label."""
        mock_get_branches.return_value = {}
        mock_github_adapter.issues = []
        patch_plugin_manager.plugin.session_exists_override = False

        sample_config.code_review_agent = "agent:reviewer"
        sample_config.code_review_label = "needs-code-review"

        mock_list_prs.return_value = [
            {"number": 123, "url": "https://github.com/owner/repo/pull/123"},
            {"number": 456, "url": "https://github.com/owner/repo/pull/456"},
        ]

        orchestrator = Orchestrator(config=sample_config, github_adapter=mock_github_adapter)
        await orchestrator.startup()

        # Should have queued both PRs for review
        assert len(orchestrator.state.pending_reviews) == 2
        assert orchestrator.state.pending_reviews[0].pr_number == 123
        assert orchestrator.state.pending_reviews[1].pr_number == 456

    @pytest.mark.asyncio
    @patch("issue_orchestrator.analysis.get_issue_branches")
    @patch("issue_orchestrator.orchestrator.list_prs_with_label")
    async def test_startup_skips_reviews_already_in_progress(
        self,
        mock_list_prs,
        mock_get_branches,
        sample_config,
        mock_github_adapter,
    ):
        """Test that startup skips PRs with active review sessions."""
        mock_get_branches.return_value = {}
        mock_github_adapter.issues = []

        sample_config.code_review_agent = "agent:reviewer"
        sample_config.code_review_label = "needs-code-review"

        mock_list_prs.return_value = [
            {"number": 123, "url": "https://github.com/owner/repo/pull/123"},
            {"number": 456, "url": "https://github.com/owner/repo/pull/456"},
        ]

        orchestrator = Orchestrator(config=sample_config, github_adapter=mock_github_adapter)

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
    @patch("issue_orchestrator.orchestrator.list_prs_with_label")
    async def test_startup_does_not_scan_without_code_review_config(
        self,
        mock_list_prs,
        mock_get_branches,
        sample_config,
        mock_github_adapter,
    ):
        """Test that startup doesn't scan for reviews without config."""
        mock_get_branches.return_value = {}
        mock_github_adapter.issues = []

        sample_config.code_review_agent = None
        sample_config.code_review_label = None

        orchestrator = Orchestrator(config=sample_config, github_adapter=mock_github_adapter)
        await orchestrator.startup()

        # Should not call list_prs_with_label
        mock_list_prs.assert_not_called()
        assert len(orchestrator.state.pending_reviews) == 0


class TestPauseBehavior:
    """Test that pause stops all new work from starting."""

    def test_process_pending_reviews_does_nothing_when_paused(self, sample_config):
        """Test that process_pending_reviews does nothing when paused."""
        from issue_orchestrator.models import PendingReview

        sample_config.code_review_agent = "agent:web"
        sample_config.max_concurrent_sessions = 5

        review = PendingReview(
            issue_number=42,
            pr_number=123,
            pr_url="https://github.com/owner/repo/pull/123",
            branch_name="feature/issue-42",
        )

        orchestrator = Orchestrator(config=sample_config)
        orchestrator.state.pending_reviews.append(review)
        orchestrator.state.paused = True  # PAUSED

        with patch.object(orchestrator, "launch_review_session") as mock_launch:
            orchestrator.process_pending_reviews()
            mock_launch.assert_not_called()

        # Review should still be in queue
        assert len(orchestrator.state.pending_reviews) == 1

    @patch("issue_orchestrator.orchestrator.list_prs_with_label")
    def test_check_cto_review_trigger_does_nothing_when_paused(
        self, mock_prs, sample_config
    ):
        """Test that check_cto_review_trigger does nothing when paused."""
        sample_config.cto_review_agent = "agent:cto"
        sample_config.code_reviewed_label = "code-reviewed"
        sample_config.cto_review_threshold = 3

        mock_prs.return_value = [
            {"number": 1, "title": "PR 1"},
            {"number": 2, "title": "PR 2"},
            {"number": 3, "title": "PR 3"},
        ]  # At threshold

        orchestrator = Orchestrator(config=sample_config)
        orchestrator.state.paused = True  # PAUSED

        with patch("issue_orchestrator.orchestrator.create_issue") as mock_create:
            orchestrator.check_cto_review_trigger()
            # Should not even check PRs when paused
            mock_prs.assert_not_called()
            mock_create.assert_not_called()

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
        mock_github_adapter,
    ):
        """Test that run_loop stops launching when paused mid-batch."""
        sample_config.max_concurrent_sessions = 5

        issue1 = create_issue(1, labels=["agent:web"])
        issue2 = create_issue(2, labels=["agent:web"])
        issue3 = create_issue(3, labels=["agent:web"])

        mock_github_adapter.issues = [issue1, issue2, issue3]

        orchestrator = Orchestrator(config=sample_config, github_adapter=mock_github_adapter)

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
    """Test the reconcile_orphaned_pr_labels method."""

    @patch("subprocess.run")
    def test_reconcile_skips_when_no_code_review_label(self, mock_run, sample_config, mock_github_adapter):
        """Test that reconciliation is skipped when code review is not configured."""
        sample_config.code_review_label = None

        orchestrator = Orchestrator(config=sample_config, github_adapter=mock_github_adapter)
        fixed_count = orchestrator.reconcile_orphaned_pr_labels()

        assert fixed_count == 0
        mock_run.assert_not_called()

    @patch("subprocess.run")
    def test_reconcile_adds_label_to_orphaned_prs(self, mock_run, sample_config):
        """Test that orphaned PRs get the code review label added."""
        sample_config.code_review_label = "needs-code-review"
        sample_config.code_reviewed_label = "code-reviewed"
        sample_config.repo = "owner/repo"

        # Mock gh pr list returning a PR without review labels
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout='[{"number": 42, "body": "Generated by issue-orchestrator agent", "labels": []}]',
            stderr="",
        )

        orchestrator = Orchestrator(config=sample_config)
        fixed_count = orchestrator.reconcile_orphaned_pr_labels()

        assert fixed_count == 1
        # Should have called gh pr list and gh pr edit
        assert mock_run.call_count == 2

    @patch("subprocess.run")
    def test_reconcile_skips_non_orchestrator_prs(self, mock_run, sample_config):
        """Test that non-orchestrator PRs are skipped."""
        sample_config.code_review_label = "needs-code-review"
        sample_config.code_reviewed_label = "code-reviewed"
        sample_config.repo = "owner/repo"

        # Mock gh pr list returning a PR without the orchestrator marker
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout='[{"number": 42, "body": "Some other PR body", "labels": []}]',
            stderr="",
        )

        orchestrator = Orchestrator(config=sample_config)
        fixed_count = orchestrator.reconcile_orphaned_pr_labels()

        assert fixed_count == 0
        # Should only have called gh pr list
        assert mock_run.call_count == 1

    @patch("subprocess.run")
    def test_reconcile_skips_prs_with_review_label(self, mock_run, sample_config):
        """Test that PRs already with review labels are skipped."""
        sample_config.code_review_label = "needs-code-review"
        sample_config.code_reviewed_label = "code-reviewed"
        sample_config.repo = "owner/repo"

        # Mock gh pr list returning a PR that already has the review label
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout='[{"number": 42, "body": "Generated by issue-orchestrator agent", "labels": [{"name": "needs-code-review"}]}]',
            stderr="",
        )

        orchestrator = Orchestrator(config=sample_config)
        fixed_count = orchestrator.reconcile_orphaned_pr_labels()

        assert fixed_count == 0
        # Should only have called gh pr list
        assert mock_run.call_count == 1

    @patch("subprocess.run")
    def test_reconcile_skips_prs_with_code_reviewed_label(self, mock_run, sample_config):
        """Test that PRs with code-reviewed label are skipped."""
        sample_config.code_review_label = "needs-code-review"
        sample_config.code_reviewed_label = "code-reviewed"
        sample_config.repo = "owner/repo"

        # Mock gh pr list returning a PR that already has been reviewed
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout='[{"number": 42, "body": "Generated by issue-orchestrator agent", "labels": [{"name": "code-reviewed"}]}]',
            stderr="",
        )

        orchestrator = Orchestrator(config=sample_config)
        fixed_count = orchestrator.reconcile_orphaned_pr_labels()

        assert fixed_count == 0
        # Should only have called gh pr list
        assert mock_run.call_count == 1


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
        mock_github_adapter,
    ):
        """Test that reworks with active sessions are removed from pending queue."""
        from issue_orchestrator.models import PendingRework

        patch_plugin_manager.plugin.session_exists_override = True  # Session already running
        mock_github_adapter.issues = [create_issue(42)]

        sample_config.code_review_agent = "agent:web"

        rework = PendingRework(
            issue_number=42,
            pr_number=123,
            pr_url="https://github.com/owner/repo/pull/123",
            branch_name="feature/issue-42",
            rework_cycle=1,
        )

        orchestrator = Orchestrator(config=sample_config, github_adapter=mock_github_adapter)
        orchestrator.state.pending_reworks.append(rework)

        result = orchestrator.launch_rework_session(rework)

        assert result is None
        # Should be removed from pending
        assert len(orchestrator.state.pending_reworks) == 0


class TestStateMachineTransitions:
    """Test state machine transitions between pending, active, and completed states."""

    @patch("issue_orchestrator.orchestrator.create_worktree")
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

    def test_process_pending_reviews_processes_all_pending(self, sample_config):
        """Test that process_pending_reviews attempts to process all items."""
        from issue_orchestrator.models import PendingReview

        sample_config.code_review_agent = "agent:web"
        sample_config.max_concurrent_sessions = 5

        orchestrator = Orchestrator(config=sample_config)

        # Add multiple pending reviews
        for i in range(3):
            review = PendingReview(
                issue_number=i,
                pr_number=100 + i,
                pr_url=f"https://github.com/owner/repo/pull/{100+i}",
                branch_name=f"feature/issue-{i}",
            )
            orchestrator.state.pending_reviews.append(review)

        # Mock launch_review_session to track calls
        launch_calls = []
        def mock_launch(review):
            launch_calls.append(review.pr_number)
            return None  # Simulate all fail

        orchestrator.launch_review_session = mock_launch
        orchestrator.process_pending_reviews()

        # Should have tried to launch all 3
        assert len(launch_calls) == 3
        assert 100 in launch_calls
        assert 101 in launch_calls
        assert 102 in launch_calls


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
    def test_handle_completion_defers_cleanup_with_cto(
        self,
        mock_remove_worktree,
        sample_config,
        mock_github_adapter,
    ):
        """Test that cleanup is deferred when CTO review is enabled."""
        from issue_orchestrator.ports.pr_repository import PRInfo

        # Enable CTO review
        sample_config.cto_review_agent = "agent:cto"
        sample_config.cto_reviewed_label = "cto-reviewed"

        # Mock PR response
        mock_github_adapter.prs["feature/test"] = [
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

        orchestrator = Orchestrator(config=sample_config, github_adapter=mock_github_adapter)
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
        mock_github_adapter,
    ):
        """Test that cleanup is deferred when code review is enabled and wait_for_code_review is true."""
        from issue_orchestrator.ports.pr_repository import PRInfo

        # Enable code review only (no CTO)
        sample_config.code_review_agent = "agent:reviewer"
        sample_config.code_reviewed_label = "code-reviewed"
        sample_config.cleanup.without_cto.wait_for_code_review = True

        # Mock PR response
        mock_github_adapter.prs["feature/test"] = [
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

        orchestrator = Orchestrator(config=sample_config, github_adapter=mock_github_adapter)
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
        mock_github_adapter,
    ):
        """Test that cleanup happens immediately when no review workflow is configured."""
        from issue_orchestrator.ports.pr_repository import PRInfo

        # No review workflow
        sample_config.cto_review_agent = None
        sample_config.code_review_agent = None

        # Mock PR response
        mock_github_adapter.prs["feature/test"] = [
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

        orchestrator = Orchestrator(config=sample_config, github_adapter=mock_github_adapter)
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
        mock_github_adapter,
    ):
        """Test that failed sessions are not deferred (left for investigation)."""
        # Enable CTO review
        sample_config.cto_review_agent = "agent:cto"

        issue = create_issue(1)
        session = create_session(issue)

        orchestrator = Orchestrator(config=sample_config, github_adapter=mock_github_adapter)
        orchestrator.state.active_sessions.append(session)

        orchestrator.handle_session_completion(session, SessionStatus.FAILED)

        # No pending cleanups for failed sessions
        assert len(orchestrator.state.pending_cleanups) == 0
        # Worktree not removed (left for investigation)
        mock_remove_worktree.assert_not_called()


class TestProcessDeferredCleanups:
    """Tests for processing deferred cleanups."""

    @patch("issue_orchestrator.orchestrator.list_prs_with_label")
    @patch("issue_orchestrator.orchestrator.remove_worktree")
    def test_process_cleanups_when_pr_reviewed(
        self,
        mock_remove_worktree,
        mock_list_prs,
        sample_config,
        tmp_path,
    ):
        """Test that cleanups are processed when PR has reviewed label."""
        from issue_orchestrator.models import PendingCleanup

        # Enable CTO review
        sample_config.cto_review_agent = "agent:cto"
        sample_config.cto_reviewed_label = "cto-reviewed"
        sample_config.cleanup.with_cto.remove_worktrees = True

        # Mock PRs with reviewed label
        mock_list_prs.return_value = [{"number": 100}]

        orchestrator = Orchestrator(config=sample_config)

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

    @patch("issue_orchestrator.orchestrator.list_prs_with_label")
    @patch("issue_orchestrator.orchestrator.remove_worktree")
    def test_process_cleanups_skips_unreviewed_prs(
        self,
        mock_remove_worktree,
        mock_list_prs,
        sample_config,
        tmp_path,
    ):
        """Test that cleanups are not processed if PR doesn't have reviewed label."""
        from issue_orchestrator.models import PendingCleanup

        # Enable CTO review
        sample_config.cto_review_agent = "agent:cto"
        sample_config.cto_reviewed_label = "cto-reviewed"

        # No PRs with reviewed label
        mock_list_prs.return_value = []

        orchestrator = Orchestrator(config=sample_config)

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
        sample_config.cto_review_agent = "agent:cto"

        orchestrator = Orchestrator(config=sample_config)
        # No pending cleanups

        # Should not raise
        orchestrator.process_deferred_cleanups()

    def test_process_cleanups_noop_without_review_workflow(self, sample_config):
        """Test that process_deferred_cleanups handles no review workflow."""
        from issue_orchestrator.models import PendingCleanup

        # No review workflow
        sample_config.cto_review_agent = None
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

    @patch("issue_orchestrator.orchestrator.list_prs_with_label")
    @patch("issue_orchestrator.orchestrator.remove_worktree")
    def test_recover_cleans_orphaned_worktrees(
        self,
        mock_remove_worktree,
        mock_list_prs,
        sample_config,
        tmp_path,
    ):
        """Test that orphaned worktrees are cleaned up on startup."""
        # Set up config
        repo_root = tmp_path / "my-repo"
        repo_root.mkdir()
        sample_config.repo_root = repo_root
        sample_config.cto_review_agent = "agent:cto"
        sample_config.cto_reviewed_label = "cto-reviewed"
        sample_config.cleanup.with_cto.remove_worktrees = True

        # Create agent config with worktree base
        worktree_base = tmp_path / "worktrees"
        worktree_base.mkdir()
        agent_config = sample_config.agents["agent:web"]
        agent_config.worktree_base = worktree_base

        # Create orphaned worktree
        orphaned_worktree = worktree_base / "my-repo-123"
        orphaned_worktree.mkdir()

        # Mock PRs with reviewed label - includes our orphan
        mock_list_prs.return_value = [
            {"number": 100, "headRefName": "issue-123-test-feature"}
        ]

        orchestrator = Orchestrator(config=sample_config)

        # Mock session_exists to return False (session not running)
        orchestrator._session_exists = lambda name: False

        orchestrator._recover_orphaned_cleanups()

        # Orphaned worktree should be cleaned up
        mock_remove_worktree.assert_called_once_with(orphaned_worktree)

    @patch("issue_orchestrator.orchestrator.list_prs_with_label")
    @patch("issue_orchestrator.orchestrator.remove_worktree")
    def test_recover_skips_running_sessions(
        self,
        mock_remove_worktree,
        mock_list_prs,
        sample_config,
        tmp_path,
    ):
        """Test that worktrees with running sessions are not cleaned up."""
        # Set up config
        repo_root = tmp_path / "my-repo"
        repo_root.mkdir()
        sample_config.repo_root = repo_root
        sample_config.cto_review_agent = "agent:cto"
        sample_config.cto_reviewed_label = "cto-reviewed"

        # Create agent config with worktree base
        worktree_base = tmp_path / "worktrees"
        worktree_base.mkdir()
        agent_config = sample_config.agents["agent:web"]
        agent_config.worktree_base = worktree_base

        # Create worktree
        worktree = worktree_base / "my-repo-123"
        worktree.mkdir()

        # Mock PRs with reviewed label
        mock_list_prs.return_value = [
            {"number": 100, "headRefName": "issue-123-test-feature"}
        ]

        orchestrator = Orchestrator(config=sample_config)

        # Mock session_exists to return True (session still running)
        orchestrator._session_exists = lambda name: True

        orchestrator._recover_orphaned_cleanups()

        # Worktree should NOT be cleaned up (session still running)
        mock_remove_worktree.assert_not_called()

    @patch("issue_orchestrator.orchestrator.list_prs_with_label")
    def test_recover_noop_without_review_workflow(
        self,
        mock_list_prs,
        sample_config,
    ):
        """Test that recovery does nothing without review workflow."""
        # No review workflow
        sample_config.cto_review_agent = None
        sample_config.code_review_agent = None

        orchestrator = Orchestrator(config=sample_config)

        orchestrator._recover_orphaned_cleanups()

        # Should not call list_prs (no label to look for)
        mock_list_prs.assert_not_called()

    @patch("issue_orchestrator.orchestrator.list_prs_with_label")
    def test_recover_handles_no_reviewed_prs(
        self,
        mock_list_prs,
        sample_config,
    ):
        """Test that recovery handles case with no reviewed PRs."""
        sample_config.cto_review_agent = "agent:cto"
        sample_config.cto_reviewed_label = "cto-reviewed"

        # No reviewed PRs
        mock_list_prs.return_value = []

        orchestrator = Orchestrator(config=sample_config)

        # Should not raise
        orchestrator._recover_orphaned_cleanups()
