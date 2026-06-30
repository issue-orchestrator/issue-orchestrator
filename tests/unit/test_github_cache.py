"""Unit tests for GitHub cache implementation."""

from unittest.mock import MagicMock

import pytest

from issue_orchestrator.adapters.github.cache import (
    CacheEntry,
    CacheStats,
    GitHubCache,
)
import time


class TestCacheEntry:
    """Tests for CacheEntry dataclass."""

    def test_cache_entry_defaults(self):
        """Test CacheEntry default values."""
        entry = CacheEntry(value={"test": "data"})
        assert entry.value == {"test": "data"}
        assert entry.etag is None
        assert isinstance(entry.cached_at, float)
        assert entry.ttl_seconds == 300.0

    def test_cache_entry_with_etag(self):
        """Test CacheEntry with ETag."""
        entry = CacheEntry(value={"data": "test"}, etag="W/abc123")
        assert entry.value == {"data": "test"}
        assert entry.etag == "W/abc123"

    def test_cache_entry_custom_ttl(self):
        """Test CacheEntry with custom TTL."""
        entry = CacheEntry(value=[], ttl_seconds=600.0)
        assert entry.ttl_seconds == 600.0

    def test_is_stale_fresh_entry(self):
        """Test is_stale returns False for fresh entry."""
        entry = CacheEntry(value={"data": "test"}, ttl_seconds=60.0)
        assert not entry.is_stale()

    def test_is_stale_expired_entry(self):
        """Test is_stale returns True for expired entry."""
        entry = CacheEntry(value={"data": "test"}, ttl_seconds=0.001)
        entry.cached_at = time.monotonic() - 1.0
        assert entry.is_stale()

    def test_is_stale_exactly_at_ttl(self):
        """Test is_stale at TTL boundary."""
        # Manually set cached_at to ensure we're past TTL
        entry = CacheEntry(value={"data": "test"}, ttl_seconds=1.0)
        entry.cached_at = time.monotonic() - 1.1  # Set 1.1 seconds ago
        assert entry.is_stale()


class TestCacheStats:
    """Tests for CacheStats dataclass."""

    def test_cache_stats_defaults(self):
        """Test CacheStats default values."""
        stats = CacheStats()
        assert stats.hits == 0
        assert stats.misses == 0
        assert stats.invalidations == 0
        assert stats.evictions == 0

    def test_hit_rate_zero_total(self):
        """Test hit_rate returns 0 when no hits or misses."""
        stats = CacheStats()
        assert stats.hit_rate() == 0.0

    def test_hit_rate_all_hits(self):
        """Test hit_rate with all hits."""
        stats = CacheStats(hits=10, misses=0)
        assert stats.hit_rate() == 1.0

    def test_hit_rate_all_misses(self):
        """Test hit_rate with all misses."""
        stats = CacheStats(hits=0, misses=10)
        assert stats.hit_rate() == 0.0

    def test_hit_rate_mixed(self):
        """Test hit_rate with mixed hits and misses."""
        stats = CacheStats(hits=75, misses=25)
        assert stats.hit_rate() == 0.75


class TestGitHubCacheIssues:
    """Tests for issue caching operations."""

    @pytest.fixture
    def cache(self):
        """Create a GitHubCache instance."""
        return GitHubCache(default_ttl=300.0, max_entries=1000)

    def test_get_issue_not_cached(self, cache):
        """Test getting uncached issue returns None."""
        result = cache.get_issue(123)
        assert result is None
        assert cache.stats.misses == 1
        assert cache.stats.hits == 0

    def test_set_and_get_issue(self, cache):
        """Test setting and getting issue data."""
        issue_data = {"number": 123, "title": "Test Issue"}
        cache.set_issue(123, issue_data)

        result = cache.get_issue(123)
        assert result == issue_data
        assert cache.stats.hits == 1
        assert cache.stats.misses == 0

    def test_set_issue_with_etag(self, cache):
        """Test setting issue with ETag."""
        issue_data = {"number": 123, "title": "Test"}
        cache.set_issue(123, issue_data, etag="W/abc123")

        result = cache.get_issue(123)
        assert result == issue_data

        etag = cache.get_issue_etag(123)
        assert etag == "W/abc123"

    def test_get_issue_etag_not_cached(self, cache):
        """Test getting ETag for uncached issue returns None."""
        etag = cache.get_issue_etag(999)
        assert etag is None

    def test_set_issue_with_custom_ttl(self, cache):
        """Test setting issue with custom TTL."""
        issue_data = {"number": 123, "title": "Test"}
        cache.set_issue(123, issue_data, ttl=600.0)

        # Verify it's cached
        result = cache.get_issue(123)
        assert result == issue_data

    def test_get_issue_stale_entry_evicted(self, cache):
        """Test getting stale issue evicts entry and returns None."""
        issue_data = {"number": 123, "title": "Test"}
        cache.set_issue(123, issue_data, ttl=0.001)
        cache._issues[123].cached_at = time.monotonic() - 1.0  # noqa: SLF001

        result = cache.get_issue(123)
        assert result is None
        assert cache.stats.misses == 1
        assert cache.stats.evictions == 1

    def test_invalidate_issue(self, cache):
        """Test invalidating cached issue."""
        issue_data = {"number": 123, "title": "Test"}
        cache.set_issue(123, issue_data)

        cache.invalidate_issue(123)

        result = cache.get_issue(123)
        assert result is None
        assert cache.stats.invalidations == 1

    def test_invalidate_issue_not_cached(self, cache):
        """Test invalidating non-existent issue is a no-op."""
        cache.invalidate_issue(999)
        # Should not raise, but invalidation count should not increase
        assert cache.stats.invalidations == 0

    def test_invalidate_issue_calls_hooks(self, cache):
        """Test invalidating issue calls registered hooks."""
        hook_calls = []

        def hook(issue_number):
            hook_calls.append(issue_number)

        cache.on_invalidate("issue", hook)

        issue_data = {"number": 123, "title": "Test"}
        cache.set_issue(123, issue_data)
        cache.invalidate_issue(123)

        assert hook_calls == [123]


class TestGitHubCacheIssueLabels:
    """Tests for issue labels caching operations."""

    @pytest.fixture
    def cache(self):
        """Create a GitHubCache instance."""
        return GitHubCache(default_ttl=300.0, max_entries=1000)

    def test_get_issue_labels_not_cached(self, cache):
        """Test getting uncached labels returns None."""
        result = cache.get_issue_labels(123)
        assert result is None
        assert cache.stats.misses == 1

    def test_set_and_get_issue_labels(self, cache):
        """Test setting and getting issue labels."""
        labels = ["bug", "priority:high"]
        cache.set_issue_labels(123, labels)

        result = cache.get_issue_labels(123)
        assert result == labels
        assert cache.stats.hits == 1

    def test_set_issue_labels_with_custom_ttl(self, cache):
        """Test setting labels with custom TTL."""
        labels = ["bug"]
        cache.set_issue_labels(123, labels, ttl=600.0)

        result = cache.get_issue_labels(123)
        assert result == labels

    def test_get_issue_labels_stale_entry(self, cache):
        """Test getting stale labels evicts entry."""
        labels = ["bug"]
        cache.set_issue_labels(123, labels, ttl=0.001)
        cache._issue_labels[123].cached_at = time.monotonic() - 1.0  # noqa: SLF001

        result = cache.get_issue_labels(123)
        assert result is None
        assert cache.stats.evictions == 1

    def test_invalidate_issue_labels(self, cache):
        """Test invalidating cached labels."""
        labels = ["bug", "feature"]
        cache.set_issue_labels(123, labels)

        cache.invalidate_issue_labels(123)

        result = cache.get_issue_labels(123)
        assert result is None
        assert cache.stats.invalidations == 1

    def test_invalidate_issue_labels_calls_hooks(self, cache):
        """Test invalidating labels calls registered hooks."""
        hook_calls = []
        cache.on_invalidate("issue_labels", lambda n: hook_calls.append(n))

        cache.set_issue_labels(123, ["bug"])
        cache.invalidate_issue_labels(123)

        assert hook_calls == [123]


class TestGitHubCachePRs:
    """Tests for PR caching operations."""

    @pytest.fixture
    def cache(self):
        """Create a GitHubCache instance."""
        return GitHubCache(default_ttl=300.0, max_entries=1000)

    def test_get_pr_not_cached(self, cache):
        """Test getting uncached PR returns None."""
        result = cache.get_pr(10)
        assert result is None
        assert cache.stats.misses == 1

    def test_set_and_get_pr(self, cache):
        """Test setting and getting PR by number."""
        pr_data = {"number": 10, "title": "Test PR", "state": "open"}
        cache.set_pr(10, pr_data)

        result = cache.get_pr(10)
        assert result == pr_data
        assert cache.stats.hits == 1

    def test_set_pr_with_etag(self, cache):
        """Test setting PR with ETag."""
        pr_data = {"number": 10, "title": "Test PR"}
        cache.set_pr(10, pr_data, etag="W/xyz789")

        result = cache.get_pr(10)
        assert result == pr_data

    def test_invalidate_pr(self, cache):
        """Test invalidating cached PR."""
        pr_data = {"number": 10, "title": "Test PR"}
        cache.set_pr(10, pr_data)

        cache.invalidate_pr(10)

        result = cache.get_pr(10)
        assert result is None
        assert cache.stats.invalidations == 1

    def test_invalidate_pr_clears_by_issue_and_branch_indexes(self, cache):
        """A PR-number invalidation must clear the PR from *every* index.

        Regression for #6595/#6670 F1: label writes address a PR by its own
        number, but the same PR is also cached under its linked issue and head
        branch. If invalidate_pr only cleared the by-number index, the stack
        predecessor work-gate (which reads ``get_prs_for_issue(predecessor)``)
        could keep serving stale ``code-reviewed`` labels for a PR that has
        since moved back to ``needs-rework``.
        """
        pr_data = {
            "number": 101,
            "title": "#20: predecessor",
            "branch": "20-base",
            "state": "open",
            "labels": ["code-reviewed"],
            "issue_number": 20,
        }
        # Cached under issue #20 and branch "20-base"...
        cache.set_pr_by_issue(20, pr_data, branch="20-base")
        # ...and under its own PR number.
        cache.set_pr(101, pr_data)

        # A label write addresses the PR by its OWN number (#101), not issue #20.
        cache.invalidate_pr(101)

        assert cache.get_pr(101) is None
        assert cache.get_pr_by_issue(20) is None
        assert cache.get_pr_by_branch("20-base") is None

    def test_get_pr_stale_entry(self, cache):
        """Test getting stale PR evicts entry."""
        pr_data = {"number": 10, "title": "Test PR"}
        cache.set_pr(10, pr_data, ttl=0.001)
        cache._prs_by_number[10].cached_at = time.monotonic() - 1.0  # noqa: SLF001

        result = cache.get_pr(10)
        assert result is None
        assert cache.stats.evictions == 1


class TestGitHubCachePRByIssue:
    """Tests for PR caching by issue number."""

    @pytest.fixture
    def cache(self):
        """Create a GitHubCache instance."""
        return GitHubCache(default_ttl=300.0, max_entries=1000)

    def test_get_pr_by_issue_not_cached(self, cache):
        """Test getting uncached PR by issue returns None."""
        result = cache.get_pr_by_issue(123)
        assert result is None
        assert cache.stats.misses == 1

    def test_set_and_get_pr_by_issue(self, cache):
        """Test setting and getting PR by issue number."""
        pr_data = {"number": 10, "issue_number": 123, "branch": "123-feature"}
        cache.set_pr_by_issue(123, pr_data)

        result = cache.get_pr_by_issue(123)
        assert result == pr_data
        assert cache.stats.hits == 1

    def test_set_pr_by_issue_with_branch(self, cache):
        """Test setting PR by issue also caches by branch."""
        pr_data = {"number": 10, "issue_number": 123, "branch": "123-feature"}
        cache.set_pr_by_issue(123, pr_data, branch="123-feature")

        # Should be accessible both ways
        result_by_issue = cache.get_pr_by_issue(123)
        result_by_branch = cache.get_pr_by_branch("123-feature")

        assert result_by_issue == pr_data
        assert result_by_branch == pr_data

    def test_invalidate_pr_by_issue(self, cache):
        """Test invalidating PR by issue number."""
        pr_data = {"number": 10, "issue_number": 123}
        cache.set_pr_by_issue(123, pr_data)

        cache.invalidate_pr_by_issue(123)

        result = cache.get_pr_by_issue(123)
        assert result is None
        assert cache.stats.invalidations == 1

    def test_invalidate_pr_by_issue_also_invalidates_branch(self, cache):
        """Test invalidating by issue also clears branch cache."""
        pr_data = {"number": 10, "issue_number": 123, "branch": "123-feature"}
        cache.set_pr_by_issue(123, pr_data, branch="123-feature")

        cache.invalidate_pr_by_issue(123)

        # Both should be cleared
        assert cache.get_pr_by_issue(123) is None
        assert cache.get_pr_by_branch("123-feature") is None

    def test_invalidate_pr_by_issue_calls_hooks(self, cache):
        """Test invalidating PR by issue calls hooks."""
        hook_calls = []
        cache.on_invalidate("pr_by_issue", lambda n: hook_calls.append(n))

        cache.set_pr_by_issue(123, {"number": 10})
        cache.invalidate_pr_by_issue(123)

        assert hook_calls == [123]

    def test_get_pr_by_issue_stale_entry(self, cache):
        """Test getting stale PR by issue evicts entry."""
        pr_data = {"number": 10, "issue_number": 123}
        cache.set_pr_by_issue(123, pr_data, ttl=0.001)
        cache._prs_by_issue[123].cached_at = time.monotonic() - 1.0  # noqa: SLF001

        result = cache.get_pr_by_issue(123)
        assert result is None
        assert cache.stats.evictions == 1


class TestGitHubCachePRByBranch:
    """Tests for PR caching by branch name."""

    @pytest.fixture
    def cache(self):
        """Create a GitHubCache instance."""
        return GitHubCache(default_ttl=300.0, max_entries=1000)

    def test_get_pr_by_branch_not_cached(self, cache):
        """Test getting uncached PR by branch returns None."""
        result = cache.get_pr_by_branch("feature-branch")
        assert result is None
        assert cache.stats.misses == 1

    def test_set_and_get_pr_by_branch(self, cache):
        """Test setting and getting PR by branch name."""
        pr_data = {"number": 10, "branch": "feature-branch"}
        cache.set_pr_by_branch("feature-branch", pr_data)

        result = cache.get_pr_by_branch("feature-branch")
        assert result == pr_data
        assert cache.stats.hits == 1

    def test_set_pr_by_branch_with_issue(self, cache):
        """Test setting PR by branch also caches by issue."""
        pr_data = {"number": 10, "branch": "123-feature", "issue_number": 123}
        cache.set_pr_by_branch("123-feature", pr_data, issue_number=123)

        # Should be accessible both ways
        result_by_branch = cache.get_pr_by_branch("123-feature")
        result_by_issue = cache.get_pr_by_issue(123)

        assert result_by_branch == pr_data
        assert result_by_issue == pr_data

    def test_invalidate_pr_by_branch(self, cache):
        """Test invalidating PR by branch."""
        pr_data = {"number": 10, "branch": "feature-branch"}
        cache.set_pr_by_branch("feature-branch", pr_data)

        cache.invalidate_pr_by_branch("feature-branch")

        result = cache.get_pr_by_branch("feature-branch")
        assert result is None
        assert cache.stats.invalidations == 1

    def test_invalidate_pr_by_branch_also_invalidates_issue(self, cache):
        """Test invalidating by branch also clears issue cache."""
        pr_data = {"number": 10, "branch": "123-feature", "issue_number": 123}
        cache.set_pr_by_branch("123-feature", pr_data, issue_number=123)

        cache.invalidate_pr_by_branch("123-feature")

        # Both should be cleared
        assert cache.get_pr_by_branch("123-feature") is None
        assert cache.get_pr_by_issue(123) is None

    def test_invalidate_pr_by_branch_calls_hooks(self, cache):
        """Test invalidating PR by branch calls hooks."""
        hook_calls = []
        cache.on_invalidate("pr_by_branch", lambda b: hook_calls.append(b))

        cache.set_pr_by_branch("feature-branch", {"number": 10})
        cache.invalidate_pr_by_branch("feature-branch")

        assert hook_calls == ["feature-branch"]

    def test_get_pr_by_branch_stale_entry(self, cache):
        """Test getting stale PR by branch evicts entry."""
        pr_data = {"number": 10, "branch": "feature"}
        cache.set_pr_by_branch("feature", pr_data, ttl=0.001)
        cache._prs_by_branch["feature"].cached_at = time.monotonic() - 1.0  # noqa: SLF001

        result = cache.get_pr_by_branch("feature")
        assert result is None
        assert cache.stats.evictions == 1


class TestGitHubCacheBranches:
    """Tests for branches list caching."""

    @pytest.fixture
    def cache(self):
        """Create a GitHubCache instance."""
        return GitHubCache(default_ttl=300.0, max_entries=1000)

    def test_get_branches_not_cached(self, cache):
        """Test getting uncached branches returns None."""
        result = cache.get_branches()
        assert result is None
        assert cache.stats.misses == 1

    def test_set_and_get_branches(self, cache):
        """Test setting and getting branches list."""
        branches = ["main", "feature-1", "feature-2"]
        cache.set_branches(branches)

        result = cache.get_branches()
        assert result == branches
        assert cache.stats.hits == 1

    def test_set_branches_with_custom_ttl(self, cache):
        """Test setting branches with custom TTL."""
        branches = ["main"]
        cache.set_branches(branches, ttl=600.0)

        result = cache.get_branches()
        assert result == branches

    def test_get_branches_stale_entry(self, cache):
        """Test getting stale branches evicts entry."""
        branches = ["main"]
        cache.set_branches(branches, ttl=0.001)
        cache._branches.cached_at = time.monotonic() - 1.0  # noqa: SLF001

        result = cache.get_branches()
        assert result is None
        assert cache.stats.evictions == 1

    def test_invalidate_branches(self, cache):
        """Test invalidating cached branches."""
        branches = ["main", "feature"]
        cache.set_branches(branches)

        cache.invalidate_branches()

        result = cache.get_branches()
        assert result is None
        assert cache.stats.invalidations == 1

    def test_invalidate_branches_not_cached(self, cache):
        """Test invalidating when no branches cached."""
        cache.invalidate_branches()
        # Should not increase invalidation count
        assert cache.stats.invalidations == 0

    def test_invalidate_branches_calls_hooks(self, cache):
        """Test invalidating branches calls hooks."""
        hook_calls = []
        cache.on_invalidate("branches", lambda x: hook_calls.append(x))

        cache.set_branches(["main"])
        cache.invalidate_branches()

        assert hook_calls == [None]


class TestGitHubCacheBulkOperations:
    """Tests for bulk cache operations."""

    @pytest.fixture
    def cache(self):
        """Create a GitHubCache instance."""
        return GitHubCache(default_ttl=300.0, max_entries=1000)

    def test_invalidate_all_empty_cache(self, cache):
        """Test invalidating all on empty cache."""
        cache.invalidate_all()
        assert cache.stats.invalidations == 0

    def test_invalidate_all_clears_all_caches(self, cache):
        """Test invalidate_all clears all cache types."""
        # Populate all cache types
        cache.set_issue(1, {"number": 1})
        cache.set_issue_labels(1, ["bug"])
        cache.set_pr(10, {"number": 10})
        cache.set_pr_by_issue(2, {"number": 20})
        cache.set_pr_by_branch("feature", {"number": 30})
        cache.set_branches(["main"])

        cache.invalidate_all()

        # All should be cleared
        assert cache.get_issue(1) is None
        assert cache.get_issue_labels(1) is None
        assert cache.get_pr(10) is None
        assert cache.get_pr_by_issue(2) is None
        assert cache.get_pr_by_branch("feature") is None
        assert cache.get_branches() is None

        # Should count all invalidations (6 entries)
        assert cache.stats.invalidations == 6

    def test_invalidate_all_counts_correctly(self, cache):
        """Test invalidate_all counts entries correctly."""
        cache.set_issue(1, {"number": 1})
        cache.set_issue(2, {"number": 2})
        cache.set_branches(["main"])

        cache.invalidate_all()

        assert cache.stats.invalidations == 3


class TestGitHubCacheEviction:
    """Tests for cache eviction behavior."""

    def test_eviction_when_max_entries_reached(self):
        """Test that oldest entries are evicted when max_entries reached."""
        cache = GitHubCache(default_ttl=300.0, max_entries=10)

        # Fill cache to max
        for i in range(10):
            cache.set_issue(i, {"number": i})

        # Add one more - should trigger eviction
        cache.set_issue(10, {"number": 10})

        # First entry should be evicted (oldest)
        assert cache.get_issue(0) is None
        assert cache.get_issue(10) is not None
        assert cache.stats.evictions == 1

    def test_eviction_removes_ten_percent(self):
        """Test that eviction removes 10% of entries."""
        cache = GitHubCache(default_ttl=300.0, max_entries=100)

        # Fill cache to max
        for i in range(100):
            cache.set_issue(i, {"number": i})

        # Add one more - should evict 10 entries
        cache.set_issue(100, {"number": 100})

        # First 10 should be evicted
        for i in range(10):
            assert cache.get_issue(i) is None

        # Entry 10+ should still be cached
        assert cache.get_issue(10) is not None
        assert cache.stats.evictions == 10

    def test_eviction_maintains_max_entries(self):
        """Test that cache size stays within max_entries."""
        cache = GitHubCache(default_ttl=300.0, max_entries=10)

        # Add many entries
        for i in range(20):
            cache.set_issue(i, {"number": i})

        # Count remaining entries
        summary = cache.get_stats_summary()
        assert summary["entries"]["issues"] <= 10

    def test_eviction_evicts_oldest_first(self):
        """Test that eviction prioritizes oldest entries."""
        cache = GitHubCache(default_ttl=300.0, max_entries=5)

        # Add entries with small delays to ensure ordering
        for i in range(5):
            cache.set_issue(i, {"number": i})
            cache._issues[i].cached_at = time.monotonic() + i  # noqa: SLF001

        # Add new entry - should evict oldest
        cache.set_issue(5, {"number": 5})

        # Entry 0 (oldest) should be evicted
        assert cache.get_issue(0) is None
        # Newest entries should remain
        assert cache.get_issue(4) is not None
        assert cache.get_issue(5) is not None


class TestGitHubCacheHooks:
    """Tests for cache invalidation hooks."""

    @pytest.fixture
    def cache(self):
        """Create a GitHubCache instance."""
        return GitHubCache(default_ttl=300.0, max_entries=1000)

    def test_register_hook_for_valid_cache_type(self, cache):
        """Test registering hook for valid cache type."""
        hook_calls = []

        def hook(key):
            hook_calls.append(key)

        cache.on_invalidate("issue", hook)
        cache.set_issue(123, {"number": 123})
        cache.invalidate_issue(123)

        assert hook_calls == [123]

    def test_register_hook_for_invalid_cache_type(self, cache):
        """Test registering hook for invalid cache type raises error."""
        with pytest.raises(ValueError, match="Unknown cache type"):
            cache.on_invalidate("invalid_type", lambda x: None)

    def test_multiple_hooks_for_same_type(self, cache):
        """Test multiple hooks can be registered for same type."""
        calls_1 = []
        calls_2 = []

        cache.on_invalidate("issue", lambda k: calls_1.append(k))
        cache.on_invalidate("issue", lambda k: calls_2.append(k))

        cache.set_issue(123, {"number": 123})
        cache.invalidate_issue(123)

        assert calls_1 == [123]
        assert calls_2 == [123]

    def test_hook_exception_does_not_break_invalidation(self, cache):
        """Test that hook exceptions don't prevent invalidation."""
        def failing_hook(key):
            raise RuntimeError("Hook failed!")

        cache.on_invalidate("issue", failing_hook)
        cache.set_issue(123, {"number": 123})

        # Should not raise despite hook failing
        cache.invalidate_issue(123)

        # Entry should still be invalidated
        assert cache.get_issue(123) is None
        assert cache.stats.invalidations == 1

    def test_hook_called_with_correct_key(self, cache):
        """Test hooks are called with correct key values."""
        issue_keys = []
        label_keys = []
        pr_keys = []
        branch_keys = []

        cache.on_invalidate("issue", lambda k: issue_keys.append(k))
        cache.on_invalidate("issue_labels", lambda k: label_keys.append(k))
        cache.on_invalidate("pr", lambda k: pr_keys.append(k))
        cache.on_invalidate("pr_by_branch", lambda k: branch_keys.append(k))

        cache.set_issue(1, {"number": 1})
        cache.invalidate_issue(1)

        cache.set_issue_labels(2, ["bug"])
        cache.invalidate_issue_labels(2)

        cache.set_pr(10, {"number": 10})
        cache.invalidate_pr(10)

        cache.set_pr_by_branch("feature", {"number": 20})
        cache.invalidate_pr_by_branch("feature")

        assert issue_keys == [1]
        assert label_keys == [2]
        assert pr_keys == [10]
        assert branch_keys == ["feature"]


class TestGitHubCacheStatistics:
    """Tests for cache statistics."""

    @pytest.fixture
    def cache(self):
        """Create a GitHubCache instance."""
        return GitHubCache(default_ttl=300.0, max_entries=1000)

    def test_stats_initial_state(self, cache):
        """Test stats are zeroed initially."""
        stats = cache.stats
        assert stats.hits == 0
        assert stats.misses == 0
        assert stats.invalidations == 0
        assert stats.evictions == 0

    def test_stats_track_hits(self, cache):
        """Test stats track cache hits."""
        cache.set_issue(1, {"number": 1})
        cache.get_issue(1)
        cache.get_issue(1)

        assert cache.stats.hits == 2
        assert cache.stats.misses == 0

    def test_stats_track_misses(self, cache):
        """Test stats track cache misses."""
        cache.get_issue(1)
        cache.get_issue(2)

        assert cache.stats.hits == 0
        assert cache.stats.misses == 2

    def test_stats_track_invalidations(self, cache):
        """Test stats track invalidations."""
        cache.set_issue(1, {"number": 1})
        cache.set_issue(2, {"number": 2})

        cache.invalidate_issue(1)
        cache.invalidate_issue(2)

        assert cache.stats.invalidations == 2

    def test_stats_track_evictions(self, cache):
        """Test stats track evictions."""
        # Use small max_entries to trigger eviction
        small_cache = GitHubCache(default_ttl=300.0, max_entries=2)

        small_cache.set_issue(1, {"number": 1})
        small_cache.set_issue(2, {"number": 2})
        small_cache.set_issue(3, {"number": 3})  # Triggers eviction

        assert small_cache.stats.evictions >= 1

    def test_stats_track_stale_evictions(self, cache, monkeypatch):
        """Test stats track evictions from stale entries."""
        cache.set_issue(1, {"number": 1}, ttl=0.001)
        cache._issues[1].cached_at = time.monotonic() - 1.0  # noqa: SLF001

        cache.get_issue(1)  # Should evict stale entry

        assert cache.stats.evictions == 1
        assert cache.stats.misses == 1

    def test_get_stats_summary(self, cache):
        """Test get_stats_summary returns complete summary."""
        cache.set_issue(1, {"number": 1})
        cache.set_issue_labels(2, ["bug"])
        cache.set_pr(10, {"number": 10})
        cache.set_branches(["main"])

        cache.get_issue(1)  # Hit
        cache.get_issue(2)  # Miss

        summary = cache.get_stats_summary()

        assert summary["hits"] == 1
        assert summary["misses"] == 1
        assert summary["invalidations"] == 0
        assert summary["evictions"] == 0
        assert summary["hit_rate"] == 0.5
        assert summary["entries"]["issues"] == 1
        assert summary["entries"]["issue_labels"] == 1
        assert summary["entries"]["prs_by_number"] == 1
        assert summary["entries"]["branches"] == 1

    def test_stats_summary_entry_counts(self, cache):
        """Test stats summary correctly counts entries in each cache."""
        cache.set_issue(1, {"number": 1})
        cache.set_issue(2, {"number": 2})
        cache.set_issue_labels(1, ["bug"])
        cache.set_pr(10, {"number": 10})
        cache.set_pr_by_issue(2, {"number": 20})
        cache.set_pr_by_branch("feature", {"number": 30})
        cache.set_branches(["main"])

        summary = cache.get_stats_summary()

        assert summary["entries"]["issues"] == 2
        assert summary["entries"]["issue_labels"] == 1
        assert summary["entries"]["prs_by_number"] == 1
        assert summary["entries"]["prs_by_issue"] == 1
        assert summary["entries"]["prs_by_branch"] == 1
        assert summary["entries"]["branches"] == 1


class TestGitHubCacheThreadSafety:
    """Tests for thread-safe operations."""

    @pytest.fixture
    def cache(self):
        """Create a GitHubCache instance."""
        return GitHubCache(default_ttl=300.0, max_entries=1000)

    def test_concurrent_get_set_operations(self, cache):
        """Test concurrent get/set operations don't corrupt cache."""
        import threading

        def worker(issue_num):
            for _ in range(10):
                cache.set_issue(issue_num, {"number": issue_num})
                cache.get_issue(issue_num)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Cache should be consistent
        for i in range(5):
            result = cache.get_issue(i)
            assert result is not None
            assert result["number"] == i

    def test_concurrent_invalidations(self, cache):
        """Test concurrent invalidations are thread-safe."""
        import threading

        # Pre-populate cache
        for i in range(10):
            cache.set_issue(i, {"number": i})

        def worker(issue_num):
            cache.invalidate_issue(issue_num)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # All should be invalidated
        for i in range(10):
            assert cache.get_issue(i) is None

        assert cache.stats.invalidations == 10


class TestGitHubCacheEdgeCases:
    """Tests for edge cases and corner conditions."""

    @pytest.fixture
    def cache(self):
        """Create a GitHubCache instance."""
        return GitHubCache(default_ttl=300.0, max_entries=1000)

    def test_cache_empty_data_structures(self, cache):
        """Test caching empty data structures."""
        cache.set_issue(1, {})
        cache.set_issue_labels(2, [])
        cache.set_branches([])

        assert cache.get_issue(1) == {}
        assert cache.get_issue_labels(2) == []
        assert cache.get_branches() == []

    def test_cache_with_very_short_ttl(self, cache):
        """Test cache with very short TTL expires quickly."""
        cache.set_issue(1, {"number": 1}, ttl=0.001)
        cache._issues[1].cached_at = time.monotonic() - 1.0  # noqa: SLF001

        # Should be stale
        result = cache.get_issue(1)
        assert result is None
        assert cache.stats.evictions == 1

    def test_cache_with_negative_ttl(self, cache):
        """Test cache with negative TTL expires immediately."""
        cache.set_issue(1, {"number": 1}, ttl=-1.0)

        result = cache.get_issue(1)
        assert result is None
        assert cache.stats.evictions == 1

    def test_overwrite_existing_entry(self, cache):
        """Test overwriting existing cache entry."""
        cache.set_issue(1, {"number": 1, "version": 1})
        cache.set_issue(1, {"number": 1, "version": 2})

        result = cache.get_issue(1)
        assert result["version"] == 2

    def test_cache_with_none_etag(self, cache):
        """Test caching with explicit None ETag."""
        cache.set_issue(1, {"number": 1}, etag=None)

        result = cache.get_issue(1)
        assert result is not None

        etag = cache.get_issue_etag(1)
        assert etag is None

    def test_invalidate_pr_by_issue_without_branch(self, cache):
        """Test invalidating PR by issue when no branch is set."""
        pr_data = {"number": 10}  # No branch field
        cache.set_pr_by_issue(123, pr_data)

        cache.invalidate_pr_by_issue(123)

        assert cache.get_pr_by_issue(123) is None

    def test_invalidate_pr_by_branch_without_issue(self, cache):
        """Test invalidating PR by branch when no issue is set."""
        pr_data = {"number": 10}  # No issue_number field
        cache.set_pr_by_branch("feature", pr_data)

        cache.invalidate_pr_by_branch("feature")

        assert cache.get_pr_by_branch("feature") is None

    def test_max_entries_of_one(self):
        """Test cache with max_entries of 1."""
        cache = GitHubCache(default_ttl=300.0, max_entries=1)

        cache.set_issue(1, {"number": 1})
        cache.set_issue(2, {"number": 2})  # Should evict first

        assert cache.get_issue(1) is None
        assert cache.get_issue(2) is not None

    def test_very_large_ttl(self, cache):
        """Test cache with very large TTL."""
        cache.set_issue(1, {"number": 1}, ttl=1000000.0)

        result = cache.get_issue(1)
        assert result is not None

    def test_shared_entry_between_caches(self, cache):
        """Test that shared entry objects work correctly."""
        pr_data = {"number": 10, "branch": "feature", "issue_number": 123}

        # Set by branch with issue number
        cache.set_pr_by_branch("feature", pr_data, issue_number=123)

        # Both caches should share the same entry object
        by_branch = cache.get_pr_by_branch("feature")
        by_issue = cache.get_pr_by_issue(123)

        assert by_branch is by_issue  # Same object reference

    def test_default_ttl_applied_correctly(self):
        """Test that default TTL is used when not specified."""
        cache = GitHubCache(default_ttl=100.0)

        cache.set_issue(1, {"number": 1})

        # noqa: SLF001 - Verifying TTL configuration requires internal cache access
        with cache._lock:  # noqa: SLF001
            entry = cache._issues.get(1)  # noqa: SLF001
            assert entry is not None
            assert entry.ttl_seconds == 100.0

    def test_custom_ttl_overrides_default(self):
        """Test that custom TTL overrides default."""
        cache = GitHubCache(default_ttl=100.0)

        cache.set_issue(1, {"number": 1}, ttl=200.0)

        # noqa: SLF001 - Verifying TTL configuration requires internal cache access
        with cache._lock:  # noqa: SLF001
            entry = cache._issues.get(1)  # noqa: SLF001
            assert entry is not None
            assert entry.ttl_seconds == 200.0
