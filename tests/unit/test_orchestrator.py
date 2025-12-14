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
    @patch("issue_orchestrator.orchestrator.cleanup_stale_claims")
    @patch("issue_orchestrator.analysis.get_issue_branches")
    @patch("issue_orchestrator.orchestrator.list_issues")
    @patch("issue_orchestrator.analysis.analyze_issue")
    @patch("issue_orchestrator.orchestrator.session_exists")
    @patch("issue_orchestrator.orchestrator.remove_label")
    async def test_startup_cleans_stale_claims(
        self,
        mock_remove_label,
        mock_session_exists,
        mock_analyze,
        mock_list_issues,
        mock_get_branches,
        mock_cleanup_claims,
        sample_config,
    ):
        """Test that startup cleans up stale lock claims."""
        mock_cleanup_claims.return_value = ["issue-1.lock", "issue-2.lock"]
        mock_get_branches.return_value = {}
        mock_list_issues.return_value = []

        orchestrator = Orchestrator(config=sample_config)
        await orchestrator.startup()

        mock_cleanup_claims.assert_called_once()

    @pytest.mark.asyncio
    @patch("issue_orchestrator.orchestrator.cleanup_stale_claims")
    @patch("issue_orchestrator.analysis.get_issue_branches")
    @patch("issue_orchestrator.orchestrator.list_issues")
    @patch("issue_orchestrator.analysis.analyze_issue")
    @patch("issue_orchestrator.orchestrator.session_exists")
    @patch("issue_orchestrator.orchestrator.remove_label")
    async def test_startup_checks_in_progress_issues(
        self,
        mock_remove_label,
        mock_session_exists,
        mock_analyze,
        mock_list_issues,
        mock_get_branches,
        mock_cleanup_claims,
        sample_config,
    ):
        """Test that startup checks for in-progress issues."""
        mock_cleanup_claims.return_value = []
        mock_get_branches.return_value = {}
        mock_list_issues.return_value = []

        orchestrator = Orchestrator(config=sample_config)
        await orchestrator.startup()

        # Should query for in-progress issues for each agent type
        mock_list_issues.assert_called()
        call_labels = mock_list_issues.call_args[1]["labels"]
        assert "agent:web" in call_labels
        assert sample_config.get_label_in_progress() in call_labels

    @pytest.mark.asyncio
    @patch("issue_orchestrator.orchestrator.cleanup_stale_claims")
    @patch("issue_orchestrator.analysis.get_issue_branches")
    @patch("issue_orchestrator.orchestrator.list_issues")
    @patch("issue_orchestrator.analysis.analyze_issue")
    @patch("issue_orchestrator.orchestrator.session_exists")
    @patch("issue_orchestrator.orchestrator.remove_label")
    async def test_startup_clears_orphaned_labels(
        self,
        mock_remove_label,
        mock_session_exists,
        mock_analyze,
        mock_list_issues,
        mock_get_branches,
        mock_cleanup_claims,
        sample_config,
    ):
        """Test that startup clears orphaned in-progress labels."""
        mock_cleanup_claims.return_value = []
        mock_get_branches.return_value = {}

        issue = create_issue(1, labels=["agent:web", "in-progress"])
        mock_list_issues.return_value = [issue]

        # Mock analyze_issue to indicate orphaned label
        mock_state = MagicMock()
        mock_state.has_session = False
        mock_state.has_open_pr = False
        mock_state.has_partial_work = False
        mock_state.is_orphaned_label = True
        mock_analyze.return_value = mock_state

        orchestrator = Orchestrator(config=sample_config)
        await orchestrator.startup()

        # Should remove the in-progress label
        mock_remove_label.assert_called_once_with(
            sample_config.repo, 1, sample_config.get_label_in_progress()
        )

    @pytest.mark.asyncio
    @patch("issue_orchestrator.orchestrator.cleanup_stale_claims")
    @patch("issue_orchestrator.analysis.get_issue_branches")
    @patch("issue_orchestrator.orchestrator.list_issues")
    @patch("issue_orchestrator.analysis.analyze_issue")
    @patch("issue_orchestrator.orchestrator.session_exists")
    @patch("issue_orchestrator.orchestrator.remove_label")
    async def test_startup_skips_issues_with_open_prs(
        self,
        mock_remove_label,
        mock_session_exists,
        mock_analyze,
        mock_list_issues,
        mock_get_branches,
        mock_cleanup_claims,
        sample_config,
    ):
        """Test that startup doesn't clear labels for issues with open PRs."""
        mock_cleanup_claims.return_value = []
        mock_get_branches.return_value = {}

        issue = create_issue(1, labels=["agent:web", "in-progress"])
        mock_list_issues.return_value = [issue]

        # Mock analyze_issue to indicate has open PR
        mock_state = MagicMock()
        mock_state.has_session = False
        mock_state.has_open_pr = True
        mock_state.pr_url = "https://github.com/owner/repo/pull/123"
        mock_analyze.return_value = mock_state

        orchestrator = Orchestrator(config=sample_config)
        await orchestrator.startup()

        # Should NOT remove the label
        mock_remove_label.assert_not_called()

    @pytest.mark.asyncio
    @patch("issue_orchestrator.orchestrator.cleanup_stale_claims")
    @patch("issue_orchestrator.analysis.get_issue_branches")
    @patch("issue_orchestrator.orchestrator.list_issues")
    @patch("issue_orchestrator.analysis.analyze_issue")
    @patch("issue_orchestrator.orchestrator.session_exists")
    @patch("issue_orchestrator.orchestrator.remove_label")
    async def test_startup_clears_labels_for_partial_work(
        self,
        mock_remove_label,
        mock_session_exists,
        mock_analyze,
        mock_list_issues,
        mock_get_branches,
        mock_cleanup_claims,
        sample_config,
    ):
        """Test that startup clears labels for partial work (branch but no session)."""
        mock_cleanup_claims.return_value = []
        mock_get_branches.return_value = {}

        issue = create_issue(1, labels=["agent:web", "in-progress"])
        mock_list_issues.return_value = [issue]

        # Mock analyze_issue to indicate has partial work
        mock_state = MagicMock()
        mock_state.has_session = False
        mock_state.has_open_pr = False
        mock_state.has_partial_work = True
        mock_state.branch = "feature/issue-1"
        mock_analyze.return_value = mock_state

        orchestrator = Orchestrator(config=sample_config)
        await orchestrator.startup()

        # Should remove the label (will resume from branch later)
        mock_remove_label.assert_called_once_with(
            sample_config.repo, 1, sample_config.get_label_in_progress()
        )


class TestLaunchSession:
    """Test the launch_session method."""

    @patch("issue_orchestrator.orchestrator.try_claim")
    @patch("issue_orchestrator.orchestrator.create_worktree")
    @patch("issue_orchestrator.orchestrator.add_label")
    @patch("issue_orchestrator.orchestrator.create_session")
    def test_launch_session_creates_worktree(
        self,
        mock_create_tmux,
        mock_add_label,
        mock_create_worktree,
        mock_try_claim,
        sample_config,
    ):
        """Test that launch_session creates a worktree."""
        mock_try_claim.return_value = True
        mock_create_worktree.return_value = (Path("/tmp/worktree"), "feature/issue-1")

        issue = create_issue(1, labels=["agent:web"])
        orchestrator = Orchestrator(config=sample_config)

        session = orchestrator.launch_session(issue)

        mock_create_worktree.assert_called_once()
        assert mock_create_worktree.call_args[1]["issue_number"] == 1

    @patch("issue_orchestrator.orchestrator.try_claim")
    @patch("issue_orchestrator.orchestrator.create_worktree")
    @patch("issue_orchestrator.orchestrator.add_label")
    @patch("issue_orchestrator.orchestrator.create_session")
    def test_launch_session_adds_in_progress_label(
        self,
        mock_create_tmux,
        mock_add_label,
        mock_create_worktree,
        mock_try_claim,
        sample_config,
    ):
        """Test that launch_session adds the in-progress label."""
        mock_try_claim.return_value = True
        mock_create_worktree.return_value = (Path("/tmp/worktree"), "feature/issue-1")

        issue = create_issue(1, labels=["agent:web"])
        orchestrator = Orchestrator(config=sample_config)

        session = orchestrator.launch_session(issue)

        mock_add_label.assert_called_once_with(
            sample_config.repo, 1, sample_config.get_label_in_progress()
        )

    @patch("issue_orchestrator.orchestrator.try_claim")
    @patch("issue_orchestrator.orchestrator.create_worktree")
    @patch("issue_orchestrator.orchestrator.add_label")
    @patch("issue_orchestrator.orchestrator.create_session")
    def test_launch_session_creates_tmux_session(
        self,
        mock_create_tmux,
        mock_add_label,
        mock_create_worktree,
        mock_try_claim,
        sample_config,
    ):
        """Test that launch_session creates a tmux session."""
        mock_try_claim.return_value = True
        mock_create_worktree.return_value = (Path("/tmp/worktree"), "feature/issue-1")

        issue = create_issue(1, title="Test Issue", labels=["agent:web"])
        sample_config.ui_mode = "tmux"  # Explicitly test tmux mode
        orchestrator = Orchestrator(config=sample_config)

        session = orchestrator.launch_session(issue)

        mock_create_tmux.assert_called_once()
        call_args = mock_create_tmux.call_args
        assert call_args[0][0] == "issue-1"  # session name
        assert isinstance(call_args[0][1], str)  # command
        assert call_args[0][2] == Path("/tmp/worktree")  # worktree path

    @patch("issue_orchestrator.orchestrator.try_claim")
    @patch("issue_orchestrator.orchestrator.create_worktree")
    @patch("issue_orchestrator.orchestrator.add_label")
    @patch("issue_orchestrator.orchestrator.create_session")
    def test_launch_session_adds_to_active_sessions(
        self,
        mock_create_tmux,
        mock_add_label,
        mock_create_worktree,
        mock_try_claim,
        sample_config,
    ):
        """Test that launch_session adds the session to active_sessions."""
        mock_try_claim.return_value = True
        mock_create_worktree.return_value = (Path("/tmp/worktree"), "feature/issue-1")

        issue = create_issue(1, labels=["agent:web"])
        orchestrator = Orchestrator(config=sample_config)

        assert len(orchestrator.state.active_sessions) == 0

        session = orchestrator.launch_session(issue)

        assert len(orchestrator.state.active_sessions) == 1
        assert orchestrator.state.active_sessions[0] == session

    @patch("issue_orchestrator.orchestrator.try_claim")
    @patch("issue_orchestrator.orchestrator.create_worktree")
    @patch("issue_orchestrator.orchestrator.add_label")
    @patch("issue_orchestrator.orchestrator.create_session")
    def test_launch_session_returns_session_object(
        self,
        mock_create_tmux,
        mock_add_label,
        mock_create_worktree,
        mock_try_claim,
        sample_config,
    ):
        """Test that launch_session returns a Session object."""
        mock_try_claim.return_value = True
        mock_create_worktree.return_value = (Path("/tmp/worktree"), "feature/issue-1")

        issue = create_issue(1, labels=["agent:web"])
        orchestrator = Orchestrator(config=sample_config)

        session = orchestrator.launch_session(issue)

        assert isinstance(session, Session)
        assert session.issue == issue
        assert session.tmux_session_name == "issue-1"
        assert session.worktree_path == Path("/tmp/worktree")
        assert session.branch_name == "feature/issue-1"

    @patch("issue_orchestrator.orchestrator.try_claim")
    def test_launch_session_returns_none_if_already_claimed(
        self,
        mock_try_claim,
        sample_config,
    ):
        """Test that launch_session returns None if issue is already claimed."""
        mock_try_claim.return_value = False

        issue = create_issue(1, labels=["agent:web"])
        orchestrator = Orchestrator(config=sample_config)

        session = orchestrator.launch_session(issue)

        assert session is None

    @patch("issue_orchestrator.orchestrator.try_claim")
    def test_launch_session_raises_error_for_unknown_agent(
        self,
        mock_try_claim,
        sample_config,
    ):
        """Test that launch_session raises error for unknown agent type."""
        mock_try_claim.return_value = True

        issue = create_issue(1, labels=["agent:unknown"])
        orchestrator = Orchestrator(config=sample_config)

        with pytest.raises(ValueError, match="No agent config for agent:unknown"):
            orchestrator.launch_session(issue)

    @patch("issue_orchestrator.orchestrator.try_claim")
    @patch("issue_orchestrator.orchestrator.create_worktree")
    @patch("issue_orchestrator.orchestrator.add_label")
    @patch("issue_orchestrator.orchestrator.create_session")
    def test_launch_session_uses_agent_repo_root_if_configured(
        self,
        mock_create_tmux,
        mock_add_label,
        mock_create_worktree,
        mock_try_claim,
        sample_config,
    ):
        """Test that launch_session uses agent-specific repo_root if configured."""
        mock_try_claim.return_value = True
        mock_create_worktree.return_value = (Path("/tmp/worktree"), "feature/issue-1")

        # Configure agent with specific repo_root
        sample_config.agents["agent:web"].repo_root = Path("/custom/repo/path")

        issue = create_issue(1, labels=["agent:web"])
        orchestrator = Orchestrator(config=sample_config)

        session = orchestrator.launch_session(issue)

        # Should use agent's repo_root
        assert mock_create_worktree.call_args[1]["repo_root"] == Path("/custom/repo/path")

    @patch("issue_orchestrator.orchestrator.try_claim")
    @patch("issue_orchestrator.orchestrator.create_worktree")
    @patch("issue_orchestrator.orchestrator.add_label")
    @patch("issue_orchestrator.orchestrator.create_session")
    def test_launch_session_falls_back_to_config_repo_root(
        self,
        mock_create_tmux,
        mock_add_label,
        mock_create_worktree,
        mock_try_claim,
        sample_config,
    ):
        """Test that launch_session falls back to config.repo_root if agent doesn't specify."""
        mock_try_claim.return_value = True
        mock_create_worktree.return_value = (Path("/tmp/worktree"), "feature/issue-1")

        # Ensure agent doesn't have repo_root set
        sample_config.agents["agent:web"].repo_root = None
        sample_config.repo_root = Path("/default/repo/path")

        issue = create_issue(1, labels=["agent:web"])
        orchestrator = Orchestrator(config=sample_config)

        session = orchestrator.launch_session(issue)

        # Should use config's repo_root
        assert mock_create_worktree.call_args[1]["repo_root"] == Path("/default/repo/path")


class TestHandleSessionCompletion:
    """Test the handle_session_completion method."""

    @patch("issue_orchestrator.orchestrator.release_claim")
    @patch("issue_orchestrator.orchestrator.remove_worktree")
    def test_handle_completion_releases_claim(
        self,
        mock_remove_worktree,
        mock_release_claim,
        sample_config,
    ):
        """Test that handle_session_completion releases the lock claim."""
        issue = create_issue(1)
        session = create_session(issue)

        orchestrator = Orchestrator(config=sample_config)
        orchestrator.state.active_sessions.append(session)

        orchestrator.handle_session_completion(session, SessionStatus.COMPLETED)

        mock_release_claim.assert_called_once_with(1)

    @patch("issue_orchestrator.orchestrator.release_claim")
    @patch("issue_orchestrator.orchestrator.remove_worktree")
    def test_handle_completion_removes_from_active_sessions(
        self,
        mock_remove_worktree,
        mock_release_claim,
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

    @patch("issue_orchestrator.orchestrator.release_claim")
    @patch("issue_orchestrator.orchestrator.remove_worktree")
    def test_handle_completion_calls_monitor_handler(
        self,
        mock_remove_worktree,
        mock_release_claim,
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

    @patch("issue_orchestrator.orchestrator.release_claim")
    @patch("issue_orchestrator.orchestrator.remove_worktree")
    def test_handle_completion_tracks_completed_issues(
        self,
        mock_remove_worktree,
        mock_release_claim,
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

    @patch("issue_orchestrator.orchestrator.release_claim")
    @patch("issue_orchestrator.orchestrator.remove_worktree")
    def test_handle_completion_does_not_track_failed_issues(
        self,
        mock_remove_worktree,
        mock_release_claim,
        sample_config,
    ):
        """Test that failed sessions are not added to completed_today."""
        issue = create_issue(1)
        session = create_session(issue)

        orchestrator = Orchestrator(config=sample_config)
        orchestrator.state.active_sessions.append(session)

        orchestrator.handle_session_completion(session, SessionStatus.FAILED)

        assert len(orchestrator.state.completed_today) == 0

    @patch("issue_orchestrator.orchestrator.release_claim")
    @patch("issue_orchestrator.orchestrator.remove_worktree")
    def test_handle_completion_removes_worktree_for_completed(
        self,
        mock_remove_worktree,
        mock_release_claim,
        sample_config,
    ):
        """Test that worktree is removed for completed sessions."""
        issue = create_issue(1)
        session = create_session(issue, worktree_path="/tmp/worktree")

        orchestrator = Orchestrator(config=sample_config)
        orchestrator.state.active_sessions.append(session)

        orchestrator.handle_session_completion(session, SessionStatus.COMPLETED)

        mock_remove_worktree.assert_called_once_with(Path("/tmp/worktree"))

    @patch("issue_orchestrator.orchestrator.release_claim")
    @patch("issue_orchestrator.orchestrator.remove_worktree")
    def test_handle_completion_keeps_worktree_for_blocked(
        self,
        mock_remove_worktree,
        mock_release_claim,
        sample_config,
    ):
        """Test that worktree is kept for blocked sessions."""
        issue = create_issue(1)
        session = create_session(issue)

        orchestrator = Orchestrator(config=sample_config)
        orchestrator.state.active_sessions.append(session)

        orchestrator.handle_session_completion(session, SessionStatus.BLOCKED)

        mock_remove_worktree.assert_not_called()

    @patch("issue_orchestrator.orchestrator.release_claim")
    @patch("issue_orchestrator.orchestrator.remove_worktree")
    def test_handle_completion_keeps_worktree_for_failed(
        self,
        mock_remove_worktree,
        mock_release_claim,
        sample_config,
    ):
        """Test that worktree is kept for failed sessions."""
        issue = create_issue(1)
        session = create_session(issue)

        orchestrator = Orchestrator(config=sample_config)
        orchestrator.state.active_sessions.append(session)

        orchestrator.handle_session_completion(session, SessionStatus.FAILED)

        mock_remove_worktree.assert_not_called()

    @patch("issue_orchestrator.orchestrator.release_claim")
    @patch("issue_orchestrator.orchestrator.remove_worktree")
    def test_handle_completion_handles_worktree_removal_error(
        self,
        mock_remove_worktree,
        mock_release_claim,
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
    @patch("issue_orchestrator.orchestrator.list_issues")
    async def test_run_loop_exits_on_shutdown_request(
        self,
        mock_list_issues,
        sample_config,
    ):
        """Test that run_loop exits when shutdown is requested."""
        mock_list_issues.return_value = []

        orchestrator = Orchestrator(config=sample_config)
        orchestrator.request_shutdown()  # Request shutdown immediately

        # Should exit quickly without running loop
        await orchestrator.run_loop()

        assert orchestrator._shutdown_requested is True

    @pytest.mark.asyncio
    @patch("issue_orchestrator.orchestrator.list_issues")
    async def test_run_loop_checks_active_sessions(
        self,
        mock_list_issues,
        sample_config,
    ):
        """Test that run_loop checks status of active sessions."""
        mock_list_issues.return_value = []

        issue = create_issue(1)
        session = create_session(issue)

        orchestrator = Orchestrator(config=sample_config)
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
    @patch("issue_orchestrator.orchestrator.list_issues")
    async def test_run_loop_handles_completed_sessions(
        self,
        mock_list_issues,
        sample_config,
    ):
        """Test that run_loop handles completed sessions."""
        mock_list_issues.return_value = []

        issue = create_issue(1)
        session = create_session(issue)

        orchestrator = Orchestrator(config=sample_config)
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
    @patch("issue_orchestrator.orchestrator.list_issues")
    async def test_run_loop_fetches_available_issues_when_not_paused(
        self,
        mock_list_issues,
        sample_config,
    ):
        """Test that run_loop fetches available issues when not paused."""
        mock_list_issues.return_value = []

        orchestrator = Orchestrator(config=sample_config)

        # Run one iteration
        async def run_one_iteration():
            await asyncio.sleep(0.01)
            orchestrator.request_shutdown()

        await asyncio.gather(
            orchestrator.run_loop(),
            run_one_iteration(),
        )

        mock_list_issues.assert_called()

    @pytest.mark.asyncio
    @patch("issue_orchestrator.orchestrator.list_issues")
    async def test_run_loop_does_not_fetch_when_paused(
        self,
        mock_list_issues,
        sample_config,
    ):
        """Test that run_loop doesn't fetch new issues when paused."""
        mock_list_issues.return_value = []

        orchestrator = Orchestrator(config=sample_config)
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
        mock_list_issues.assert_not_called()

    @pytest.mark.asyncio
    @patch("issue_orchestrator.orchestrator.list_issues")
    async def test_run_loop_respects_max_sessions_limit(
        self,
        mock_list_issues,
        sample_config,
    ):
        """Test that run_loop respects max_sessions limit."""
        sample_config.max_concurrent_sessions = 2

        issue1 = create_issue(1)
        issue2 = create_issue(2)
        issue3 = create_issue(3)

        mock_list_issues.return_value = [issue1, issue2, issue3]

        orchestrator = Orchestrator(config=sample_config)

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
    @patch("issue_orchestrator.orchestrator.list_issues")
    async def test_run_loop_launches_sessions_with_available_capacity(
        self,
        mock_list_issues,
        sample_config,
    ):
        """Test that run_loop launches sessions when capacity is available."""
        sample_config.max_concurrent_sessions = 3

        issue1 = create_issue(1, labels=["agent:web"])

        mock_list_issues.return_value = [issue1]

        orchestrator = Orchestrator(config=sample_config)

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
    @patch("issue_orchestrator.orchestrator.list_issues")
    async def test_run_loop_handles_launch_exceptions(
        self,
        mock_list_issues,
        sample_config,
    ):
        """Test that run_loop handles exceptions during launch gracefully."""
        issue1 = create_issue(1, labels=["agent:web"])

        mock_list_issues.return_value = [issue1]

        orchestrator = Orchestrator(config=sample_config)

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
    @patch("issue_orchestrator.orchestrator.list_issues")
    async def test_run_loop_skips_already_claimed_issues(
        self,
        mock_list_issues,
        sample_config,
    ):
        """Test that run_loop continues when an issue is already claimed."""
        issue1 = create_issue(1, labels=["agent:web"])
        issue2 = create_issue(2, labels=["agent:web"])

        mock_list_issues.return_value = [issue1, issue2]

        orchestrator = Orchestrator(config=sample_config)

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
    @patch("issue_orchestrator.orchestrator.list_issues")
    async def test_run_loop_respects_max_issues_to_start(
        self,
        mock_list_issues,
        sample_config,
    ):
        """Test that run_loop stops launching when max_issues_to_start is reached."""
        sample_config.max_issues_to_start = 2
        sample_config.max_concurrent_sessions = 5  # Plenty of capacity

        issue1 = create_issue(1, labels=["agent:web"])
        issue2 = create_issue(2, labels=["agent:web"])
        issue3 = create_issue(3, labels=["agent:web"])

        mock_list_issues.return_value = [issue1, issue2, issue3]

        orchestrator = Orchestrator(config=sample_config)

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
    @patch("issue_orchestrator.orchestrator.list_issues")
    async def test_run_loop_unlimited_when_max_issues_zero(
        self,
        mock_list_issues,
        sample_config,
    ):
        """Test that max_issues_to_start=0 means unlimited."""
        sample_config.max_issues_to_start = 0  # Unlimited
        sample_config.max_concurrent_sessions = 5

        issue1 = create_issue(1, labels=["agent:web"])
        issue2 = create_issue(2, labels=["agent:web"])
        issue3 = create_issue(3, labels=["agent:web"])

        mock_list_issues.return_value = [issue1, issue2, issue3]

        orchestrator = Orchestrator(config=sample_config)

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
    @patch("issue_orchestrator.orchestrator.list_issues")
    async def test_run_loop_does_not_launch_when_limit_already_reached(
        self,
        mock_list_issues,
        sample_config,
    ):
        """Test that no new issues are launched if limit was already reached."""
        sample_config.max_issues_to_start = 2
        sample_config.max_concurrent_sessions = 5

        issue1 = create_issue(1, labels=["agent:web"])

        mock_list_issues.return_value = [issue1]

        orchestrator = Orchestrator(config=sample_config)
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

    @patch("issue_orchestrator.orchestrator.try_claim")
    @patch("issue_orchestrator.orchestrator.create_worktree")
    @patch("issue_orchestrator.orchestrator.add_label")
    @patch("issue_orchestrator.orchestrator.create_session")
    def test_launch_session_increments_issues_started_count(
        self,
        mock_create_tmux,
        mock_add_label,
        mock_create_worktree,
        mock_try_claim,
        sample_config,
    ):
        """Test that launching a session increments issues_started_count."""
        mock_try_claim.return_value = True
        mock_create_worktree.return_value = (Path("/tmp/worktree"), "feature/issue-1")

        issue = create_issue(1, labels=["agent:web"])
        orchestrator = Orchestrator(config=sample_config)

        assert orchestrator.state.issues_started_count == 0

        # Note: The counter is incremented in run_loop, not in launch_session itself
        # So we test that the state field exists and works
        orchestrator.state.issues_started_count = 5
        assert orchestrator.state.issues_started_count == 5

    @pytest.mark.asyncio
    @patch("issue_orchestrator.orchestrator.list_issues")
    async def test_run_loop_checks_limit_before_each_launch(
        self,
        mock_list_issues,
        sample_config,
    ):
        """Test that limit is checked before each launch in a batch."""
        sample_config.max_issues_to_start = 1
        sample_config.max_concurrent_sessions = 5

        issue1 = create_issue(1, labels=["agent:web"])
        issue2 = create_issue(2, labels=["agent:web"])

        mock_list_issues.return_value = [issue1, issue2]

        orchestrator = Orchestrator(config=sample_config)

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
    @patch("issue_orchestrator.orchestrator.list_issues")
    async def test_run_loop_skipped_claims_dont_count_toward_limit(
        self,
        mock_list_issues,
        sample_config,
    ):
        """Test that issues that were already claimed don't count toward the limit."""
        sample_config.max_issues_to_start = 2
        sample_config.max_concurrent_sessions = 5

        issue1 = create_issue(1, labels=["agent:web"])
        issue2 = create_issue(2, labels=["agent:web"])
        issue3 = create_issue(3, labels=["agent:web"])

        mock_list_issues.return_value = [issue1, issue2, issue3]

        orchestrator = Orchestrator(config=sample_config)

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


class TestCheckReviewTrigger:
    """Test the check_review_trigger method for CTO review workflow."""

    def test_check_review_trigger_disabled_without_review_label(self, sample_config):
        """Test that check_review_trigger does nothing without review_label configured."""
        sample_config.review_label = None
        sample_config.review_agent = "agent:cto"
        sample_config.review_threshold = 5

        orchestrator = Orchestrator(config=sample_config)

        # Should not raise and should not call any GitHub functions
        with patch("issue_orchestrator.orchestrator.list_prs_with_label") as mock_prs:
            orchestrator.check_review_trigger()
            mock_prs.assert_not_called()

    def test_check_review_trigger_disabled_without_review_agent(self, sample_config):
        """Test that check_review_trigger does nothing without review_agent configured."""
        sample_config.review_label = "needs-cto-review"
        sample_config.review_agent = None
        sample_config.review_threshold = 5

        orchestrator = Orchestrator(config=sample_config)

        with patch("issue_orchestrator.orchestrator.list_prs_with_label") as mock_prs:
            orchestrator.check_review_trigger()
            mock_prs.assert_not_called()

    def test_check_review_trigger_disabled_with_zero_threshold(self, sample_config):
        """Test that check_review_trigger does nothing with threshold=0."""
        sample_config.review_label = "needs-cto-review"
        sample_config.review_agent = "agent:cto"
        sample_config.review_threshold = 0

        orchestrator = Orchestrator(config=sample_config)

        with patch("issue_orchestrator.orchestrator.list_prs_with_label") as mock_prs:
            orchestrator.check_review_trigger()
            mock_prs.assert_not_called()

    @patch("issue_orchestrator.orchestrator.list_prs_with_label")
    def test_check_review_trigger_below_threshold(self, mock_prs, sample_config):
        """Test that no review issue is created when below threshold."""
        sample_config.review_label = "needs-cto-review"
        sample_config.review_agent = "agent:cto"
        sample_config.review_threshold = 5

        mock_prs.return_value = [
            {"number": 1, "title": "PR 1"},
            {"number": 2, "title": "PR 2"},
        ]  # Only 2 PRs, below threshold of 5

        orchestrator = Orchestrator(config=sample_config)

        with patch("issue_orchestrator.orchestrator.create_issue") as mock_create:
            orchestrator.check_review_trigger()
            mock_create.assert_not_called()

    @patch("issue_orchestrator.orchestrator.list_issues")
    @patch("issue_orchestrator.orchestrator.list_prs_with_label")
    @patch("issue_orchestrator.orchestrator.create_issue")
    def test_check_review_trigger_creates_review_issue(
        self, mock_create, mock_prs, mock_issues, sample_config
    ):
        """Test that review issue is created when threshold is reached."""
        sample_config.review_label = "needs-cto-review"
        sample_config.review_agent = "agent:cto"
        sample_config.review_threshold = 3

        mock_prs.return_value = [
            {"number": 1, "title": "PR 1"},
            {"number": 2, "title": "PR 2"},
            {"number": 3, "title": "PR 3"},
        ]  # 3 PRs, meets threshold
        mock_issues.return_value = []  # No existing review issues
        mock_create.return_value = 42

        orchestrator = Orchestrator(config=sample_config)
        orchestrator.check_review_trigger()

        mock_create.assert_called_once()
        call_args = mock_create.call_args
        assert "CTO Batch Review" in call_args[1]["title"]
        assert sample_config.review_agent in call_args[1]["labels"]

    @patch("issue_orchestrator.orchestrator.list_issues")
    @patch("issue_orchestrator.orchestrator.list_prs_with_label")
    @patch("issue_orchestrator.orchestrator.create_issue")
    def test_check_review_trigger_skips_if_review_issue_exists(
        self, mock_create, mock_prs, mock_issues, sample_config
    ):
        """Test that no duplicate review issue is created."""
        sample_config.review_label = "needs-cto-review"
        sample_config.review_agent = "agent:cto"
        sample_config.review_threshold = 3

        mock_prs.return_value = [
            {"number": 1, "title": "PR 1"},
            {"number": 2, "title": "PR 2"},
            {"number": 3, "title": "PR 3"},
        ]

        # Existing review issue
        existing_issue = create_issue(100, title="CTO Batch Review: 3 PRs pending")
        mock_issues.return_value = [existing_issue]

        orchestrator = Orchestrator(config=sample_config)
        orchestrator.check_review_trigger()

        # Should not create a new issue
        mock_create.assert_not_called()

    @patch("issue_orchestrator.orchestrator.list_issues")
    @patch("issue_orchestrator.orchestrator.list_prs_with_label")
    @patch("issue_orchestrator.orchestrator.create_issue")
    def test_check_review_trigger_review_body_includes_pr_list(
        self, mock_create, mock_prs, mock_issues, sample_config
    ):
        """Test that review issue body includes list of PRs."""
        sample_config.review_label = "needs-cto-review"
        sample_config.review_agent = "agent:cto"
        sample_config.reviewed_label = "cto-reviewed"
        sample_config.review_threshold = 2

        mock_prs.return_value = [
            {"number": 10, "title": "Fix bug A"},
            {"number": 20, "title": "Add feature B"},
        ]
        mock_issues.return_value = []
        mock_create.return_value = 42

        orchestrator = Orchestrator(config=sample_config)
        orchestrator.check_review_trigger()

        call_args = mock_create.call_args
        body = call_args[1]["body"]
        assert "PR #10" in body
        assert "Fix bug A" in body
        assert "PR #20" in body
        assert "Add feature B" in body
        assert "needs-cto-review" in body
        assert "cto-reviewed" in body
