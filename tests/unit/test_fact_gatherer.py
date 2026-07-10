"""Unit tests for FactGatherer."""

import pytest
from unittest.mock import MagicMock, Mock, call
from pathlib import Path

from issue_orchestrator.adapters.github.http_client import GitHubHttpError
from issue_orchestrator.control.fact_gatherer import FactGatherer
from issue_orchestrator.events import EventName
from issue_orchestrator.infra.config import Config
from issue_orchestrator.domain.models import (
    Issue,
    OrchestratorState,
    Session,
    AgentConfig,
    PendingReview,
    PendingRework,
    PendingCleanup,
    PendingTriageReview,
    DiscoveredAwaitingMergeDrift,
    DiscoveredAwaitingMergeReconciliation,
)
from issue_orchestrator.domain.issue_key import FakeIssueKey
from issue_orchestrator.domain.session_key import SessionKey, TaskKind
from issue_orchestrator.ports import PRInfo
from issue_orchestrator.ports.event_sink import InMemoryEventSink
from tests.unit.session_run_helpers import make_session_run_assets


@pytest.fixture
def mock_config():
    """Create a mock config for testing."""
    config = Config()
    config.repo = "owner/repo"
    config.max_concurrent_sessions = 3
    config.filtering.max_to_start = 5
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
        issue_key = FakeIssueKey(name=str(sample_issues[0].number))
        session_key = SessionKey(issue=issue_key, task=TaskKind.CODE)
        session = Session(
            key=session_key,
            issue=sample_issues[0],
            agent_config=sample_agent_config,
            terminal_id="issue-1",
            worktree_path=tmp_path / "worktree",
            branch_name="1-issue-1",
            run_assets=make_session_run_assets(
                tmp_path / "worktree",
                session_name="issue-1",
            ),
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
            issue_key=FakeIssueKey(name="1"),
            pr_number=10,
            pr_url="https://github.com/owner/repo/pull/10",
            branch_name="1-issue-1",
            _issue_number=1,
        )
        sample_state.pending_reviews = [review]

        snapshot = fact_gatherer.create_snapshot(sample_state, sample_issues)

        assert len(snapshot.pending_reviews) == 1
        assert snapshot.pending_reviews[0].pr_number == 10

    def test_create_snapshot_includes_awaiting_merge_reconciliation_facts(
        self,
        fact_gatherer,
        sample_state,
        sample_issues,
    ):
        """Snapshot carries awaiting-merge reconciliation facts to the planner."""
        fact = DiscoveredAwaitingMergeReconciliation(
            issue_number=1,
            pr_number=10,
            pr_url="https://github.com/owner/repo/pull/10",
            status="merged",
            status_reason="PR merged; awaiting merge reconciled",
            source="pull_request",
        )
        sample_state.discovered_awaiting_merge_reconciliations = [fact]

        snapshot = fact_gatherer.create_snapshot(sample_state, sample_issues)

        assert snapshot.discovered_awaiting_merge_reconciliations == (fact,)

    def test_create_snapshot_includes_awaiting_merge_drift_facts(
        self,
        fact_gatherer,
        sample_state,
        sample_issues,
    ):
        """Snapshot carries awaiting-merge label drift facts to the planner."""
        fact = DiscoveredAwaitingMergeDrift(
            issue_number=1,
            pr_number=10,
            pr_url="https://github.com/owner/repo/pull/10",
            status_reason="PR closed; issue remains open",
        )
        sample_state.discovered_awaiting_merge_drifts = [fact]

        snapshot = fact_gatherer.create_snapshot(sample_state, sample_issues)

        assert snapshot.discovered_awaiting_merge_drifts == (fact,)

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


class TestFactGathererEvents:
    """Tests for fetch-time event emission."""

    def test_fetch_issues_emits_issues_fetched_without_label_churn(self, mock_config, mock_repository_host):
        mock_config.agents = {"agent:web": MagicMock()}
        mock_repository_host.list_issues.return_value = [
            Issue(number=1, title="Issue 1", labels=["agent:web"]),
        ]
        events = InMemoryEventSink()
        gatherer = FactGatherer(config=mock_config, repository_host=mock_repository_host, events=events)

        gatherer.fetch_issues(labels_for_agent=[], milestone=None)

        assert events.has_event(EventName.ISSUES_FETCHED.value)
        assert not events.has_event(EventName.ISSUE_LABELS_CHANGED.value)

    def test_fetch_issues_repeated_cycles_do_not_emit_label_churn(self, mock_config, mock_repository_host):
        mock_config.agents = {"agent:web": MagicMock()}
        mock_repository_host.list_issues.return_value = [
            Issue(number=1, title="Issue 1", labels=["agent:web"]),
        ]
        events = InMemoryEventSink()
        gatherer = FactGatherer(config=mock_config, repository_host=mock_repository_host, events=events)

        gatherer.fetch_issues(labels_for_agent=[], milestone=None)
        gatherer.fetch_issues(labels_for_agent=[], milestone=None)

        assert len(events.get_events(EventName.ISSUES_FETCHED.value)) == 2
        assert len(events.get_events(EventName.ISSUE_LABELS_CHANGED.value)) == 0

    def test_create_snapshot_max_issues_to_start(
        self, fact_gatherer, sample_state, sample_issues, mock_config
    ):
        """Test snapshot includes max_issues_to_start from config."""
        mock_config.filtering.max_to_start = 10

        snapshot = fact_gatherer.create_snapshot(sample_state, sample_issues)

        assert snapshot.max_issues_to_start == 10

    def test_create_snapshot_max_issues_zero_becomes_none(
        self, fact_gatherer, sample_state, sample_issues, mock_config
    ):
        """Test filtering.max_to_start=0 becomes None (unlimited)."""
        mock_config.filtering.max_to_start = 0

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

    def test_triage_facts_uses_default_watch_label_when_none_configured(
        self, fact_gatherer, sample_state, mock_config, mock_repository_host
    ):
        """With agent+threshold set, the watch label falls back to the default.

        The single watch-label owner (Config.triage_watch_label, #6768 B3)
        returns "code-reviewed" when neither label is configured — the same
        default the manifest builder always applied — instead of silently
        disabling the trigger while the manifest side stayed armed.
        """
        mock_config.triage_review_agent = "agent:triage"
        mock_config.triage_review_threshold = 5
        mock_config.triage_review_label = None
        mock_config.code_reviewed_label = None
        mock_repository_host.get_prs_with_label.return_value = []
        mock_repository_host.list_issues.return_value = []

        result = fact_gatherer.gather_triage_facts(sample_state)

        assert result is not None
        assert result.watch_label == "code-reviewed"

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

    def test_triage_facts_exclude_terminally_triaged_prs(
        self, fact_gatherer, sample_state, mock_config, mock_repository_host
    ):
        """Terminally-triaged PRs never count toward the threshold (#6768 r5).

        Fact gathering shares the manifest builder's candidate predicate;
        counting triage-reviewed/triage-failed PRs that the manifest then
        filters out is what created endless empty-batch tracking issues.
        """
        mock_config.triage_review_agent = "agent:triage"
        mock_config.triage_review_threshold = 2
        mock_config.code_reviewed_label = "code-reviewed"

        mock_repository_host.get_prs_with_label.return_value = [
            PRInfo(number=10, url="...", title="Still pending", branch="b1",
                   labels=["code-reviewed"], body="", state="open"),
            PRInfo(number=11, url="...", title="Audited", branch="b2",
                   labels=["code-reviewed", "triage-reviewed"], body="", state="open"),
            PRInfo(number=12, url="...", title="Audit failed", branch="b3",
                   labels=["code-reviewed", "triage-failed"], body="", state="open"),
        ]
        mock_repository_host.list_issues.return_value = []

        result = fact_gatherer.gather_triage_facts(sample_state)

        assert result is not None
        assert result.pr_count == 1
        assert result.prs == ((10, "Still pending"),)

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

    def test_triage_facts_ignore_closed_batch_tracking_issue(
        self, fact_gatherer, sample_state, mock_config, mock_repository_host
    ):
        """A CLOSED batch tracking issue no longer suppresses batch creation (#6768 r4).

        Successful batch completion closes the tracking issue; the existing-batch
        finder queries state="open", so the closed batch stops matching and
        existing_triage_issue clears, allowing the next threshold trigger.
        """
        mock_config.triage_review_agent = "agent:triage"
        mock_config.triage_review_threshold = 2
        mock_config.code_reviewed_label = "code-reviewed"

        closed_batch = Issue(
            number=100,
            title="Triage Batch Review: 5 PRs pending",
            labels=["agent:triage"],
        )

        def list_issues(labels=None, state="open", limit=100, **kwargs):
            # Honor GitHub state filtering: the closed batch only appears in
            # non-open queries.
            del labels, limit, kwargs
            return [] if state == "open" else [closed_batch]

        mock_repository_host.get_prs_with_label.return_value = []
        mock_repository_host.list_issues.side_effect = list_issues

        result = fact_gatherer.gather_triage_facts(sample_state)

        assert result is not None
        assert result.existing_triage_issue is None

    def test_triage_facts_ignores_existing_issue_outside_filter_label(
        self, fact_gatherer, sample_state, mock_config, mock_repository_host
    ):
        """Test filtered runs ignore triage issues outside the active label scope."""
        mock_config.triage_review_agent = "agent:triage"
        mock_config.triage_review_threshold = 2
        mock_config.code_reviewed_label = "code-reviewed"
        mock_config.filtering.label = "io:e2e:run-1"

        mock_repository_host.get_prs_with_label.return_value = []
        mock_repository_host.list_issues.return_value = [
            Issue(number=100, title="Batch Review: 5 PRs", labels=["agent:triage"]),
        ]

        result = fact_gatherer.gather_triage_facts(sample_state)

        assert result is not None
        assert result.existing_triage_issue is None

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
        mock_repository_host.get_prs_with_label.assert_called_with("ready-for-triage", state="all")

    def test_triage_facts_collects_labels_from_prs_and_linked_issues(
        self, fact_gatherer, sample_state, mock_config, mock_repository_host
    ):
        """Test collects labels from both PRs and their linked issues.

        When gathering triage facts, source_labels should include:
        - Labels directly on the PRs
        - Labels on issues linked from the PR body/title (e.g., "Fixes #123")

        This enables triage.inherit_labels to work with labels from linked issues.
        """
        mock_config.triage_review_agent = "agent:triage"
        mock_config.triage_review_threshold = 2
        mock_config.code_reviewed_label = "code-reviewed"

        # PRs have some labels and reference linked issues in their titles
        mock_repository_host.get_prs_with_label.return_value = [
            PRInfo(
                number=10,
                url="...",
                title="#100: Fix the bug",
                branch="b1",
                labels=["pr-label-1"],
                body="Fixes #101",
                state="open",
            ),
            PRInfo(
                number=11,
                url="...",
                title="PR 11 for #102",
                branch="b2",
                labels=["pr-label-2"],
                body="",
                state="open",
            ),
        ]
        mock_repository_host.list_issues.return_value = []

        # Linked issues have different labels (including one we want to inherit)
        def get_issue_side_effect(issue_num):
            issues = {
                100: Issue(number=100, title="Issue 100", labels=["agent:backend", "io-e2e-test-data"]),
                101: Issue(number=101, title="Issue 101", labels=["agent:backend", "priority:high"]),
                102: Issue(number=102, title="Issue 102", labels=["agent:frontend", "test-data"]),
            }
            return issues.get(issue_num)

        mock_repository_host.get_issue.side_effect = get_issue_side_effect

        result = fact_gatherer.gather_triage_facts(sample_state)

        assert result is not None
        # Should include PR labels
        assert "pr-label-1" in result.source_labels
        assert "pr-label-2" in result.source_labels
        # Should include labels from linked issues
        assert "agent:backend" in result.source_labels
        assert "agent:frontend" in result.source_labels
        assert "io-e2e-test-data" in result.source_labels
        assert "test-data" in result.source_labels
        assert "priority:high" in result.source_labels


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
        """Test returns CleanupFacts with defaults when no review workflow configured.

        Even without a review workflow, we return CleanupFacts so that
        immediate cleanups can still be processed.
        """
        sample_state.pending_cleanups = [
            PendingCleanup(
                issue=Issue(number=1, title="Test issue", labels=[]),
                pr_number=10,
                pr_url="https://github.com/owner/repo/pull/10",
                branch_name="1-issue-1",
                terminal_id="issue-1",
                worktree_path=Path("/tmp/wt"),
            )
        ]
        mock_config.triage_review_agent = None
        mock_config.code_review_agent = None

        result = fact_gatherer.gather_cleanup_facts(sample_state)

        # Returns CleanupFacts with empty reviewed_pr_numbers (deferred cleanups won't match)
        # but immediate_cleanups can still be processed
        assert result is not None
        assert result.reviewed_pr_numbers == frozenset()  # No review label to check
        assert len(result.pending_cleanups) == 1

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
                issue=Issue(number=1, title="Test issue", labels=[]),
                pr_number=10,
                pr_url="https://github.com/owner/repo/pull/10",
                branch_name="1-issue-1",
                terminal_id="issue-1",
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
                issue=Issue(number=1, title="Test issue", labels=[]),
                pr_number=10,
                pr_url="https://github.com/owner/repo/pull/10",
                branch_name="1-issue-1",
                terminal_id="issue-1",
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
        """Test handles API error gracefully.

        On API error, we still return CleanupFacts but with empty reviewed_pr_numbers.
        This allows immediate cleanups to still work even if deferred cleanup lookup fails.
        """
        mock_config.triage_review_agent = "agent:triage"
        mock_config.triage_reviewed_label = "triage-reviewed"

        sample_state.pending_cleanups = [
            PendingCleanup(
                issue=Issue(number=1, title="Test issue", labels=[]),
                pr_number=10,
                pr_url="https://github.com/owner/repo/pull/10",
                branch_name="1-issue-1",
                terminal_id="issue-1",
                worktree_path=Path("/tmp/wt1"),
            )
        ]

        mock_repository_host.get_prs_with_label.side_effect = Exception("API error")

        result = fact_gatherer.gather_cleanup_facts(sample_state)

        # Returns CleanupFacts with empty reviewed_pr_numbers on API error
        # Deferred cleanups won't trigger, but immediate cleanups still work
        assert result is not None
        assert result.reviewed_pr_numbers == frozenset()
        assert len(result.pending_cleanups) == 1

    def test_cleanup_facts_propagates_repository_host_error(
        self, fact_gatherer, sample_state, mock_config, mock_repository_host
    ):
        """Repository-host failures should fail the snapshot instead of hiding staleness."""
        mock_config.triage_review_agent = "agent:triage"
        mock_config.triage_reviewed_label = "triage-reviewed"
        sample_state.pending_cleanups = [
            PendingCleanup(
                issue=Issue(number=1, title="Test issue", labels=[]),
                pr_number=10,
                pr_url="https://github.com/owner/repo/pull/10",
                branch_name="1-issue-1",
                terminal_id="issue-1",
                worktree_path=Path("/tmp/wt1"),
            )
        ]
        mock_repository_host.get_prs_with_label.side_effect = GitHubHttpError(
            "GitHub unavailable",
            status_code=503,
        )

        with pytest.raises(GitHubHttpError) as exc_info:
            fact_gatherer.gather_cleanup_facts(sample_state)

        assert exc_info.value.status_code == 503


class TestFactGathererFetchIssues:
    """Tests for fetch_issues method."""

    def test_fetch_issues_multiple_milestones_dedupes(
        self, mock_config, mock_repository_host
    ):
        """Fetch across multiple milestones, deduping overlapping issues."""
        mock_config.agents = {"agent:web": Mock()}
        mock_config.filtering.milestones = ["M1", "M2"]
        mock_config.filtering.milestone = None
        mock_config.filtering.fetch_limit = 100

        issue_1 = Issue(number=1, title="Issue 1", labels=["agent:web"])
        issue_2 = Issue(number=2, title="Issue 2", labels=["agent:web"])
        mock_repository_host.list_issues.side_effect = [
            [issue_1],
            [issue_1, issue_2],
        ]

        gatherer = FactGatherer(config=mock_config, repository_host=mock_repository_host)
        results = gatherer.fetch_issues(labels_for_agent=["test-label"])

        assert [issue.number for issue in results] == [1, 2]
        assert mock_repository_host.list_issues.call_args_list == [
            call(labels=["test-label", "agent:web"], milestone="M1", limit=100, required_stable_ids=None),
            call(labels=["test-label", "agent:web"], milestone="M2", limit=100, required_stable_ids=None),
        ]

    def test_fetch_issues_uses_milestone_param_when_unfiltered(
        self, mock_config, mock_repository_host
    ):
        """Use explicit milestone when config has no filter."""
        mock_config.agents = {"agent:web": Mock()}
        mock_config.filtering.milestones = []
        mock_config.filtering.milestone = None
        mock_config.filtering.fetch_limit = 50

        issue_1 = Issue(number=1, title="Issue 1", labels=["agent:web"])
        mock_repository_host.list_issues.return_value = [issue_1]

        gatherer = FactGatherer(config=mock_config, repository_host=mock_repository_host)
        results = gatherer.fetch_issues(labels_for_agent=["test-label"], milestone="M3")

        assert [issue.number for issue in results] == [1]
        mock_repository_host.list_issues.assert_called_once_with(
            labels=["test-label", "agent:web"],
            milestone="M3",
            limit=50,
            required_stable_ids=None,
        )

    def test_fetch_issues_applies_exclude_labels_filter(
        self, mock_config, mock_repository_host
    ):
        """Test that exclude_labels filters out matching issues."""
        mock_config.agents = {"agent:web": Mock()}
        mock_config.filtering.milestones = []
        mock_config.filtering.milestone = None
        mock_config.filtering.fetch_limit = 50
        mock_config.filtering.exclude_labels = ["test-data"]  # Exclude issues with this label

        issue_1 = Issue(number=1, title="Issue 1", labels=["agent:web"])
        issue_2 = Issue(number=2, title="Issue 2", labels=["agent:web", "test-data"])  # Should be excluded
        issue_3 = Issue(number=3, title="Issue 3", labels=["agent:web", "bug"])
        mock_repository_host.list_issues.return_value = [issue_1, issue_2, issue_3]

        gatherer = FactGatherer(config=mock_config, repository_host=mock_repository_host)
        results = gatherer.fetch_issues(labels_for_agent=["test-label"])

        # Issue 2 should be excluded because it has 'test-data' label
        assert [issue.number for issue in results] == [1, 3]

    def test_fetch_issues_exclude_labels_empty_passes_all(
        self, mock_config, mock_repository_host
    ):
        """Test that empty exclude_labels passes all issues through."""
        mock_config.agents = {"agent:web": Mock()}
        mock_config.filtering.milestones = []
        mock_config.filtering.milestone = None
        mock_config.filtering.fetch_limit = 50
        mock_config.filtering.exclude_labels = []  # No exclusions

        issue_1 = Issue(number=1, title="Issue 1", labels=["agent:web"])
        issue_2 = Issue(number=2, title="Issue 2", labels=["agent:web", "test-data"])
        mock_repository_host.list_issues.return_value = [issue_1, issue_2]

        gatherer = FactGatherer(config=mock_config, repository_host=mock_repository_host)
        results = gatherer.fetch_issues(labels_for_agent=["test-label"])

        # All issues should pass through
        assert [issue.number for issue in results] == [1, 2]

    def test_fetch_issues_applies_exclude_label_prefixes_filter(
        self, mock_config, mock_repository_host
    ):
        """Issue fetch excludes issues carrying labels in excluded namespaces."""
        mock_config.agents = {"agent:web": Mock()}
        mock_config.filtering.milestones = []
        mock_config.filtering.milestone = None
        mock_config.filtering.fetch_limit = 50
        mock_config.filtering.exclude_label_prefixes = ["io:e2e:"]

        issue_1 = Issue(number=1, title="Issue 1", labels=["agent:web"])
        issue_2 = Issue(number=2, title="Issue 2", labels=["agent:web", "io:e2e:isolated-4057"])
        issue_3 = Issue(number=3, title="Issue 3", labels=["agent:web", "bug"])
        mock_repository_host.list_issues.return_value = [issue_1, issue_2, issue_3]

        gatherer = FactGatherer(config=mock_config, repository_host=mock_repository_host)
        results = gatherer.fetch_issues(labels_for_agent=["test-label"])

        assert [issue.number for issue in results] == [1, 3]
