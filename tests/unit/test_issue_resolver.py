"""Tests for GitHub issue resolver adapter.

This module tests the GitHubIssueResolver adapter which translates IssueKeys
to GitHub issue numbers via cached lookup.

Tests cover:
- Cache building by scanning issues
- Resolution with cache hits and misses
- Cache invalidation
- External ID extraction from issue titles
- Duplicate external ID detection and warning
"""

import logging
from unittest.mock import MagicMock

import pytest

from issue_orchestrator.adapters.github.issue_resolver import GitHubIssueResolver
from issue_orchestrator.domain.issue_key import GitHubIssueKey
from issue_orchestrator.domain.models import Issue
from issue_orchestrator.events import EventName


class TestGitHubIssueResolverBuildIndex:
    """Tests for the build_index method.

    Verifies that the resolver correctly scans issues and builds the
    external_id -> issue_number mapping cache.
    """

    @pytest.fixture
    def mock_tracker(self):
        """Create a mock IssueTracker."""
        return MagicMock()

    @pytest.fixture
    def mock_events(self):
        """Create a mock EventSink."""
        return MagicMock()

    @pytest.fixture
    def resolver(self, mock_tracker, mock_events):
        """Create a GitHubIssueResolver with mocked dependencies."""
        return GitHubIssueResolver(
            repo="owner/repo",
            issue_tracker=mock_tracker,
            events=mock_events,
        )

    def test_build_index_creates_cache_from_issues(self, resolver, mock_tracker):
        """Index is built from issues in the tracker."""
        mock_tracker.list_issues.return_value = [
            Issue(number=1, title="[M1-011] Fix login bug", labels=[]),
            Issue(number=2, title="[M1-012] Add logout", labels=[]),
            Issue(number=3, title="Regular issue without prefix", labels=[]),
        ]

        resolver.build_index()

        # Verify tracker was called to list issues
        mock_tracker.list_issues.assert_called_once_with(state="all", limit=500)

    def test_resolve_uses_cache_after_build_index(self, resolver, mock_tracker):
        """Subsequent resolves use the cache, not the tracker."""
        mock_tracker.list_issues.return_value = [
            Issue(number=1, title="[M1-011] Fix login bug", labels=[]),
        ]

        resolver.build_index()
        mock_tracker.reset_mock()

        # Now resolve - should use cache and not call tracker
        key = GitHubIssueKey(repo="owner/repo", external_id="M1-011")
        result = resolver.resolve(key)

        assert result == 1
        mock_tracker.list_issues.assert_not_called()

    def test_build_index_ignores_issues_without_external_id(self, resolver, mock_tracker):
        """Issues without external ID prefix are skipped."""
        mock_tracker.list_issues.return_value = [
            Issue(number=1, title="Regular issue", labels=[]),
            Issue(number=2, title="Another issue", labels=[]),
        ]

        resolver.build_index()

        key = GitHubIssueKey(repo="owner/repo", external_id="M1-999")
        result = resolver.resolve(key)

        # Should return None because no issue has this external_id
        assert result is None

    def test_build_index_with_empty_issues(self, resolver, mock_tracker):
        """Build index handles empty issue list gracefully."""
        mock_tracker.list_issues.return_value = []

        resolver.build_index()

        key = GitHubIssueKey(repo="owner/repo", external_id="M1-011")
        result = resolver.resolve(key)

        assert result is None

    def test_build_index_warns_on_duplicate_external_ids(
        self, resolver, mock_tracker, caplog
    ):
        """Duplicate external IDs are detected and warned."""
        mock_tracker.list_issues.return_value = [
            Issue(number=1, title="[M1-011] First issue", labels=[]),
            Issue(number=2, title="[M1-011] Second issue with same ID", labels=[]),
        ]

        with caplog.at_level(logging.WARNING):
            resolver.build_index()

        # Should log warning about duplicate
        assert "Duplicate external_id" in caplog.text
        assert "M1-011" in caplog.text

    def test_build_index_publishes_duplicate_event(self, resolver, mock_tracker):
        """Duplicate external IDs trigger a trace event."""
        mock_tracker.list_issues.return_value = [
            Issue(number=1, title="[M1-011] First issue", labels=[]),
            Issue(number=2, title="[M1-011] Second issue", labels=[]),
        ]

        resolver.build_index()

        # Should publish event for duplicate
        assert resolver.events.publish.called
        events_published = [
            args[0] for args, _ in resolver.events.publish.call_args_list
        ]

        duplicate_events = [
            e for e in events_published
            if e.name == EventName.RESOLVER_DUPLICATE_EXTERNAL_ID
        ]

        assert len(duplicate_events) == 1
        assert duplicate_events[0].data["external_id"] == "M1-011"
        assert duplicate_events[0].data["issue_numbers"] == [1, 2]

    def test_build_index_first_duplicate_wins_for_resolution(
        self, resolver, mock_tracker
    ):
        """When duplicates exist, the first one is cached."""
        mock_tracker.list_issues.return_value = [
            Issue(number=1, title="[M1-011] First", labels=[]),
            Issue(number=2, title="[M1-011] Second", labels=[]),
        ]

        resolver.build_index()

        key = GitHubIssueKey(repo="owner/repo", external_id="M1-011")
        result = resolver.resolve(key)

        # First one is cached
        assert result == 1


class TestGitHubIssueResolverResolve:
    """Tests for the resolve method.

    Verifies cache lookups, cache misses trigger rebuild, and proper
    handling of missing keys.
    """

    @pytest.fixture
    def mock_tracker(self):
        """Create a mock IssueTracker."""
        return MagicMock()

    @pytest.fixture
    def mock_events(self):
        """Create a mock EventSink."""
        return MagicMock()

    @pytest.fixture
    def resolver(self, mock_tracker, mock_events):
        """Create a GitHubIssueResolver with mocked dependencies."""
        return GitHubIssueResolver(
            repo="owner/repo",
            issue_tracker=mock_tracker,
            events=mock_events,
        )

    def test_resolve_returns_issue_number_on_cache_hit(self, resolver, mock_tracker):
        """Resolve returns the issue number from cache."""
        mock_tracker.list_issues.return_value = [
            Issue(number=42, title="[M1-011] Issue", labels=[]),
        ]

        resolver.build_index()

        key = GitHubIssueKey(repo="owner/repo", external_id="M1-011")
        result = resolver.resolve(key)

        assert result == 42

    def test_resolve_rebuilds_cache_on_miss(self, resolver, mock_tracker):
        """Resolve rebuilds index when key not found."""
        # First call returns one set of issues
        mock_tracker.list_issues.side_effect = [
            [Issue(number=1, title="[M1-011] First", labels=[])],
            # Second call (from resolve's rebuild) returns updated issues
            [
                Issue(number=1, title="[M1-011] First", labels=[]),
                Issue(number=2, title="[M1-012] Second", labels=[]),
            ],
        ]

        resolver.build_index()
        mock_tracker.reset_mock()
        mock_tracker.list_issues.return_value = [
            Issue(number=1, title="[M1-011] First", labels=[]),
            Issue(number=2, title="[M1-012] Second", labels=[]),
        ]

        # Resolve key not in initial cache
        key = GitHubIssueKey(repo="owner/repo", external_id="M1-012")
        result = resolver.resolve(key)

        # Should trigger rebuild and return the newly discovered issue
        assert result == 2

    def test_resolve_returns_none_for_missing_key(self, resolver, mock_tracker):
        """Resolve returns None if key cannot be resolved."""
        mock_tracker.list_issues.return_value = [
            Issue(number=1, title="[M1-011] Issue", labels=[]),
        ]

        resolver.build_index()

        key = GitHubIssueKey(repo="owner/repo", external_id="M1-999")
        result = resolver.resolve(key)

        assert result is None

    def test_resolve_handles_multiple_different_keys(self, resolver, mock_tracker):
        """Resolve works correctly with different external IDs."""
        mock_tracker.list_issues.return_value = [
            Issue(number=1, title="[M1-011] First", labels=[]),
            Issue(number=2, title="[M1-012] Second", labels=[]),
            Issue(number=3, title="[M1-013] Third", labels=[]),
        ]

        resolver.build_index()

        key1 = GitHubIssueKey(repo="owner/repo", external_id="M1-011")
        key2 = GitHubIssueKey(repo="owner/repo", external_id="M1-012")
        key3 = GitHubIssueKey(repo="owner/repo", external_id="M1-013")

        assert resolver.resolve(key1) == 1
        assert resolver.resolve(key2) == 2
        assert resolver.resolve(key3) == 3


class TestGitHubIssueResolverInvalidate:
    """Tests for the invalidate method.

    Verifies cache entries can be removed and subsequent resolves
    trigger rebuilds.
    """

    @pytest.fixture
    def mock_tracker(self):
        """Create a mock IssueTracker."""
        return MagicMock()

    @pytest.fixture
    def mock_events(self):
        """Create a mock EventSink."""
        return MagicMock()

    @pytest.fixture
    def resolver(self, mock_tracker, mock_events):
        """Create a GitHubIssueResolver with mocked dependencies."""
        return GitHubIssueResolver(
            repo="owner/repo",
            issue_tracker=mock_tracker,
            events=mock_events,
        )

    def test_invalidate_clears_cache_entry(self, resolver, mock_tracker):
        """Invalidate removes the cached entry for a key.

        Steps:
        1. Build index with an issue
        2. Verify resolve works (key is cached)
        3. Invalidate the key
        4. Change the issue list to empty
        5. Verify resolve triggers rebuild and returns None
        """
        # Step 1: Build index
        mock_tracker.list_issues.return_value = [
            Issue(number=1, title="[M1-011] Issue", labels=[]),
        ]
        resolver.build_index()

        key = GitHubIssueKey(repo="owner/repo", external_id="M1-011")

        # Step 2: Verify key is in cache
        assert resolver.resolve(key) == 1

        # Step 3: Invalidate the key
        resolver.invalidate(key)

        # Step 4: Change mock to return empty list for next resolve
        mock_tracker.list_issues.return_value = []

        # Step 5: Resolve triggers rebuild, finds nothing, returns None
        result = resolver.resolve(key)
        assert result is None

    def test_invalidate_nonexistent_key_is_safe(self, resolver, mock_tracker):
        """Invalidating a non-existent key doesn't raise an error."""
        key = GitHubIssueKey(repo="owner/repo", external_id="M1-999")

        # Should not raise
        resolver.invalidate(key)

    def test_resolve_rebuilds_after_invalidate(self, resolver, mock_tracker):
        """After invalidate, resolve triggers a rebuild."""
        call_count = [0]

        def list_issues_side_effect(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return [Issue(number=1, title="[M1-011] Issue", labels=[])]
            else:
                # Second call (rebuild after invalidate)
                return [Issue(number=1, title="[M1-011] Issue", labels=[])]

        mock_tracker.list_issues.side_effect = list_issues_side_effect

        resolver.build_index()
        key = GitHubIssueKey(repo="owner/repo", external_id="M1-011")

        resolver.invalidate(key)

        # Resolve should trigger rebuild
        result = resolver.resolve(key)
        assert result == 1
        assert call_count[0] == 2  # build_index + resolve rebuild


class TestGitHubIssueResolverGetKeyForIssue:
    """Tests for the get_key_for_issue method.

    Verifies external ID extraction from issue titles.
    """

    @pytest.fixture
    def mock_tracker(self):
        """Create a mock IssueTracker."""
        return MagicMock()

    @pytest.fixture
    def mock_events(self):
        """Create a mock EventSink."""
        return MagicMock()

    @pytest.fixture
    def resolver(self, mock_tracker, mock_events):
        """Create a GitHubIssueResolver with mocked dependencies."""
        return GitHubIssueResolver(
            repo="owner/repo",
            issue_tracker=mock_tracker,
            events=mock_events,
        )

    def test_get_key_for_issue_with_external_id(self, resolver):
        """Extract key from issue with external ID in title."""
        issue = Issue(number=1, title="[M1-011] Fix login bug", labels=[])

        key = resolver.get_key_for_issue(issue)

        assert key is not None
        assert key.repo == "owner/repo"
        assert key.external_id == "M1-011"
        assert key.stable_id() == "M1-011"

    def test_get_key_for_issue_without_external_id(self, resolver):
        """Return None for issue without external ID in title."""
        issue = Issue(number=1, title="Regular issue without prefix", labels=[])

        key = resolver.get_key_for_issue(issue)

        assert key is None

    def test_get_key_for_issue_with_whitespace(self, resolver):
        """Extract key with extra whitespace in title."""
        issue = Issue(number=1, title="[M1-011]   Fix login bug", labels=[])

        key = resolver.get_key_for_issue(issue)

        assert key is not None
        assert key.external_id == "M1-011"

    def test_get_key_for_issue_preserves_repo(self, resolver):
        """Key has the correct repo from resolver."""
        issue = Issue(number=1, title="[M1-011] Issue", labels=[])

        key = resolver.get_key_for_issue(issue)

        assert key.repo == "owner/repo"

    def test_get_key_for_issue_multiple_formats(self, resolver):
        """Extract different external ID formats."""
        test_cases = [
            ("[M1-011] Issue", "M1-011"),
            ("[M2-001] Another", "M2-001"),
            ("[M99-999] Edge case", "M99-999"),
        ]

        for title, expected_id in test_cases:
            issue = Issue(number=1, title=title, labels=[])
            key = resolver.get_key_for_issue(issue)
            assert key is not None
            assert key.external_id == expected_id


class TestGitHubIssueResolverGetAllKeys:
    """Tests for the get_all_keys method.

    Verifies retrieval of all cached keys.
    """

    @pytest.fixture
    def mock_tracker(self):
        """Create a mock IssueTracker."""
        return MagicMock()

    @pytest.fixture
    def mock_events(self):
        """Create a mock EventSink."""
        return MagicMock()

    @pytest.fixture
    def resolver(self, mock_tracker, mock_events):
        """Create a GitHubIssueResolver with mocked dependencies."""
        return GitHubIssueResolver(
            repo="owner/repo",
            issue_tracker=mock_tracker,
            events=mock_events,
        )

    def test_get_all_keys_empty_cache(self, resolver):
        """Returns empty list when cache is empty."""
        keys = resolver.get_all_keys()

        assert keys == []

    def test_get_all_keys_after_build_index(self, resolver, mock_tracker):
        """Returns all keys from built cache."""
        mock_tracker.list_issues.return_value = [
            Issue(number=1, title="[M1-011] First", labels=[]),
            Issue(number=2, title="[M1-012] Second", labels=[]),
            Issue(number=3, title="[M1-013] Third", labels=[]),
        ]

        resolver.build_index()

        keys = resolver.get_all_keys()

        assert len(keys) == 3
        external_ids = {key.external_id for key in keys}
        assert external_ids == {"M1-011", "M1-012", "M1-013"}

    def test_get_all_keys_all_have_correct_repo(self, resolver, mock_tracker):
        """All returned keys have the correct repo."""
        mock_tracker.list_issues.return_value = [
            Issue(number=1, title="[M1-011] Issue", labels=[]),
            Issue(number=2, title="[M1-012] Issue", labels=[]),
        ]

        resolver.build_index()

        keys = resolver.get_all_keys()

        assert all(key.repo == "owner/repo" for key in keys)

    def test_get_all_keys_ignores_issues_without_external_id(self, resolver, mock_tracker):
        """Keys are only returned for issues with external IDs."""
        mock_tracker.list_issues.return_value = [
            Issue(number=1, title="[M1-011] Has ID", labels=[]),
            Issue(number=2, title="No ID here", labels=[]),
            Issue(number=3, title="[M1-013] Has ID", labels=[]),
        ]

        resolver.build_index()

        keys = resolver.get_all_keys()

        assert len(keys) == 2
        external_ids = {key.external_id for key in keys}
        assert external_ids == {"M1-011", "M1-013"}

    def test_get_all_keys_after_invalidate(self, resolver, mock_tracker):
        """After invalidating a key, get_all_keys returns only the remaining keys."""
        mock_tracker.list_issues.return_value = [
            Issue(number=1, title="[M1-011] First", labels=[]),
            Issue(number=2, title="[M1-012] Second", labels=[]),
        ]

        resolver.build_index()
        assert len(resolver.get_all_keys()) == 2

        key_to_invalidate = GitHubIssueKey(repo="owner/repo", external_id="M1-011")
        resolver.invalidate(key_to_invalidate)

        keys = resolver.get_all_keys()

        # Only M1-012 remains after invalidating M1-011
        assert len(keys) == 1
        assert keys[0].external_id == "M1-012"

    def test_get_all_keys_distinct_external_ids(self, resolver, mock_tracker):
        """When duplicates exist, all are still tracked."""
        mock_tracker.list_issues.return_value = [
            Issue(number=1, title="[M1-011] First", labels=[]),
            Issue(number=2, title="[M1-011] Duplicate", labels=[]),
        ]

        resolver.build_index()

        keys = resolver.get_all_keys()

        # Only one key per external_id in the cache (first one wins)
        assert len(keys) == 1
        assert keys[0].external_id == "M1-011"
