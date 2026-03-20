"""Unit tests for StartupManager."""

import pytest
from unittest.mock import MagicMock, patch

from issue_orchestrator.control.startup_manager import StartupManager
from issue_orchestrator.control.actions import AddLabelAction, RemoveLabelAction
from issue_orchestrator.infra.config import Config
from issue_orchestrator.domain.models import (
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
    config.filtering.label = None
    config.filtering.milestone = None
    config.filtering.fetch_limit = 100
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
def mock_action_applier():
    """Create a mock ActionApplier."""
    applier = MagicMock()
    applier.apply = MagicMock()
    return applier


@pytest.fixture
def mock_label_store():
    """Create a mock LabelStore."""
    store = MagicMock()
    store.load_all.return_value = {}
    return store


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
    mock_action_applier,
    mock_issue_branches_fn,
    mock_label_store,
):
    """Create a StartupManager with mocks."""
    return StartupManager(
        config=mock_config,
        events=mock_events,
        runner=mock_runner,
        repository_host=mock_repository_host,
        action_applier=mock_action_applier,
        issue_branches_fn=mock_issue_branches_fn,
        session_exists_fn=lambda name: False,
        restore_sessions_fn=MagicMock(),
        launch_session_fn=lambda issue: None,
        update_queue_cache_fn=lambda: None,
        label_store=mock_label_store,
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
        mock_action_applier,
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

        mock_action_applier.apply.assert_called_once()
        action = mock_action_applier.apply.call_args.args[0]
        assert isinstance(action, RemoveLabelAction)
        assert action.issue_number == 1
        assert action.label == "in-progress"

    @pytest.mark.asyncio
    @patch("issue_orchestrator.control.startup_manager.analyze_issue")
    async def test_reconciles_issues_with_open_prs(
        self,
        mock_analyze,
        startup_manager,
        sample_state,
        mock_action_applier,
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
        assert mock_action_applier.apply.call_count == 2
        actions = [call.args[0] for call in mock_action_applier.apply.call_args_list]
        assert any(isinstance(a, AddLabelAction) and a.label == "pr-pending" for a in actions)
        assert any(isinstance(a, RemoveLabelAction) and a.label == "in-progress" for a in actions)

    @pytest.mark.asyncio
    @patch("issue_orchestrator.control.startup_manager.analyze_issue")
    async def test_warm_start_logs_and_recovers_locally_in_progress_issue_missing_from_cache(
        self,
        mock_analyze,
        startup_manager,
        sample_state,
        mock_repository_host,
        mock_label_store,
        caplog,
    ):
        cached_issue = Issue(number=1, title="Cached", labels=["agent:web"])
        missing_issue = Issue(number=4057, title="Missing", labels=["agent:web", "in-progress"])
        sample_state.cached_queue_issues = [cached_issue]
        mock_label_store.load_all.return_value = {
            4057: {"in-progress", "agent:web"},
        }
        mock_repository_host.get_issue.return_value = missing_issue

        mock_state = MagicMock()
        mock_state.has_session = False
        mock_state.has_open_pr = False
        mock_state.has_partial_work = False
        mock_state.is_orphaned_label = True
        mock_analyze.return_value = mock_state

        with caplog.at_level("INFO"):
            await startup_manager.run_startup(sample_state)

        mock_repository_host.get_issue.assert_called_once_with(4057)
        analyzed_issues = [call.kwargs["issue"].number for call in mock_analyze.call_args_list]
        assert analyzed_issues == [4057]
        assert "Cached queue omitted 1 locally in-progress issue(s): [4057]" in caplog.text

    @pytest.mark.asyncio
    async def test_warm_start_logs_when_cached_queue_matches_local_in_progress(
        self,
        startup_manager,
        sample_state,
        mock_label_store,
        caplog,
    ):
        sample_state.cached_queue_issues = [
            Issue(number=4057, title="Cached", labels=["agent:web", "in-progress"]),
        ]
        mock_label_store.load_all.return_value = {
            4057: {"in-progress", "agent:web"},
        }

        with caplog.at_level("INFO"):
            await startup_manager.run_startup(sample_state)

        assert "Local label store and cached queue agree on 1 in-progress issue(s)" in caplog.text

    @pytest.mark.asyncio
    @patch("issue_orchestrator.control.startup_manager.analyze_issue")
    async def test_warm_start_logs_when_missing_locally_in_progress_issue_cannot_be_refetched(
        self,
        mock_analyze,
        startup_manager,
        sample_state,
        mock_repository_host,
        mock_label_store,
        caplog,
    ):
        sample_state.cached_queue_issues = [
            Issue(number=1, title="Cached", labels=["agent:web"]),
        ]
        mock_label_store.load_all.return_value = {
            4057: {"in-progress", "agent:web"},
        }
        mock_repository_host.get_issue.return_value = None

        with caplog.at_level("INFO"):
            await startup_manager.run_startup(sample_state)

        mock_repository_host.get_issue.assert_called_once_with(4057)
        mock_analyze.assert_not_called()
        assert "Cached queue omitted 1 locally in-progress issue(s): [4057]" in caplog.text
        assert "Failed to refetch locally in-progress issue missing from cache: issue=4057" in caplog.text


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
        mock_action_applier,
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
        # noqa: SLF001 - Injecting mock for session launch in test
        startup_manager._launch_session = lambda i: mock_session  # noqa: SLF001

        await startup_manager.run_startup(sample_state)

        # The session should have been launched (check via callback)
        # Since we mocked the callback, we verify state wasn't modified with label removal
        mock_action_applier.apply.assert_not_called()


class TestStartupManagerValidationRetryRecovery:
    """Tests for validation retry state recovery."""

    @pytest.mark.asyncio
    async def test_recovers_pending_validation_retry(
        self,
        startup_manager,
        sample_state,
        mock_config,
        mock_issue_branches_fn,
        tmp_path,
    ):
        """Test that pending validation retries are recovered from worktree state."""
        from issue_orchestrator.infra.validation_state import (
            ValidationState,
            write_validation_state,
            write_retry_prompt,
        )

        # Set up worktree path
        mock_config.worktree_base = tmp_path
        worktree = tmp_path / f"{mock_config.repo_root.name}-42"
        worktree.mkdir()

        # Create validation state in the worktree
        state = ValidationState(
            retry_count=1,
            max_retries=3,
            validation_cmd="make test",
            last_error="Test failed: assertion error",
        )
        write_validation_state(worktree, state)
        write_retry_prompt(
            worktree,
            original_prompt="Fix the login bug",
            validation_cmd="make test",
            validation_error="Test failed: assertion error",
            retry_count=1,
            max_retries=3,
        )

        # Set up issue branches to include this issue
        mock_issue_branches_fn.return_value = {42: "42-fix-login"}

        await startup_manager.run_startup(sample_state)

        # Verify the pending validation retry was recovered
        assert len(sample_state.pending_validation_retries) == 1
        retry = sample_state.pending_validation_retries[0]
        assert retry.issue_number == 42
        assert retry.retry_count == 1
        assert retry.validation_cmd == "make test"
        assert "Test failed" in retry.validation_error

    @pytest.mark.asyncio
    async def test_no_recovery_when_no_pending_retry(
        self,
        startup_manager,
        sample_state,
        mock_config,
        mock_issue_branches_fn,
        tmp_path,
    ):
        """Test that worktrees without pending retries are not recovered."""
        # Set up worktree path without validation state
        mock_config.worktree_base = tmp_path
        worktree = tmp_path / f"{mock_config.repo_root.name}-42"
        worktree.mkdir()

        # Set up issue branches to include this issue
        mock_issue_branches_fn.return_value = {42: "42-feature"}

        await startup_manager.run_startup(sample_state)

        # Verify no pending validation retry was created
        assert len(sample_state.pending_validation_retries) == 0

    @pytest.mark.asyncio
    async def test_no_recovery_when_max_retries_exhausted(
        self,
        startup_manager,
        sample_state,
        mock_config,
        mock_issue_branches_fn,
        tmp_path,
    ):
        """Test that retries at max count are not recovered."""
        from issue_orchestrator.infra.validation_state import (
            ValidationState,
            write_validation_state,
        )

        # Set up worktree path
        mock_config.worktree_base = tmp_path
        worktree = tmp_path / f"{mock_config.repo_root.name}-42"
        worktree.mkdir()

        # Create validation state at max retries
        state = ValidationState(
            retry_count=3,  # At max
            max_retries=3,
            validation_cmd="make test",
        )
        write_validation_state(worktree, state)

        # Set up issue branches
        mock_issue_branches_fn.return_value = {42: "42-feature"}

        await startup_manager.run_startup(sample_state)

        # Verify no pending retry (max exhausted)
        assert len(sample_state.pending_validation_retries) == 0


class TestStartupGitHubCallBudget:
    """Verify startup makes only the expected number of GitHub API calls.

    This prevents regressions that silently add extra API calls to the
    startup path, which compound with agents × milestones.

    With no queue_cache_store, the startup path is:
    - Step 5: queue cache restore (no-op without store)
    - Step 6: in-progress check (cold fallback, per-agent list_issues)
    - Step 12: audit (list_issues via fetch_all_issues)

    With a warm cache store, the startup path is:
    - Step 5: queue cache restore (1 list_issues_delta call)
    - Step 6: in-progress check (filters from cache, 0 list_issues)
    - Step 12: audit (uses preloaded cache, 0 list_issues)
    """

    @pytest.mark.asyncio
    async def test_cold_start_call_count_one_agent_no_milestones(
        self, mock_config, mock_events, mock_runner, mock_repository_host,
        mock_action_applier, mock_issue_branches_fn,
    ):
        """Cold start (no store) with 1 agent, no milestones: 2 list_issues.

        - Step 6: 1 list_issues (in-progress cold fallback, 1 agent × 1 milestone)
        - Step 12: 1 list_issues (audit fetch, 1 agent × 1 milestone)
        Total: 2 list_issues, 0 get_prs_with_label
        """
        mock_config.agents = {"agent:test": AgentConfig(prompt_path=mock_config.repo_root / "prompt.md", timeout_minutes=30)}
        sm = StartupManager(
            config=mock_config, events=mock_events, runner=mock_runner,
            repository_host=mock_repository_host, action_applier=mock_action_applier,
            issue_branches_fn=mock_issue_branches_fn,
            session_exists_fn=lambda name: False,
            restore_sessions_fn=MagicMock(),
            launch_session_fn=lambda issue: None,
            update_queue_cache_fn=lambda: None,
        )

        await sm.run_startup(OrchestratorState())

        assert mock_repository_host.list_issues.call_count == 2
        assert mock_repository_host.get_prs_with_label.call_count == 0

    @pytest.mark.asyncio
    async def test_cold_start_call_count_two_agents_two_milestones(
        self, mock_config, mock_events, mock_runner, mock_repository_host,
        mock_action_applier, mock_issue_branches_fn,
    ):
        """Cold start (no store) with 2 agents, 2 milestones: 8 list_issues.

        - Step 6: 4 list_issues (2 agents × 2 milestones, cold fallback)
        - Step 12: 4 list_issues (2 agents × 2 milestones, audit fetch)
        Total: 8 list_issues, 0 get_prs_with_label
        """
        mock_config.agents = {
            "agent:a": AgentConfig(prompt_path=mock_config.repo_root / "prompt.md", timeout_minutes=30),
            "agent:b": AgentConfig(prompt_path=mock_config.repo_root / "prompt.md", timeout_minutes=30),
        }
        mock_config.filtering.milestones = ["M1", "M2"]
        sm = StartupManager(
            config=mock_config, events=mock_events, runner=mock_runner,
            repository_host=mock_repository_host, action_applier=mock_action_applier,
            issue_branches_fn=mock_issue_branches_fn,
            session_exists_fn=lambda name: False,
            restore_sessions_fn=MagicMock(),
            launch_session_fn=lambda issue: None,
            update_queue_cache_fn=lambda: None,
        )

        await sm.run_startup(OrchestratorState())

        assert mock_repository_host.list_issues.call_count == 8
        assert mock_repository_host.get_prs_with_label.call_count == 0

    @pytest.mark.asyncio
    async def test_cold_start_with_code_review_adds_one_pr_call(
        self, mock_config, mock_events, mock_runner, mock_repository_host,
        mock_action_applier, mock_issue_branches_fn,
    ):
        """Code review config adds exactly 1 get_prs_with_label call."""
        mock_config.agents = {"agent:test": AgentConfig(prompt_path=mock_config.repo_root / "prompt.md", timeout_minutes=30)}
        mock_config.code_review_agent = "agent:reviewer"
        mock_config.code_review_label = "needs-review"
        sm = StartupManager(
            config=mock_config, events=mock_events, runner=mock_runner,
            repository_host=mock_repository_host, action_applier=mock_action_applier,
            issue_branches_fn=mock_issue_branches_fn,
            session_exists_fn=lambda name: False,
            restore_sessions_fn=MagicMock(),
            launch_session_fn=lambda issue: None,
            update_queue_cache_fn=lambda: None,
        )

        await sm.run_startup(OrchestratorState())

        assert mock_repository_host.list_issues.call_count == 2
        assert mock_repository_host.get_prs_with_label.call_count == 1

    @pytest.mark.asyncio
    async def test_no_agents_makes_zero_list_issues_calls(
        self, startup_manager, sample_state, mock_repository_host,
    ):
        """With no agents configured, startup should make zero list_issues calls."""
        await startup_manager.run_startup(sample_state)

        assert mock_repository_host.list_issues.call_count == 0
        assert mock_repository_host.get_prs_with_label.call_count == 0

    @pytest.mark.asyncio
    async def test_warm_start_uses_delta_only(
        self, mock_config, mock_events, mock_runner, mock_repository_host,
        mock_action_applier, mock_issue_branches_fn,
    ):
        """Warm restart with cache: 0 list_issues, 1 list_issues_delta.

        When queue_cache_store has cached issues + watermark:
        - Step 5: 1 list_issues_delta (delta sync from cache)
        - Step 6: 0 list_issues (filters in-progress from cache)
        - Step 12: 0 list_issues (audit uses preloaded cache)
        Total: 0 list_issues, 1 list_issues_delta
        """
        mock_config.agents = {"agent:test": AgentConfig(prompt_path=mock_config.repo_root / "prompt.md", timeout_minutes=30)}

        # Pre-populate queue cache store with issues + watermark
        mock_store = MagicMock()
        mock_store.load_issues.return_value = [
            Issue(number=1, title="Cached Issue", labels=["agent:test"]),
        ]
        mock_store.load_watermark.return_value = "2025-01-01T00:00:00Z"

        # Delta sync returns no changes
        mock_repository_host.list_issues_delta.return_value = ([], "2025-01-01T00:00:01Z")

        sm = StartupManager(
            config=mock_config, events=mock_events, runner=mock_runner,
            repository_host=mock_repository_host, action_applier=mock_action_applier,
            issue_branches_fn=mock_issue_branches_fn,
            session_exists_fn=lambda name: False,
            restore_sessions_fn=MagicMock(),
            launch_session_fn=lambda issue: None,
            update_queue_cache_fn=lambda: None,
            queue_cache_store=mock_store,
        )

        await sm.run_startup(OrchestratorState())

        assert mock_repository_host.list_issues.call_count == 0
        assert mock_repository_host.list_issues_delta.call_count == 1

    @pytest.mark.asyncio
    async def test_warm_start_empty_cache_with_watermark(
        self, mock_config, mock_events, mock_runner, mock_repository_host,
        mock_action_applier, mock_issue_branches_fn,
    ):
        """Empty cached issue list + valid watermark is still a warm start.

        An empty queue is a valid cached state (no eligible issues). Should
        use delta sync (list_issues_delta), not fall back to full scan.
        """
        mock_config.agents = {"agent:test": AgentConfig(prompt_path=mock_config.repo_root / "prompt.md", timeout_minutes=30)}

        mock_store = MagicMock()
        mock_store.load_issues.return_value = []  # Empty but valid cache
        mock_store.load_watermark.return_value = "2025-01-01T00:00:00Z"

        # Delta sync returns one new issue
        mock_repository_host.list_issues_delta.return_value = (
            [Issue(number=5, title="New Issue", labels=["agent:test"])],
            "2025-01-01T00:00:01Z",
        )

        update_queue_fn = MagicMock()
        sm = StartupManager(
            config=mock_config, events=mock_events, runner=mock_runner,
            repository_host=mock_repository_host, action_applier=mock_action_applier,
            issue_branches_fn=mock_issue_branches_fn,
            session_exists_fn=lambda name: False,
            restore_sessions_fn=MagicMock(),
            launch_session_fn=lambda issue: None,
            update_queue_cache_fn=update_queue_fn,
            queue_cache_store=mock_store,
        )

        await sm.run_startup(OrchestratorState())

        # Should use delta sync, NOT fall back to full scan
        assert mock_repository_host.list_issues.call_count == 0
        assert mock_repository_host.list_issues_delta.call_count == 1
        update_queue_fn.assert_not_called()
