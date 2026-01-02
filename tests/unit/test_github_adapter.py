"""Unit tests for GitHubAdapter caching behavior."""

from unittest.mock import MagicMock, call

from issue_orchestrator.config import Config
from issue_orchestrator.adapters.github import GitHubAdapter
from issue_orchestrator.adapters.github.cache import GitHubCache
from issue_orchestrator.ports.pull_request_tracker import PRInfo


def test_update_label_cache_updates_pr_cache() -> None:
    """Test that update_label_cache also updates labels on cached PRs."""
    config = Config()
    config.github_token = "token"
    config.queue_refresh_seconds = 60

    # Create adapter with a cache
    cache = GitHubCache(default_ttl=60.0)
    adapter = GitHubAdapter("owner/repo", config=config, cache=cache)

    # Pre-populate the cache with a PR
    pr_data = {
        "number": 10,
        "title": "Test PR",
        "url": "https://github.com/owner/repo/pull/10",
        "branch": "1-test",
        "body": "body",
        "state": "open",
        "labels": ["old-label"],
        "issue_number": 1,
    }
    cache.set_pr_by_issue(1, pr_data, branch="1-test")

    # Update label cache - this should also update PR labels
    adapter.update_label_cache(1, ["new-label"])

    # Verify PR cache was updated
    cached_pr = cache.get_pr_by_issue(1)
    assert cached_pr is not None
    assert cached_pr["labels"] == ["new-label"]

    # Also verify branch cache was updated
    cached_by_branch = cache.get_pr_by_branch("1-test")
    assert cached_by_branch is not None
    assert cached_by_branch["labels"] == ["new-label"]


def test_list_issues_retries_without_cache_when_required_ids_missing() -> None:
    """Test that list_issues retries with use_cache=False when required IDs are missing.

    This is the core behavior for handling GitHub's eventual consistency:
    1. First call with use_cache=True may return stale (304) data
    2. If required_stable_ids are specified and missing, retry without cache
    """
    config = Config()
    config.github_token = "token"
    config.repo = "owner/repo"

    cache = GitHubCache(default_ttl=60.0)
    adapter = GitHubAdapter("owner/repo", config=config, cache=cache)

    # Mock the HTTP client
    mock_client = MagicMock()
    adapter._client = mock_client

    # Simulate: cached response missing the required issue
    cached_response = [
        {"number": 1, "title": "Old Issue", "labels": [], "state": "open"},
    ]
    # Fresh response includes the new issue
    fresh_response = [
        {"number": 1, "title": "Old Issue", "labels": [], "state": "open"},
        {"number": 2, "title": "New Issue", "labels": [], "state": "open"},
    ]

    # First call returns cached (missing #2), second call returns fresh
    mock_client.list_issues.side_effect = [cached_response, fresh_response]

    # Request with required_stable_ids that includes the new issue
    # stable_id for issues without external ID prefix is just the issue number as string
    required_ids = {"2"}
    issues = adapter.list_issues(required_stable_ids=required_ids)

    # Should have made two calls
    assert mock_client.list_issues.call_count == 2

    # First call should use cache
    first_call = mock_client.list_issues.call_args_list[0]
    assert first_call.kwargs.get("use_cache") is True

    # Second call should bypass cache
    second_call = mock_client.list_issues.call_args_list[1]
    assert second_call.kwargs.get("use_cache") is False

    # Result should include both issues
    assert len(issues) == 2
    assert {i.number for i in issues} == {1, 2}


def test_list_issues_no_retry_when_required_ids_found() -> None:
    """Test that list_issues doesn't retry when all required IDs are found."""
    config = Config()
    config.github_token = "token"
    config.repo = "owner/repo"

    cache = GitHubCache(default_ttl=60.0)
    adapter = GitHubAdapter("owner/repo", config=config, cache=cache)

    mock_client = MagicMock()
    adapter._client = mock_client

    # Response includes the required issue
    response = [
        {"number": 1, "title": "Issue", "labels": [], "state": "open"},
    ]
    mock_client.list_issues.return_value = response

    # Request with required_stable_ids that's already in the response
    # stable_id for issues without external ID prefix is just the issue number as string
    required_ids = {"1"}
    issues = adapter.list_issues(required_stable_ids=required_ids)

    # Should have made only one call
    assert mock_client.list_issues.call_count == 1
    assert len(issues) == 1


def test_list_issues_no_retry_without_required_ids() -> None:
    """Test that list_issues doesn't retry when no required IDs specified."""
    config = Config()
    config.github_token = "token"
    config.repo = "owner/repo"

    cache = GitHubCache(default_ttl=60.0)
    adapter = GitHubAdapter("owner/repo", config=config, cache=cache)

    mock_client = MagicMock()
    adapter._client = mock_client

    response = [
        {"number": 1, "title": "Issue", "labels": [], "state": "open"},
    ]
    mock_client.list_issues.return_value = response

    # No required_stable_ids
    issues = adapter.list_issues()

    # Should have made only one call
    assert mock_client.list_issues.call_count == 1
    assert len(issues) == 1
