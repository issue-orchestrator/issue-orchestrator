"""Unit tests for FactGatherer."""

import pytest
from unittest.mock import MagicMock, Mock
from pathlib import Path

from issue_orchestrator.control.fact_gatherer import FactGatherer
from issue_orchestrator.config import Config
from issue_orchestrator.models import (
    Issue,
    OrchestratorState,
    Session,
    AgentConfig,
    PendingReview,
    PendingRework,
    PendingCleanup,
    PendingTriageReview,
)
from issue_orchestrator.ports import PRInfo


@pytest.fixture
def mock_config():
    """Create a mock config for testing."""
    config = Config()
    config.repo = "owner/repo"
    config.max_concurrent_sessions = 3
    config.max_issues_to_start = 5
    config.triage_review_agent = None
    config.triage_review_threshold = 0
    config.triage_review_label = None
    config.code_review_agent = None
    config.code_reviewed_label = None
    config.triage_reviewed_label = None
    return config


@pytest.fixture
def mock_repository_host():
    """Create a mock RepositoryHost."""
    host = MagicMock()
    host.list_issues.return_value = []
    host.get_prs_with_label.return_value = []
    return host


@pytest.fixture
def sample_issues():
    """Create sample issues for testing."""
    return [
        Issue(number=1, title="Issue 1", labels=["agent:web"]),
        Issue(number=2, title="Issue 2", labels=["agent:web"]),
    ]


@pytest.fixture
def sample_state():
    """Create a sample orchestrator state."""
    return OrchestratorState()


@pytest.fixture
def fact_gatherer(mock_config, mock_repository_host):
    """Create a FactGatherer instance."""
    return FactGatherer(config=mock_config, repository_host=mock_repository_host)


class TestFactGathererCreateSnapshot:
    """Tests for create_snapshot method."""

    def test_create_snapshot_basic(self, fact_gatherer, sample_state, sample_issues):
        """Test creating a basic snapshot."""
        snapshot = fact_gatherer.create_snapshot(sample_state, sample_issues)

        assert len(snapshot.issues) == 2
        assert snapshot.issues[0].number == 1
        assert snapshot.paused is False
        assert snapshot.issues_started_count == 0

    def test_create_snapshot_with_active_sessions(
        self, fact_gatherer, sample_state, sample_issues, sample_agent_config, tmp_path
    ):
        """Test snapshot includes active sessions."""
        session = Session(
            issue=sample_issues[0],
            agent_config=sample_agent_config,
            tmux_session_name="issue-1",
            worktree_path=tmp_path / "worktree",
            branch_name="1-issue-1",
        )
        sample_state.active_sessions = [session]

        snapshot = fact_gatherer.create_snapshot(sample_state, sample_issues)

        assert len(snapshot.active_sessions) == 1
        assert snapshot.active_sessions[0].issue.number == 1

    def test_create_snapshot_with_pending_reviews(
        self, fact_gatherer, sample_state, sample_issues
    ):
        """Test snapshot includes pending reviews."""
        review = PendingReview(
            issue_number=1,
            pr_number=10,
            pr_url="https://github.com/owner/repo/pull/10",
            branch_name="1-issue-1",
        )
        sample_state.pending_reviews = [review]

        snapshot = fact_gatherer.create_snapshot(sample_state, sample_issues)

        assert len(snapshot.pending_reviews) == 1
        assert snapshot.pending_reviews[0].pr_number == 10

    def test_create_snapshot_with_priority_queue(
        self, fact_gatherer, sample_state, sample_issues
    ):
        """Test snapshot includes priority queue."""
        sample_state.priority_queue = [5, 3, 1]

        snapshot = fact_gatherer.create_snapshot(sample_state, sample_issues)

        assert snapshot.priority_queue == (5, 3, 1)

    def test_create_snapshot_paused_state(
        self, fact_gatherer, sample_state, sample_issues
    ):
        """Test snapshot reflects paused state."""
        sample_state.paused = True

        snapshot = fact_gatherer.create_snapshot(sample_state, sample_issues)

        assert snapshot.paused is True

    def test_create_snapshot_max_issues_to_start(
        self, fact_gatherer, sample_state, sample_issues, mock_config
    ):
        """Test snapshot includes max_issues_to_start from config."""
        mock_config.max_issues_to_start = 10

        snapshot = fact_gatherer.create_snapshot(sample_state, sample_issues)

        assert snapshot.max_issues_to_start == 10

    def test_create_snapshot_max_issues_zero_becomes_none(
        self, fact_gatherer, sample_state, sample_issues, mock_config
    ):
        """Test max_issues_to_start=0 becomes None (unlimited)."""
        mock_config.max_issues_to_start = 0

        snapshot = fact_gatherer.create_snapshot(sample_state, sample_issues)

        assert snapshot.max_issues_to_start is None


class TestFactGathererTriageFacts:
    """Tests for gather_triage_facts method."""

    def test_triage_facts_returns_none_when_not_configured(
        self, fact_gatherer, sample_state
    ):
        """Test returns None when triage review not configured."""
        result = fact_gatherer.gather_triage_facts(sample_state)

        assert result is None

    def test_triage_facts_returns_none_when_threshold_zero(
        self, fact_gatherer, sample_state, mock_config
    ):
        """Test returns None when threshold is 0."""
        mock_config.triage_review_agent = "agent:triage"
        mock_config.triage_review_threshold = 0

        result = fact_gatherer.gather_triage_facts(sample_state)

        assert result is None

    def test_triage_facts_returns_none_when_no_watch_label(
        self, fact_gatherer, sample_state, mock_config
    ):
        """Test returns None when no watch label configured."""
        mock_config.triage_review_agent = "agent:triage"
        mock_config.triage_review_threshold = 5
        mock_config.triage_review_label = None
        mock_config.code_reviewed_label = None

        result = fact_gatherer.gather_triage_facts(sample_state)

        assert result is None

    def test_triage_facts_counts_prs_with_label(
        self, fact_gatherer, sample_state, mock_config, mock_repository_host
    ):
        """Test counts PRs with the watch label."""
        mock_config.triage_review_agent = "agent:triage"
        mock_config.triage_review_threshold = 2
        mock_config.code_reviewed_label = "code-reviewed"

        mock_repository_host.get_prs_with_label.return_value = [
            PRInfo(number=10, url="...", title="PR 10", branch="b1", labels=[], body="", state="open"),
            PRInfo(number=11, url="...", title="PR 11", branch="b2", labels=[], body="", state="open"),
            PRInfo(number=12, url="...", title="PR 12", branch="b3", labels=[], body="", state="open"),
        ]
        mock_repository_host.list_issues.return_value = []

        result = fact_gatherer.gather_triage_facts(sample_state)

        assert result is not None
        assert result.pr_count == 3
        assert result.threshold == 2
        assert result.watch_label == "code-reviewed"

    def test_triage_facts_detects_existing_issue(
        self, fact_gatherer, sample_state, mock_config, mock_repository_host
    ):
        """Test detects existing triage review issue."""
        mock_config.triage_review_agent = "agent:triage"
        mock_config.triage_review_threshold = 2
        mock_config.code_reviewed_label = "code-reviewed"

        mock_repository_host.get_prs_with_label.return_value = []
        mock_repository_host.list_issues.return_value = [
            Issue(number=100, title="Batch Review: 5 PRs", labels=["agent:triage"]),
        ]

        result = fact_gatherer.gather_triage_facts(sample_state)

        assert result is not None
        assert result.existing_triage_issue == 100

    def test_triage_facts_uses_triage_label_over_code_reviewed(
        self, fact_gatherer, sample_state, mock_config, mock_repository_host
    ):
        """Test prefers triage_review_label over code_reviewed_label."""
        mock_config.triage_review_agent = "agent:triage"
        mock_config.triage_review_threshold = 2
        mock_config.triage_review_label = "ready-for-triage"
        mock_config.code_reviewed_label = "code-reviewed"

        mock_repository_host.get_prs_with_label.return_value = []
        mock_repository_host.list_issues.return_value = []

        result = fact_gatherer.gather_triage_facts(sample_state)

        assert result is not None
        assert result.watch_label == "ready-for-triage"
        mock_repository_host.get_prs_with_label.assert_called_with("ready-for-triage")


class TestFactGathererCleanupFacts:
    """Tests for gather_cleanup_facts method."""

    def test_cleanup_facts_returns_none_when_no_pending(
        self, fact_gatherer, sample_state
    ):
        """Test returns None when no pending cleanups."""
        result = fact_gatherer.gather_cleanup_facts(sample_state)

        assert result is None

    def test_cleanup_facts_returns_none_when_no_review_workflow(
        self, fact_gatherer, sample_state, mock_config
    ):
        """Test returns None when no review workflow configured."""
        sample_state.pending_cleanups = [
            PendingCleanup(
                issue_number=1,
                pr_number=10,
                pr_url="https://github.com/owner/repo/pull/10",
                branch_name="1-issue-1",
                terminal_session_name="issue-1",
                worktree_path=Path("/tmp/wt"),
            )
        ]
        mock_config.triage_review_agent = None
        mock_config.code_review_agent = None

        result = fact_gatherer.gather_cleanup_facts(sample_state)

        assert result is None

    def test_cleanup_facts_with_triage_workflow(
        self, fact_gatherer, sample_state, mock_config, mock_repository_host
    ):
        """Test cleanup facts with triage review workflow."""
        mock_config.triage_review_agent = "agent:triage"
        mock_config.triage_reviewed_label = "triage-reviewed"
        mock_config.cleanup.with_triage.close_ai_session_tabs = True
        mock_config.cleanup.with_triage.remove_worktrees = True

        sample_state.pending_cleanups = [
            PendingCleanup(
                issue_number=1,
                pr_number=10,
                pr_url="https://github.com/owner/repo/pull/10",
                branch_name="1-issue-1",
                terminal_session_name="issue-1",
                worktree_path=Path("/tmp/wt1"),
            )
        ]

        mock_repository_host.get_prs_with_label.return_value = [
            PRInfo(number=10, url="...", title="PR 10", branch="b1", labels=[], body="", state="open"),
        ]

        result = fact_gatherer.gather_cleanup_facts(sample_state)

        assert result is not None
        assert len(result.pending_cleanups) == 1
        assert 10 in result.reviewed_pr_numbers
        assert result.close_tabs is True
        assert result.remove_worktrees is True

    def test_cleanup_facts_with_code_review_workflow(
        self, fact_gatherer, sample_state, mock_config, mock_repository_host
    ):
        """Test cleanup facts with code review workflow (no triage)."""
        mock_config.triage_review_agent = None
        mock_config.code_review_agent = "agent:reviewer"
        mock_config.code_reviewed_label = "code-reviewed"
        mock_config.cleanup.without_triage.close_ai_session_tabs = False
        mock_config.cleanup.without_triage.remove_worktrees = True

        sample_state.pending_cleanups = [
            PendingCleanup(
                issue_number=1,
                pr_number=10,
                pr_url="https://github.com/owner/repo/pull/10",
                branch_name="1-issue-1",
                terminal_session_name="issue-1",
                worktree_path=Path("/tmp/wt1"),
            )
        ]

        mock_repository_host.get_prs_with_label.return_value = [
            PRInfo(number=10, url="...", title="PR 10", branch="b1", labels=[], body="", state="open"),
        ]

        result = fact_gatherer.gather_cleanup_facts(sample_state)

        assert result is not None
        assert result.close_tabs is False
        assert result.remove_worktrees is True

    def test_cleanup_facts_handles_api_error(
        self, fact_gatherer, sample_state, mock_config, mock_repository_host
    ):
        """Test handles API error gracefully."""
        mock_config.triage_review_agent = "agent:triage"
        mock_config.triage_reviewed_label = "triage-reviewed"

        sample_state.pending_cleanups = [
            PendingCleanup(
                issue_number=1,
                pr_number=10,
                pr_url="https://github.com/owner/repo/pull/10",
                branch_name="1-issue-1",
                terminal_session_name="issue-1",
                worktree_path=Path("/tmp/wt1"),
            )
        ]

        mock_repository_host.get_prs_with_label.side_effect = Exception("API error")

        result = fact_gatherer.gather_cleanup_facts(sample_state)

        assert result is None
