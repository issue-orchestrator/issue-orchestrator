"""Comprehensive unit tests for GitHubAdapter.

This test suite achieves high coverage of the GitHubAdapter by mocking the
HTTP client layer and testing:
- Issue operations (get, list, create)
- Label operations (add, remove, get, has)
- PR operations (get, list, create)
- Caching behavior
- Error handling
- Write verification
"""

import pytest
from unittest.mock import MagicMock, Mock, patch, call
from issue_orchestrator.adapters.github import GitHubAdapter
from issue_orchestrator.adapters.github.cache import GitHubCache
from issue_orchestrator.adapters.github.http_client import GitHubHttpError
from issue_orchestrator.adapters.github.github_issue import GitHubIssue
from issue_orchestrator.infra.config import Config
from issue_orchestrator.ports.pull_request_tracker import PRInfo
from issue_orchestrator.domain.issue_key import GitHubIssueKey
from issue_orchestrator.ports.verification import VerificationResult, FailureType


@pytest.fixture
def mock_config():
    """Create a mock config for testing."""
    config = Config()
    config.github_token = "test-token"
    config.github_api_url = "https://api.github.com"
    config.github_http_timeout_seconds = 20.0
    config.queue_refresh_seconds = 60
    config.gh_write_verify_timeout_seconds = 5
    config.gh_write_verify_initial_delay_ms = 100
    config.gh_write_verify_max_delay_ms = 500
    config.gh_write_verify_backoff = 1.5
    config.gh_write_verify_jitter_ms = 0
    return config


@pytest.fixture
def mock_http_client():
    """Create a mock HTTP client."""
    return MagicMock()


@pytest.fixture
def mock_verification_service():
    """Create a mock verification service."""
    service = MagicMock()
    # Default to successful verification
    service.verify_condition.return_value = (VerificationResult.SUCCESS, None)
    return service


@pytest.fixture
def adapter(mock_config, mock_http_client, mock_verification_service):
    """Create an adapter with mocked dependencies."""
    cache = GitHubCache(default_ttl=60.0)
    adapter = GitHubAdapter(
        repo="owner/repo",
        config=mock_config,
        cache=cache,
        verification_service=mock_verification_service,
    )
    adapter._client = mock_http_client
    adapter._verify_writes = True
    return adapter


class TestInitialization:
    """Test adapter initialization."""

    def test_init_with_repo_string(self, mock_config):
        """Test initialization with explicit repo string."""
        adapter = GitHubAdapter(repo="owner/repo", config=mock_config)
        assert adapter.repo == "owner/repo"

    def test_init_without_repo_uses_git_remote(self, mock_config):
        """Test initialization without repo falls back to git remote."""
        with patch("issue_orchestrator.adapters.github.github_adapter.get_repo_from_git") as mock_get_repo:
            mock_get_repo.return_value = "owner/inferred-repo"
            adapter = GitHubAdapter(repo=None, config=mock_config)
            assert adapter.repo == "owner/inferred-repo"
            mock_get_repo.assert_called_once()

    def test_init_cache_enabled_when_refresh_seconds_positive(self, mock_config):
        """Test cache is enabled when queue_refresh_seconds > 0."""
        mock_config.queue_refresh_seconds = 60
        adapter = GitHubAdapter(repo="owner/repo", config=mock_config)
        assert adapter._cache_enabled is True

    def test_init_cache_disabled_when_refresh_seconds_zero(self, mock_config):
        """Test cache is disabled when queue_refresh_seconds = 0."""
        mock_config.queue_refresh_seconds = 0
        adapter = GitHubAdapter(repo="owner/repo", config=mock_config)
        assert adapter._cache_enabled is False

    def test_init_verification_service_injected(self, mock_config, mock_verification_service):
        """Test that injected verification service is used."""
        adapter = GitHubAdapter(
            repo="owner/repo",
            config=mock_config,
            verification_service=mock_verification_service,
        )
        assert adapter._verification_service is mock_verification_service


class TestIssueOperations:
    """Test issue-related operations."""

    def test_get_issue_success(self, adapter, mock_http_client):
        """Test successful issue retrieval."""
        mock_http_client.get_issue.return_value = {
            "number": 42,
            "title": "Test Issue",
            "labels": [{"name": "bug"}, {"name": "priority:high"}],
            "state": "open",
            "body": "Issue body",
            "milestone": {"title": "v1.0", "number": 1, "due_on": "2024-01-01"},
        }

        issue = adapter.get_issue(42)

        assert isinstance(issue, GitHubIssue)
        assert issue.number == 42
        assert issue.title == "Test Issue"
        assert issue.labels == ("bug", "priority:high")
        assert issue.state == "open"
        assert issue.body == "Issue body"
        assert issue.milestone == "v1.0"
        assert issue.milestone_number == 1
        mock_http_client.get_issue.assert_called_once_with(42)

    def test_get_issue_not_found(self, adapter, mock_http_client):
        """Test get_issue returns None when issue not found."""
        mock_http_client.get_issue.side_effect = GitHubHttpError("Not found", status_code=404)

        issue = adapter.get_issue(999)

        assert issue is None

    def test_get_issue_invalid_response(self, adapter, mock_http_client):
        """Test get_issue returns None on invalid response."""
        mock_http_client.get_issue.return_value = "not a dict"

        issue = adapter.get_issue(42)

        assert issue is None

    def test_list_issues_success(self, adapter, mock_http_client):
        """Test successful issue listing."""
        mock_http_client.list_issues.return_value = [
            {
                "number": 1,
                "title": "Issue 1",
                "labels": [{"name": "bug"}],
                "state": "open",
            },
            {
                "number": 2,
                "title": "Issue 2",
                "labels": [{"name": "feature"}],
                "state": "open",
            },
        ]

        issues = adapter.list_issues(labels=["bug"], state="open", limit=10)

        assert len(issues) == 2
        assert all(isinstance(i, GitHubIssue) for i in issues)
        assert issues[0].number == 1
        assert issues[1].number == 2
        mock_http_client.list_issues.assert_called_once_with(
            labels=["bug"],
            state="open",
            milestone=None,
            limit=10,
            use_cache=True,
        )

    def test_list_issues_with_milestone(self, adapter, mock_http_client):
        """Test listing issues filtered by milestone."""
        mock_http_client.list_issues.return_value = [
            {
                "number": 1,
                "title": "Issue 1",
                "labels": [],
                "state": "open",
                "milestone": {"title": "v1.0"},
            },
        ]

        issues = adapter.list_issues(milestone="v1.0")

        assert len(issues) == 1
        mock_http_client.list_issues.assert_called_once_with(
            labels=None,
            state="open",
            milestone="v1.0",
            limit=100,
            use_cache=True,
        )

    def test_list_issues_http_error_returns_empty_list(self, adapter, mock_http_client):
        """Test list_issues returns empty list on HTTP error."""
        mock_http_client.list_issues.side_effect = GitHubHttpError("API error")

        issues = adapter.list_issues()

        assert issues == []

    def test_list_issues_retries_without_cache_when_required_ids_missing(
        self, adapter, mock_http_client
    ):
        """Test list_issues retries without cache when required IDs are missing."""
        # First call returns stale data without issue 2
        cached_response = [
            {"number": 1, "title": "Issue 1", "labels": [], "state": "open"},
        ]
        # Second call returns fresh data with issue 2
        fresh_response = [
            {"number": 1, "title": "Issue 1", "labels": [], "state": "open"},
            {"number": 2, "title": "Issue 2", "labels": [], "state": "open"},
        ]
        mock_http_client.list_issues.side_effect = [cached_response, fresh_response]

        required_ids = {"2"}  # Require issue 2's stable_id
        issues = adapter.list_issues(required_stable_ids=required_ids)

        # Should have made two calls
        assert mock_http_client.list_issues.call_count == 2
        # First with cache, second without
        assert mock_http_client.list_issues.call_args_list[0].kwargs["use_cache"] is True
        assert mock_http_client.list_issues.call_args_list[1].kwargs["use_cache"] is False
        # Result should include both issues
        assert len(issues) == 2

    def test_list_issues_no_retry_when_required_ids_found(self, adapter, mock_http_client):
        """Test list_issues doesn't retry when all required IDs are present."""
        mock_http_client.list_issues.return_value = [
            {"number": 1, "title": "Issue 1", "labels": [], "state": "open"},
        ]

        required_ids = {"1"}
        issues = adapter.list_issues(required_stable_ids=required_ids)

        # Should only make one call
        assert mock_http_client.list_issues.call_count == 1
        assert len(issues) == 1

    def test_get_issue_by_key_github_key_numeric(self, adapter, mock_http_client):
        """Test get_issue_by_key with numeric GitHub key."""
        key = GitHubIssueKey(repo="owner/repo", external_id="42")
        mock_http_client.get_issue.return_value = {
            "number": 42,
            "title": "Test",
            "labels": [],
            "state": "open",
        }

        issue = adapter.get_issue_by_key(key)

        assert issue is not None
        assert issue.number == 42
        mock_http_client.get_issue.assert_called_once_with(42)

    def test_get_issue_by_key_non_numeric_returns_none(self, adapter):
        """Test get_issue_by_key returns None for non-numeric external_id."""
        key = GitHubIssueKey(repo="owner/repo", external_id="M1-011")

        issue = adapter.get_issue_by_key(key)

        assert issue is None

    def test_create_issue(self, adapter, mock_http_client, mock_verification_service):
        """Test creating a new issue."""
        mock_http_client.create_issue.return_value = 42
        mock_http_client.get_issue_labels.return_value = ["bug", "priority:high"]

        issue_number = adapter.create_issue(
            title="New Issue",
            body="Issue body",
            labels=["bug", "priority:high"],
        )

        assert issue_number == 42
        mock_http_client.create_issue.assert_called_once_with(
            title="New Issue",
            body="Issue body",
            labels=["bug", "priority:high"],
        )
        # Should verify the labels were applied
        mock_verification_service.verify_condition.assert_called_once()

    def test_create_issue_without_labels(self, adapter, mock_http_client, mock_verification_service):
        """Test creating issue without labels doesn't trigger verification."""
        mock_http_client.create_issue.return_value = 42

        issue_number = adapter.create_issue(title="New Issue", body="Body", labels=None)

        assert issue_number == 42
        # No verification should occur without labels
        mock_verification_service.verify_condition.assert_not_called()

    def test_create_issue_returns_none_on_failure(self, adapter, mock_http_client):
        """Test create_issue returns None on failure."""
        mock_http_client.create_issue.return_value = None

        issue_number = adapter.create_issue(title="Test", body="Body")

        assert issue_number is None


class TestLabelOperations:
    """Test label-related operations."""

    def test_get_issue_labels_from_cache(self, adapter):
        """Test getting labels from cache when available."""
        adapter._cache.set_issue_labels(42, ["bug", "feature"])

        labels = adapter.get_issue_labels(42)

        assert labels == ["bug", "feature"]
        # Should not call HTTP client
        adapter._client.get_issue_labels.assert_not_called()

    def test_get_issue_labels_from_api_when_cache_disabled(self, adapter, mock_http_client):
        """Test getting labels from API when cache is disabled."""
        adapter._cache_enabled = False
        mock_http_client.get_issue_labels.return_value = ["bug"]

        labels = adapter.get_issue_labels(42)

        assert labels == ["bug"]
        mock_http_client.get_issue_labels.assert_called_once_with(42)

    def test_get_issue_labels_updates_cache(self, adapter, mock_http_client):
        """Test that fetching labels updates the cache."""
        mock_http_client.get_issue_labels.return_value = ["bug", "feature"]

        labels = adapter.get_issue_labels(42)

        assert labels == ["bug", "feature"]
        # Cache should be updated
        cached = adapter._cache.get_issue_labels(42)
        assert cached == ["bug", "feature"]

    def test_get_issue_labels_error_returns_empty_list(self, adapter, mock_http_client):
        """Test get_issue_labels returns empty list on error."""
        mock_http_client.get_issue_labels.side_effect = GitHubHttpError("API error")

        labels = adapter.get_issue_labels(42)

        assert labels == []

    def test_add_label_success(self, adapter, mock_http_client, mock_verification_service):
        """Test successfully adding a label."""
        mock_http_client.get_issue_labels.return_value = ["bug"]

        adapter.add_label(42, "bug")

        mock_http_client.add_label.assert_called_once_with(42, "bug")
        # Verification should be called
        mock_verification_service.verify_condition.assert_called_once()

    def test_add_label_invalidates_cache(self, adapter, mock_http_client, mock_verification_service):
        """Test that adding a label invalidates the cache."""
        # Pre-populate cache
        adapter._cache.set_issue_labels(42, ["old-label"])
        mock_http_client.get_issue_labels.return_value = ["old-label", "new-label"]

        adapter.add_label(42, "new-label")

        # Cache should be invalidated (empty after invalidation)
        assert adapter._cache.get_issue_labels(42) is None

    def test_add_label_updates_pr_cache_labels(self, adapter, mock_http_client, mock_verification_service):
        """Test that adding a label updates PR cache labels."""
        # Pre-populate PR cache
        pr_data = {
            "number": 10,
            "title": "Test PR",
            "url": "https://github.com/owner/repo/pull/10",
            "branch": "42-test",
            "body": "body",
            "state": "open",
            "labels": ["old-label"],
            "issue_number": 42,
        }
        adapter._cache.set_pr_by_issue(42, pr_data, branch="42-test")
        mock_http_client.get_issue_labels.return_value = ["old-label", "new-label"]

        adapter.add_label(42, "new-label")

        # Cache should be invalidated
        assert adapter._cache.get_issue_labels(42) is None

    def test_add_label_verification_failure_raises(self, adapter, mock_http_client, mock_verification_service):
        """Test that add_label raises on verification failure."""
        mock_verification_service.verify_condition.return_value = (VerificationResult.FAILED_FATAL, "state")

        with pytest.raises(GitHubHttpError) as exc_info:
            adapter.add_label(42, "bug")

        assert "Failed to verify write" in str(exc_info.value)
        assert exc_info.value.is_issue_local()

    def test_remove_label_success(self, adapter, mock_http_client, mock_verification_service):
        """Test successfully removing a label."""
        mock_http_client.get_issue_labels.return_value = []

        adapter.remove_label(42, "bug")

        mock_http_client.remove_label.assert_called_once_with(42, "bug")
        mock_verification_service.verify_condition.assert_called_once()

    def test_remove_label_invalidates_cache(self, adapter, mock_http_client, mock_verification_service):
        """Test that removing a label invalidates the cache."""
        adapter._cache.set_issue_labels(42, ["bug"])
        mock_http_client.get_issue_labels.return_value = []

        adapter.remove_label(42, "bug")

        assert adapter._cache.get_issue_labels(42) is None

    def test_has_label_true(self, adapter, mock_http_client):
        """Test has_label returns True when label exists."""
        mock_http_client.get_issue_labels.return_value = ["bug", "feature"]

        assert adapter.has_label(42, "bug") is True

    def test_has_label_false(self, adapter, mock_http_client):
        """Test has_label returns False when label doesn't exist."""
        mock_http_client.get_issue_labels.return_value = ["bug"]

        assert adapter.has_label(42, "feature") is False

    def test_has_label_error_returns_false(self, adapter, mock_http_client):
        """Test has_label returns False on error."""
        mock_http_client.get_issue_labels.side_effect = GitHubHttpError("API error")

        assert adapter.has_label(42, "bug") is False

    def test_update_label_cache(self, adapter):
        """Test update_label_cache updates both issue and PR caches."""
        # Pre-populate PR cache
        pr_data = {
            "number": 10,
            "branch": "42-test",
            "labels": ["old"],
            "issue_number": 42,
        }
        adapter._cache.set_pr_by_issue(42, pr_data, branch="42-test")

        adapter.update_label_cache(42, ["new"])

        # Issue labels should be updated
        assert adapter._cache.get_issue_labels(42) == ["new"]
        # PR labels should also be updated
        cached_pr = adapter._cache.get_pr_by_issue(42)
        assert cached_pr["labels"] == ["new"]

    def test_invalidate_label_cache(self, adapter):
        """Test invalidate_label_cache removes cached labels."""
        adapter._cache.set_issue_labels(42, ["bug"])

        adapter.invalidate_label_cache(42)

        assert adapter._cache.get_issue_labels(42) is None


class TestPROperations:
    """Test PR-related operations."""

    def test_get_pr_success(self, adapter, mock_http_client):
        """Test successfully getting a PR."""
        mock_http_client.get_pr.return_value = {
            "number": 10,
            "title": "Test PR",
            "html_url": "https://github.com/owner/repo/pull/10",
            "head": {"ref": "feature-branch"},
            "body": "PR description",
            "state": "open",
            "labels": [{"name": "bug"}],
        }

        pr = adapter.get_pr(10)

        assert pr is not None
        assert pr.number == 10
        assert pr.title == "Test PR"
        assert pr.branch == "feature-branch"
        assert pr.labels == ["bug"]
        mock_http_client.get_pr.assert_called_once_with(10)

    def test_get_pr_not_found(self, adapter, mock_http_client):
        """Test get_pr returns None when PR not found."""
        mock_http_client.get_pr.side_effect = GitHubHttpError("Not found", status_code=404)

        pr = adapter.get_pr(999)

        assert pr is None

    def test_list_prs_success(self, adapter, mock_http_client):
        """Test successfully listing PRs."""
        mock_http_client.list_prs.return_value = [
            {
                "number": 10,
                "title": "PR 1",
                "html_url": "https://github.com/owner/repo/pull/10",
                "head": {"ref": "branch-1"},
                "body": "",
                "state": "open",
                "labels": [],
            },
            {
                "number": 11,
                "title": "PR 2",
                "html_url": "https://github.com/owner/repo/pull/11",
                "head": {"ref": "branch-2"},
                "body": "",
                "state": "open",
                "labels": [],
            },
        ]

        prs = adapter.list_prs(state="open", limit=10)

        assert len(prs) == 2
        assert all(isinstance(pr, PRInfo) for pr in prs)
        assert prs[0].number == 10
        assert prs[1].number == 11
        mock_http_client.list_prs.assert_called_once_with(state="open", limit=10)

    def test_list_prs_error_returns_empty_list(self, adapter, mock_http_client):
        """Test list_prs returns empty list on error."""
        mock_http_client.list_prs.side_effect = GitHubHttpError("API error")

        prs = adapter.list_prs()

        assert prs == []

    def test_get_prs_for_branch_from_cache(self, adapter, mock_http_client):
        """Test getting PRs for branch from cache."""
        pr_data = {
            "number": 10,
            "title": "Test PR",
            "url": "https://github.com/owner/repo/pull/10",
            "branch": "feature",
            "body": "",
            "state": "open",
            "labels": [],
        }
        adapter._cache.set_pr_by_branch("feature", pr_data)

        prs = adapter.get_prs_for_branch("feature")

        assert len(prs) == 1
        assert prs[0].number == 10
        # Should not call API
        mock_http_client.get_prs_for_branch.assert_not_called()

    def test_get_prs_for_branch_from_api(self, adapter, mock_http_client):
        """Test getting PRs for branch from API when not cached."""
        mock_http_client.get_prs_for_branch.return_value = [
            {
                "number": 10,
                "title": "Test PR",
                "html_url": "https://github.com/owner/repo/pull/10",
                "head": {"ref": "feature"},
                "body": "",
                "state": "open",
                "labels": [],
            },
        ]

        prs = adapter.get_prs_for_branch("feature")

        assert len(prs) == 1
        assert prs[0].branch == "feature"
        mock_http_client.get_prs_for_branch.assert_called_once_with("feature", state="open")

    def test_get_prs_for_issue_from_cache(self, adapter):
        """Test getting PRs for issue from cache."""
        pr_data = {
            "number": 10,
            "title": "#42: Test",
            "url": "https://github.com/owner/repo/pull/10",
            "branch": "42-feature",
            "body": "",
            "state": "open",
            "labels": [],
            "issue_number": 42,
        }
        adapter._cache.set_pr_by_issue(42, pr_data, branch="42-feature")

        prs = adapter.get_prs_for_issue(42)

        assert len(prs) == 1
        assert prs[0].number == 10

    def test_get_prs_for_issue_from_api(self, adapter, mock_http_client):
        """Test getting PRs for issue from API."""
        mock_http_client.get_prs_for_issue.return_value = [
            {
                "number": 10,
                "title": "#42: Test",
                "html_url": "https://github.com/owner/repo/pull/10",
                "head": {"ref": "42-feature"},
                "body": "",
                "state": "open",
                "labels": [],
            },
        ]
        # Need to mock the full PR fetch
        mock_http_client.get_pr.return_value = {
            "number": 10,
            "title": "#42: Test",
            "html_url": "https://github.com/owner/repo/pull/10",
            "head": {"ref": "42-feature"},
            "body": "",
            "state": "open",
            "labels": [],
        }

        prs = adapter.get_prs_for_issue(42)

        assert len(prs) == 1
        assert prs[0].number == 10

    def test_get_prs_with_label(self, adapter, mock_http_client):
        """Test getting PRs with a specific label."""
        mock_http_client.get_prs_with_label.return_value = [
            {
                "number": 10,
                "title": "Test PR",
                "html_url": "https://github.com/owner/repo/pull/10",
                "head": {"ref": "feature"},
                "body": "",
                "state": "open",
                "labels": [{"name": "bug"}],
            },
        ]
        # Mock full PR fetch for _fetch_pr_info_from_search
        mock_http_client.get_pr.return_value = {
            "number": 10,
            "title": "Test PR",
            "html_url": "https://github.com/owner/repo/pull/10",
            "head": {"ref": "feature"},
            "body": "",
            "state": "open",
            "labels": [{"name": "bug"}],
        }

        prs = adapter.get_prs_with_label("bug")

        assert len(prs) == 1
        assert "bug" in prs[0].labels

    def test_create_pr_success(self, adapter, mock_http_client, mock_verification_service):
        """Test creating a new PR."""
        # No existing PR
        mock_http_client.get_prs_for_branch.return_value = []
        mock_http_client.create_pr.return_value = {
            "number": 10,
            "title": "Test PR",
            "html_url": "https://github.com/owner/repo/pull/10",
            "head": {"ref": "feature"},
            "body": "PR body",
            "state": "open",
            "labels": [],
        }
        # Mock get_pr for verification
        mock_http_client.get_pr.return_value = {
            "number": 10,
            "title": "Test PR",
            "html_url": "https://github.com/owner/repo/pull/10",
            "head": {"ref": "feature"},
            "body": "PR body",
            "state": "open",
            "labels": [],
        }

        pr = adapter.create_pr(
            title="Test PR",
            body="PR body",
            head="feature",
            base="main",
        )

        assert pr.number == 10
        assert pr.title == "Test PR"
        mock_http_client.create_pr.assert_called_once_with(
            title="Test PR",
            body="PR body",
            head="feature",
            base="main",
        )
        # Should verify PR creation
        mock_verification_service.verify_condition.assert_called_once()

    def test_create_pr_returns_existing_if_present(self, adapter, mock_http_client):
        """Test create_pr returns existing PR if one exists for the branch."""
        existing_pr = PRInfo(
            number=10,
            title="Existing PR",
            url="https://github.com/owner/repo/pull/10",
            branch="feature",
            body="",
            state="open",
            labels=[],
        )
        # Mock get_prs_for_branch to return existing PR
        adapter._cache.set_pr_by_branch("feature", {
            "number": 10,
            "title": "Existing PR",
            "url": "https://github.com/owner/repo/pull/10",
            "branch": "feature",
            "body": "",
            "state": "open",
            "labels": [],
        })

        pr = adapter.create_pr(title="New PR", body="Body", head="feature")

        assert pr.number == 10
        assert pr.title == "Existing PR"
        # Should not call create API
        mock_http_client.create_pr.assert_not_called()

    def test_add_comment_success(self, adapter, mock_http_client, mock_verification_service):
        """Test successfully adding a comment."""
        mock_http_client.add_comment.return_value = "https://github.com/owner/repo/issues/42#comment"
        mock_http_client.get_issue_comments.return_value = [
            {"body": "Test comment"}
        ]

        url = adapter.add_comment(42, "Test comment")

        assert url == "https://github.com/owner/repo/issues/42#comment"
        mock_http_client.add_comment.assert_called_once_with(42, "Test comment")
        # Should verify comment was added
        mock_verification_service.verify_condition.assert_called_once()

    def test_close_pr(self, adapter, mock_http_client, mock_verification_service):
        """Test closing a PR."""
        mock_http_client.get_pr.return_value = {
            "number": 10,
            "title": "Test PR",
            "html_url": "https://github.com/owner/repo/pull/10",
            "head": {"ref": "feature"},
            "body": "",
            "state": "closed",
            "labels": [],
        }

        adapter.close_pr(10)

        mock_http_client.close_pr.assert_called_once_with(10)
        mock_verification_service.verify_condition.assert_called_once()

    def test_invalidate_pr_cache(self, adapter):
        """Test invalidating PR cache by issue and branch."""
        pr_data = {
            "number": 10,
            "branch": "feature",
            "issue_number": 42,
        }
        adapter._cache.set_pr_by_issue(42, pr_data, branch="feature")

        adapter.invalidate_pr_cache(issue_number=42)

        assert adapter._cache.get_pr_by_issue(42) is None

    def test_cache_pr_info_with_issue_number(self, adapter):
        """Test caching PR info with extracted issue number."""
        pr_info = PRInfo(
            number=10,
            title="#42: Test",
            url="https://github.com/owner/repo/pull/10",
            branch="42-feature",
            body="",
            state="open",
            labels=["bug"],
        )

        adapter._cache_pr_info(pr_info)

        # Should be cached by issue number (extracted from branch)
        cached = adapter._cache.get_pr_by_issue(42)
        assert cached is not None
        assert cached["number"] == 10


class TestCacheBehavior:
    """Test caching behavior."""

    def test_cache_disabled_no_caching(self, mock_config, mock_http_client):
        """Test that cache is bypassed when disabled."""
        mock_config.queue_refresh_seconds = 0
        adapter = GitHubAdapter(repo="owner/repo", config=mock_config)
        adapter._client = mock_http_client
        mock_http_client.get_issue_labels.return_value = ["bug"]

        # First call
        labels1 = adapter.get_issue_labels(42)
        # Second call
        labels2 = adapter.get_issue_labels(42)

        # Should call API both times (no caching)
        assert mock_http_client.get_issue_labels.call_count == 2
        assert labels1 == labels2

    def test_pr_info_from_cache_conversion(self, adapter):
        """Test converting cached PR data to PRInfo."""
        cached = {
            "number": 10,
            "title": "Test PR",
            "url": "https://github.com/owner/repo/pull/10",
            "branch": "feature",
            "body": "Body",
            "state": "open",
            "labels": ["bug", "feature"],
        }

        pr_info = adapter._pr_info_from_cache(cached)

        assert pr_info.number == 10
        assert pr_info.title == "Test PR"
        assert pr_info.branch == "feature"
        assert pr_info.labels == ["bug", "feature"]

    def test_pr_info_from_cache_returns_none_on_empty(self, adapter):
        """Test _pr_info_from_cache returns None for empty dict."""
        assert adapter._pr_info_from_cache({}) is None
        assert adapter._pr_info_from_cache(None) is None

    def test_extract_issue_number_from_branch(self, adapter):
        """Test extracting issue number from branch name."""
        assert adapter._extract_issue_number("42-feature", None) == 42
        assert adapter._extract_issue_number("123-bugfix", None) == 123

    def test_extract_issue_number_from_title(self, adapter):
        """Test extracting issue number from PR title."""
        assert adapter._extract_issue_number(None, "#42: Fix bug") == 42
        assert adapter._extract_issue_number(None, "#123: Feature") == 123

    def test_extract_issue_number_returns_none_when_not_found(self, adapter):
        """Test extract returns None when no pattern matches."""
        assert adapter._extract_issue_number("feature", "Test PR") is None


class TestWriteVerification:
    """Test write verification behavior."""

    def test_verify_write_disabled_skips_verification(self, adapter, mock_verification_service):
        """Test that verification is skipped when disabled."""
        adapter._verify_writes = False

        adapter._verify_write("test", lambda: True)

        mock_verification_service.verify_condition.assert_not_called()

    def test_verify_write_success(self, adapter, mock_verification_service):
        """Test successful write verification."""
        mock_verification_service.verify_condition.return_value = (VerificationResult.SUCCESS, None)

        # Should not raise
        adapter._verify_write("test operation", lambda: True)

        mock_verification_service.verify_condition.assert_called_once()

    def test_verify_write_timeout_raises_systemic_error(self, adapter, mock_verification_service):
        """Test that timeout raises systemic error."""
        mock_verification_service.verify_condition.return_value = (VerificationResult.TIMED_OUT, None)

        with pytest.raises(GitHubHttpError) as exc_info:
            adapter._verify_write("test operation", lambda: True)

        assert exc_info.value.is_systemic()
        assert "Timed out verifying write" in str(exc_info.value)

    def test_verify_write_failed_raises_issue_local_error(self, adapter, mock_verification_service):
        """Test that verification failure raises issue-local error."""
        mock_verification_service.verify_condition.return_value = (VerificationResult.FAILED_FATAL, "state")

        with pytest.raises(GitHubHttpError) as exc_info:
            adapter._verify_write("test operation", lambda: False, issue_number=42)

        assert exc_info.value.is_issue_local()
        assert exc_info.value.issue_number == 42
        assert "Failed to verify write" in str(exc_info.value)

    def test_verify_write_with_detail_function(self, adapter, mock_verification_service):
        """Test verification with detail function for debugging."""
        mock_verification_service.verify_condition.return_value = (VerificationResult.FAILED_FATAL, {"state": "open"})

        with pytest.raises(GitHubHttpError):
            adapter._verify_write(
                "test",
                lambda: False,
                detail_fn=lambda: {"current": "state"},
            )

        # Detail function should be used for logging


class TestRepositoryOperations:
    """Test repository-related operations."""

    def test_get_issue_state_same_repo(self, adapter, mock_http_client):
        """Test getting issue state from same repo."""
        mock_http_client.get_issue.return_value = {
            "number": 42,
            "state": "open",
            "title": "Test",
            "labels": [],
        }

        state = adapter.get_issue_state(42)

        assert state == "open"
        mock_http_client.get_issue.assert_called_once_with(42)

    def test_get_issue_state_different_repo(self, adapter, mock_http_client):
        """Test getting issue state from different repo creates temp client."""
        # Mock the temp client creation
        with patch("issue_orchestrator.adapters.github.github_adapter.GitHubHttpClient") as mock_client_class:
            mock_temp_client = MagicMock()
            mock_client_class.return_value = mock_temp_client
            mock_temp_client.get_issue.return_value = {
                "number": 42,
                "state": "closed",
                "title": "Test",
                "labels": [],
            }

            state = adapter.get_issue_state(42, repo="other/repo")

            assert state == "closed"
            mock_temp_client.get_issue.assert_called_once_with(42)
            mock_temp_client.close.assert_called_once()

    def test_get_issue_state_not_found_returns_none(self, adapter, mock_http_client):
        """Test get_issue_state returns None when issue not found."""
        mock_http_client.get_issue.side_effect = GitHubHttpError("Not found")

        state = adapter.get_issue_state(999)

        assert state is None

    def test_create_issue_key_with_external_id(self, adapter, mock_http_client):
        """Test creating issue key with external ID from title."""
        mock_http_client.get_issue.return_value = {
            "number": 42,
            "title": "[M1-011] Test Issue",
            "labels": [],
            "state": "open",
        }

        key = adapter.create_issue_key(42)

        assert isinstance(key, GitHubIssueKey)
        assert key.external_id == "M1-011"
        assert key.repo == "owner/repo"

    def test_create_issue_key_fallback_to_number(self, adapter, mock_http_client):
        """Test creating issue key falls back to number if no external ID."""
        mock_http_client.get_issue.return_value = {
            "number": 42,
            "title": "Test Issue",
            "labels": [],
            "state": "open",
        }

        key = adapter.create_issue_key(42)

        assert key.external_id == "42"

    def test_create_issue_key_when_issue_not_found(self, adapter, mock_http_client):
        """Test creating issue key when issue fetch fails."""
        mock_http_client.get_issue.return_value = None

        key = adapter.create_issue_key(42)

        # Should fall back to issue number
        assert key.external_id == "42"

    def test_list_branches(self, adapter, mock_http_client):
        """Test listing branches."""
        mock_http_client.list_branches.return_value = ["main", "feature", "bugfix"]

        branches = adapter.list_branches()

        assert branches == ["main", "feature", "bugfix"]

    def test_branch_exists(self, adapter, mock_http_client):
        """Test checking if branch exists."""
        mock_http_client.branch_exists.return_value = True

        exists = adapter.branch_exists("feature")

        assert exists is True
        mock_http_client.branch_exists.assert_called_once_with("feature")

    def test_delete_branch(self, adapter, mock_http_client, mock_verification_service):
        """Test deleting a branch."""
        mock_http_client.branch_exists.return_value = False

        adapter.delete_branch("feature")

        mock_http_client.delete_branch.assert_called_once_with("feature")
        mock_verification_service.verify_condition.assert_called_once()

    def test_update_issue_state(self, adapter, mock_http_client, mock_verification_service):
        """Test updating issue state."""
        mock_http_client.get_issue.return_value = {
            "number": 42,
            "state": "closed",
            "title": "Test",
            "labels": [],
        }

        adapter.update_issue_state(42, "closed")

        mock_http_client.update_issue_state.assert_called_once_with(42, "closed")
        mock_verification_service.verify_condition.assert_called_once()

    def test_get_issue_comments(self, adapter, mock_http_client):
        """Test getting issue comments."""
        mock_http_client.get_issue_comments.return_value = [
            {"id": 1, "body": "Comment 1"},
            {"id": 2, "body": "Comment 2"},
        ]

        comments = adapter.get_issue_comments(42)

        assert len(comments) == 2
        assert comments[0]["body"] == "Comment 1"

    def test_list_labels(self, adapter, mock_http_client):
        """Test listing repository labels."""
        mock_http_client.list_labels.return_value = [
            {"name": "bug", "color": "ff0000"},
            {"name": "feature", "color": "00ff00"},
        ]

        labels = adapter.list_labels()

        assert len(labels) == 2
        assert labels[0]["name"] == "bug"

    def test_create_label(self, adapter, mock_http_client, mock_verification_service):
        """Test creating a label."""
        mock_http_client.list_labels.return_value = [
            {"name": "new-label", "color": "ededed"},
        ]

        adapter.create_label("new-label", color="ededed", description="Test label")

        mock_http_client.create_label.assert_called_once_with(
            "new-label",
            color="ededed",
            description="Test label",
            force=False,
        )
        mock_verification_service.verify_condition.assert_called_once()

    def test_create_label_force_updates_existing(self, adapter, mock_http_client, mock_verification_service):
        """Test creating label with force updates existing label."""
        mock_http_client.list_labels.return_value = [
            {"name": "existing", "color": "000000"},
        ]

        adapter.create_label("existing", color="ffffff", force=True)

        mock_http_client.create_label.assert_called_once_with(
            "existing",
            color="ffffff",
            description=None,
            force=True,
        )

    def test_delete_label(self, adapter, mock_http_client, mock_verification_service):
        """Test deleting a label."""
        mock_http_client.list_labels.return_value = []

        adapter.delete_label("old-label")

        mock_http_client.delete_label.assert_called_once_with("old-label")
        mock_verification_service.verify_condition.assert_called_once()

    def test_get_rate_limit_snapshot(self, adapter, mock_http_client):
        """Test getting rate limit snapshot."""
        from issue_orchestrator.adapters.github.http_client import GitHubRateLimitSnapshot

        mock_snapshot = GitHubRateLimitSnapshot(
            core_remaining=5000,
            core_limit=5000,
            core_reset=1234567890,
            search_remaining=30,
            search_limit=30,
            search_reset=1234567890,
            graphql_remaining=5000,
            graphql_limit=5000,
            graphql_reset=1234567890,
        )
        mock_http_client.get_rate_limit_snapshot.return_value = mock_snapshot

        snapshot = adapter.get_rate_limit_snapshot()

        assert snapshot is not None
        assert snapshot["core"]["remaining"] == 5000

    def test_get_rate_limit_snapshot_none(self, adapter, mock_http_client):
        """Test getting rate limit snapshot returns None when not available."""
        mock_http_client.get_rate_limit_snapshot.return_value = None

        snapshot = adapter.get_rate_limit_snapshot()

        assert snapshot is None

    def test_get_token_scopes(self, adapter, mock_http_client):
        """Test getting token scopes."""
        mock_http_client.get_token_scopes.return_value = ["repo", "read:org"]

        scopes = adapter.get_token_scopes()

        assert scopes == ["repo", "read:org"]


class TestEdgeCases:
    """Test edge cases and error handling."""

    def test_pr_info_from_api_missing_number(self, adapter):
        """Test _pr_info_from_api handles missing number."""
        pr_data = {
            "number": None,
            "title": "Test",
            "html_url": "https://github.com/owner/repo/pull/0",
            "head": {"ref": "feature"},
            "body": "",
            "state": "open",
            "labels": [],
        }

        pr_info = adapter._pr_info_from_api(pr_data)

        assert pr_info.number == 0

    def test_pr_info_from_api_invalid_number(self, adapter):
        """Test _pr_info_from_api handles invalid number."""
        pr_data = {
            "number": "not-a-number",
            "title": "Test",
            "html_url": "https://github.com/owner/repo/pull/0",
            "head": {"ref": "feature"},
            "body": "",
            "state": "open",
            "labels": [],
        }

        pr_info = adapter._pr_info_from_api(pr_data)

        assert pr_info.number == 0

    def test_pr_info_from_api_handles_missing_labels(self, adapter):
        """Test _pr_info_from_api handles missing or invalid labels."""
        pr_data = {
            "number": 10,
            "title": "Test",
            "html_url": "https://github.com/owner/repo/pull/10",
            "head": {"ref": "feature"},
            "body": "",
            "state": "open",
            "labels": [
                {"name": "bug"},
                "invalid-label",  # Not a dict
                {"not_name": "feature"},  # Missing 'name' key
            ],
        }

        pr_info = adapter._pr_info_from_api(pr_data)

        # Should only include valid labels
        assert pr_info.labels == ["bug"]

    def test_pr_info_from_api_uses_headRefName_fallback(self, adapter):
        """Test _pr_info_from_api uses headRefName when head.ref not available."""
        pr_data = {
            "number": 10,
            "title": "Test",
            "html_url": "https://github.com/owner/repo/pull/10",
            "head": {},
            "headRefName": "fallback-branch",
            "body": "",
            "state": "open",
            "labels": [],
        }

        pr_info = adapter._pr_info_from_api(pr_data)

        assert pr_info.branch == "fallback-branch"

    def test_fetch_pr_info_from_search_invalid_data(self, adapter):
        """Test _fetch_pr_info_from_search handles invalid data."""
        # Not a dict
        assert adapter._fetch_pr_info_from_search("not-a-dict") is None
        # Missing number
        assert adapter._fetch_pr_info_from_search({}) is None

    def test_fetch_pr_info_from_search_full_fetch_fails(self, adapter, mock_http_client):
        """Test _fetch_pr_info_from_search when full fetch fails."""
        mock_http_client.get_pr.return_value = None

        result = adapter._fetch_pr_info_from_search({"number": 10})

        assert result is None

    def test_list_issues_filters_invalid_items(self, adapter, mock_http_client):
        """Test list_issues filters out non-dict items."""
        mock_http_client.list_issues.return_value = [
            {"number": 1, "title": "Valid", "labels": [], "state": "open"},
            "invalid-item",
            None,
            {"number": 2, "title": "Also valid", "labels": [], "state": "open"},
        ]

        issues = adapter.list_issues()

        # Should only include valid items
        assert len(issues) == 2
        assert issues[0].number == 1
        assert issues[1].number == 2

    def test_list_prs_filters_invalid_items(self, adapter, mock_http_client):
        """Test list_prs filters out non-dict items."""
        mock_http_client.list_prs.return_value = [
            {"number": 10, "title": "Valid", "html_url": "url", "head": {"ref": "feature"}, "body": "", "state": "open", "labels": []},
            "invalid-item",
            {"number": 11, "title": "Also valid", "html_url": "url", "head": {"ref": "feature2"}, "body": "", "state": "open", "labels": []},
        ]

        prs = adapter.list_prs()

        # Should only include valid items
        assert len(prs) == 2

    def test_list_all_labels(self, adapter, mock_http_client):
        """Test listing all labels with pagination."""
        mock_http_client.list_all_labels.return_value = [
            {"name": "label1"},
            {"name": "label2"},
            {"name": "label3"},
        ]

        labels = adapter.list_all_labels()

        assert len(labels) == 3
        assert labels[0]["name"] == "label1"
