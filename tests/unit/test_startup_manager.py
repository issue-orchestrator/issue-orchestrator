"""Unit tests for StartupManager."""

import pytest
from unittest.mock import MagicMock, Mock, AsyncMock, patch

from issue_orchestrator.control.startup_manager import StartupManager
from issue_orchestrator.config import Config
from issue_orchestrator.models import (
    Issue,
    OrchestratorState,
    Session,
    AgentConfig,
    PendingReview,
    PendingTriageReview,
)


@pytest.fixture
def mock_config(tmp_path):
    """Create a mock config for testing."""
    config = Config()
    config.repo = "owner/repo"
    config.repo_root = tmp_path
    config.max_concurrent_sessions = 3
    config.code_review_agent = None
    config.code_review_label = None
    config.triage_review_agent = None
    config.filter_label = None
    config.filter_milestone = None
    config.issue_fetch_limit = 100
    config.dangerous = MagicMock()
    config.dangerous.allow_unsupported_agents = True
    return config


@pytest.fixture
def mock_events():
    """Create a mock EventSink."""
    return MagicMock()


@pytest.fixture
def mock_runner():
    """Create a mock SessionRunner."""
    runner = MagicMock()
    runner.cleanup_idle_sessions.return_value = 0
    runner.discover_running_sessions.return_value = []
    return runner


@pytest.fixture
def mock_repository_host():
    """Create a mock RepositoryHost."""
    repo = MagicMock()
    repo.list_issues.return_value = []
    repo.get_prs_with_label.return_value = []
    return repo


@pytest.fixture
def mock_hook_verifier():
    """Create a mock HookVerifier."""
    verifier = MagicMock()
    verifier.verify = AsyncMock(return_value=MagicMock(success=True, message="ok"))
    verifier.raise_on_failure = MagicMock()
    return verifier


@pytest.fixture
def mock_issue_branches_fn():
    """Create a mock issue branches provider."""
    return MagicMock(return_value={})


@pytest.fixture
def sample_state():
    """Create a sample orchestrator state."""
    return OrchestratorState()


@pytest.fixture
def startup_manager(
    mock_config,
    mock_events,
    mock_runner,
    mock_repository_host,
    mock_hook_verifier,
    mock_issue_branches_fn,
):
    """Create a StartupManager with mocks."""
    return StartupManager(
        config=mock_config,
        events=mock_events,
        runner=mock_runner,
        repository_host=mock_repository_host,
        hook_verifier=mock_hook_verifier,
        issue_branches_fn=mock_issue_branches_fn,
        session_exists_fn=lambda name: False,
        restore_sessions_fn=MagicMock(),
        launch_session_fn=lambda issue: None,
        update_queue_cache_fn=lambda: None,
    )


class TestStartupManagerBasic:
    """Basic tests for StartupManager."""

    @pytest.mark.asyncio
    async def test_run_startup_completes(self, startup_manager, sample_state):
        """Test that startup runs to completion."""

        await startup_manager.run_startup(sample_state)

        assert sample_state.startup_status == "complete"
        assert sample_state.startup_message == ""

    @pytest.mark.asyncio
    async def test_run_startup_emits_config_event(
        self, startup_manager, sample_state, mock_events
    ):
        """Test that startup emits config event."""

        await startup_manager.run_startup(sample_state)

        # Check that config.merged event was published
        calls = mock_events.publish.call_args_list
        assert any("config.merged" in str(call) for call in calls)

    @pytest.mark.asyncio
    async def test_run_startup_emits_ready_event(
        self, startup_manager, sample_state, mock_events
    ):
        """Test that startup emits ready event."""

        await startup_manager.run_startup(sample_state)

        # Check that orchestrator.ready event was published
        calls = mock_events.publish.call_args_list
        assert any("orchestrator.ready" in str(call) for call in calls)


class TestStartupManagerCleanup:
    """Tests for cleanup during startup."""

    @pytest.mark.asyncio
    async def test_cleans_up_idle_sessions(
        self, startup_manager, sample_state, mock_runner
    ):
        """Test that idle sessions are cleaned up."""
        mock_runner.cleanup_idle_sessions.return_value = 3

        await startup_manager.run_startup(sample_state)

        mock_runner.cleanup_idle_sessions.assert_called_once()

    @pytest.mark.asyncio
    async def test_discovers_running_sessions(
        self, startup_manager, sample_state, mock_runner
    ):
        """Test that running sessions are discovered."""
        mock_runner.discover_running_sessions.return_value = [
            {"session_name": "issue-123", "issue_number": 123}
        ]

        await startup_manager.run_startup(sample_state)

        mock_runner.discover_running_sessions.assert_called_once()


class TestStartupManagerInProgressIssues:
    """Tests for handling in-progress issues."""

    @pytest.mark.asyncio
    @patch("issue_orchestrator.control.startup_manager.analyze_issue")
    async def test_clears_orphaned_label(
        self,
        mock_analyze,
        startup_manager,
        sample_state,
        mock_repository_host,
        mock_config,
    ):
        """Test that orphaned in-progress labels are cleared."""
        mock_config.agents = {"agent:web": MagicMock()}

        issue = Issue(number=1, title="Test Issue", labels=["agent:web", "in-progress"])
        mock_repository_host.list_issues.return_value = [issue]

        # Mock analyze_issue to indicate orphaned label
        mock_state = MagicMock()
        mock_state.has_session = False
        mock_state.has_open_pr = False
        mock_state.has_partial_work = False
        mock_state.is_orphaned_label = True
        mock_analyze.return_value = mock_state

        await startup_manager.run_startup(sample_state)

        mock_repository_host.remove_label.assert_called_once_with(1, "in-progress")

    @pytest.mark.asyncio
    @patch("issue_orchestrator.control.startup_manager.analyze_issue")
    async def test_reconciles_issues_with_open_prs(
        self,
        mock_analyze,
        startup_manager,
        sample_state,
        mock_repository_host,
        mock_config,
    ):
        """Test that issues with open PRs get pr-pending label and in-progress removed (S2 crash recovery)."""
        mock_config.agents = {"agent:web": MagicMock()}

        issue = Issue(number=1, title="Test Issue", labels=["agent:web", "in-progress"])
        mock_repository_host.list_issues.return_value = [issue]

        # Mock analyze_issue to indicate has open PR
        mock_state = MagicMock()
        mock_state.has_session = False
        mock_state.has_open_pr = True
        mock_state.pr_url = "https://github.com/owner/repo/pull/123"
        mock_analyze.return_value = mock_state

        await startup_manager.run_startup(sample_state)

        # S2 crash recovery: add pr-pending, remove in-progress
        mock_repository_host.add_label.assert_called_once_with(1, "pr-pending")
        mock_repository_host.remove_label.assert_called_once_with(1, "in-progress")


class TestStartupManagerCodeReviewRecovery:
    """Tests for code review recovery."""

    @pytest.mark.asyncio
    async def test_recovers_pending_reviews(
        self,
        startup_manager,
        sample_state,
        mock_repository_host,
        mock_config,
    ):
        """Test that pending code reviews are recovered."""
        mock_config.agents = {}
        mock_config.code_review_agent = "agent:reviewer"
        mock_config.code_review_label = "needs-code-review"

        from issue_orchestrator.ports import PRInfo
        pr = PRInfo(
            number=10,
            url="https://github.com/owner/repo/pull/10",
            title="Test PR",
            branch="1-feature",
            labels=["needs-code-review"],
            body="Closes #1",
            state="open",
        )
        mock_repository_host.get_prs_with_label.return_value = [pr]

        await startup_manager.run_startup(sample_state)

        assert len(sample_state.pending_reviews) == 1
        assert sample_state.pending_reviews[0].pr_number == 10


class TestStartupManagerTriageRecovery:
    """Tests for triage review recovery."""

    @pytest.mark.asyncio
    async def test_recovers_pending_triage(
        self,
        startup_manager,
        sample_state,
        mock_repository_host,
        mock_config,
    ):
        """Test that pending triage reviews are recovered."""
        mock_config.agents = {}
        mock_config.triage_review_agent = "agent:triage"

        triage_issue = Issue(
            number=100,
            title="Batch Review: 5 PRs",
            labels=["agent:triage"]
        )
        mock_repository_host.list_issues.return_value = [triage_issue]

        await startup_manager.run_startup(sample_state)

        assert len(sample_state.pending_triage_reviews) == 1
        assert sample_state.pending_triage_reviews[0].issue_number == 100


class TestStartupManagerResumePartialWork:
    """Tests for resuming partial work."""

    @pytest.mark.asyncio
    @patch("issue_orchestrator.control.startup_manager.analyze_issue")
    async def test_resumes_partial_work(
        self,
        mock_analyze,
        startup_manager,
        sample_state,
        mock_repository_host,
        mock_config,
        mock_issue_branches_fn,
    ):
        """Test that issues with partial work are resumed."""
        mock_issue_branches_fn.return_value = {1: "1-feature"}
        mock_config.agents = {"agent:web": MagicMock()}

        issue = Issue(number=1, title="Test Issue", labels=["agent:web", "in-progress"])
        mock_repository_host.list_issues.return_value = [issue]

        # Mock analyze_issue to indicate has partial work
        mock_state = MagicMock()
        mock_state.has_session = False
        mock_state.has_open_pr = False
        mock_state.has_partial_work = True
        mock_state.branch = "1-feature"
        mock_analyze.return_value = mock_state

        # Mock launch_session_fn to return a session
        mock_session = MagicMock()
        startup_manager._launch_session = lambda i: mock_session

        await startup_manager.run_startup(sample_state)

        # The session should have been launched (check via callback)
        # Since we mocked the callback, we verify state wasn't modified with label removal
        mock_repository_host.remove_label.assert_not_called()
