"""GitHubCache - Explicit cache surface with invalidation hooks.

This module provides a small, documented cache surface for GitHub API
responses. It separates caching concerns from the HTTP transport layer.

Cache Surface (what gets cached):
- Issue labels (invalidated on label changes)
- Issue details (invalidated on issue updates)
- PR details (invalidated on PR updates)

Invalidation Rules:
- After any write operation to an entity, its cache entry is invalidated
- Cache entries have a TTL for staleness protection
- Explicit invalidate() methods for manual cache control
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Generic, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


@dataclass
class CacheEntry(Generic[T]):
    """A single cache entry with metadata."""

    value: T
    etag: str | None = None
    cached_at: float = field(default_factory=time.monotonic)
    ttl_seconds: float = 300.0  # Default 5 minute TTL

    def is_stale(self) -> bool:
        """Check if entry has exceeded its TTL."""
        return (time.monotonic() - self.cached_at) > self.ttl_seconds


@dataclass
class CacheStats:
    """Statistics for cache performance monitoring."""

    hits: int = 0
    misses: int = 0
    invalidations: int = 0
    evictions: int = 0

    def hit_rate(self) -> float:
        """Calculate cache hit rate."""
        total = self.hits + self.misses
        return self.hits / total if total > 0 else 0.0


class GitHubCache:
    """Explicit cache surface for GitHub API responses.

    This cache provides:
    - Small, documented cache surface (issues, labels, PRs)
    - Explicit invalidation rules
    - TTL-based staleness protection
    - Thread-safe operations
    - Cache statistics for monitoring

    Usage:
        cache = GitHubCache(default_ttl=300.0)

        # Cache a value
        cache.set_issue(123, issue_data, etag="abc123")

        # Get a cached value (returns None if not found or stale)
        issue = cache.get_issue(123)

        # Invalidate after a write
        cache.invalidate_issue(123)

        # Register invalidation hooks
        cache.on_invalidate("issue", lambda key: logger.info(f"Issue {key} invalidated"))
    """

    def __init__(
        self,
        default_ttl: float = 300.0,
        max_entries: int = 1000,
    ) -> None:
        """Initialize the cache.

        Args:
            default_ttl: Default time-to-live in seconds for cache entries.
            max_entries: Maximum entries per cache type before eviction.
        """
        self._default_ttl = default_ttl
        self._max_entries = max_entries
        self._lock = threading.Lock()

        # Separate caches for different entity types
        self._issues: dict[int, CacheEntry[dict[str, Any]]] = {}
        self._issue_labels: dict[int, CacheEntry[list[str]]] = {}
        self._prs_by_number: dict[int, CacheEntry[dict[str, Any]]] = {}
        self._prs_by_issue: dict[int, CacheEntry[dict[str, Any]]] = {}
        self._prs_by_branch: dict[str, CacheEntry[dict[str, Any]]] = {}
        self._branches: CacheEntry[list[str]] | None = None

        # Invalidation hooks
        self._hooks: dict[str, list[Callable[[Any], None]]] = {
            "issue": [],
            "issue_labels": [],
            "pr": [],
            "pr_by_issue": [],
            "pr_by_branch": [],
            "branches": [],
        }

        # Statistics
        self._stats = CacheStats()

    # -------------------- Issue Cache --------------------

    def get_issue(self, issue_number: int) -> dict[str, Any] | None:
        """Get cached issue data.

        Args:
            issue_number: The issue number.

        Returns:
            Cached issue data, or None if not cached or stale.
        """
        with self._lock:
            entry = self._issues.get(issue_number)
            if entry is None:
                self._stats.misses += 1
                return None
            if entry.is_stale():
                del self._issues[issue_number]
                self._stats.misses += 1
                self._stats.evictions += 1
                return None
            self._stats.hits += 1
            return entry.value

    def set_issue(
        self,
        issue_number: int,
        data: dict[str, Any],
        etag: str | None = None,
        ttl: float | None = None,
    ) -> None:
        """Cache issue data.

        Args:
            issue_number: The issue number.
            data: The issue data to cache.
            etag: Optional ETag for conditional requests.
            ttl: Optional TTL override.
        """
        with self._lock:
            self._maybe_evict(self._issues)
            self._issues[issue_number] = CacheEntry(
                value=data,
                etag=etag,
                ttl_seconds=ttl or self._default_ttl,
            )

    def get_issue_etag(self, issue_number: int) -> str | None:
        """Get ETag for conditional request."""
        with self._lock:
            entry = self._issues.get(issue_number)
            return entry.etag if entry else None

    def invalidate_issue(self, issue_number: int) -> None:
        """Invalidate cached issue data.

        Call this after any write operation that modifies the issue.
        """
        with self._lock:
            if issue_number in self._issues:
                del self._issues[issue_number]
                self._stats.invalidations += 1
        self._call_hooks("issue", issue_number)

    # -------------------- Issue Labels Cache --------------------

    def get_issue_labels(self, issue_number: int) -> list[str] | None:
        """Get cached issue labels.

        Args:
            issue_number: The issue number.

        Returns:
            Cached label list, or None if not cached or stale.
        """
        with self._lock:
            entry = self._issue_labels.get(issue_number)
            if entry is None:
                self._stats.misses += 1
                return None
            if entry.is_stale():
                del self._issue_labels[issue_number]
                self._stats.misses += 1
                self._stats.evictions += 1
                return None
            self._stats.hits += 1
            return entry.value

    def set_issue_labels(
        self,
        issue_number: int,
        labels: list[str],
        ttl: float | None = None,
    ) -> None:
        """Cache issue labels.

        Args:
            issue_number: The issue number.
            labels: The label names to cache.
            ttl: Optional TTL override.
        """
        with self._lock:
            self._maybe_evict(self._issue_labels)
            self._issue_labels[issue_number] = CacheEntry(
                value=labels,
                ttl_seconds=ttl or self._default_ttl,
            )

    def invalidate_issue_labels(self, issue_number: int) -> None:
        """Invalidate cached issue labels.

        Call this after adding/removing labels.
        """
        with self._lock:
            if issue_number in self._issue_labels:
                del self._issue_labels[issue_number]
                self._stats.invalidations += 1
        self._call_hooks("issue_labels", issue_number)

    # -------------------- PR Cache (by PR number) --------------------

    def get_pr(self, pr_number: int) -> dict[str, Any] | None:
        """Get cached PR data by PR number."""
        with self._lock:
            entry = self._prs_by_number.get(pr_number)
            if entry is None:
                self._stats.misses += 1
                return None
            if entry.is_stale():
                del self._prs_by_number[pr_number]
                self._stats.misses += 1
                self._stats.evictions += 1
                return None
            self._stats.hits += 1
            return entry.value

    def set_pr(
        self,
        pr_number: int,
        data: dict[str, Any],
        etag: str | None = None,
        ttl: float | None = None,
    ) -> None:
        """Cache PR data by PR number."""
        with self._lock:
            self._maybe_evict(self._prs_by_number)
            self._prs_by_number[pr_number] = CacheEntry(
                value=data,
                etag=etag,
                ttl_seconds=ttl or self._default_ttl,
            )

    def invalidate_pr(self, pr_number: int) -> None:
        """Invalidate cached PR data for a PR number across *every* index.

        A single PR is cached under as many as three keys: its own PR number
        (``_prs_by_number``), its linked issue number (``_prs_by_issue``), and
        its head branch (``_prs_by_branch``). Label writes address a PR by its
        own number (``add_label(pr_number, ...)``), so clearing only the
        by-number index would leave a stale copy — including stale labels —
        reachable through ``get_pr_by_issue`` / ``get_pr_by_branch``. The stack
        predecessor work-gate reads ``get_prs_for_issue(predecessor_issue)`` and
        trusts the PR's labels; without this cross-index clear a label write to
        PR ``#101`` would not invalidate the predecessor-issue ``#20`` entry, so
        the gate could keep seeing ``code-reviewed`` after the PR moved back to
        ``needs-rework`` (#6595/#6670 F1). Remove every index entry for this PR.

        Issue and PR numbers share one numbering space on GitHub, so matching by
        the cached ``number`` field targets exactly the mutated PR.
        """
        with self._lock:
            removed = self._prs_by_number.pop(pr_number, None) is not None
            for issue_number, entry in list(self._prs_by_issue.items()):
                if entry.value.get("number") == pr_number:
                    del self._prs_by_issue[issue_number]
                    removed = True
            for branch, entry in list(self._prs_by_branch.items()):
                if entry.value.get("number") == pr_number:
                    del self._prs_by_branch[branch]
                    removed = True
            if removed:
                self._stats.invalidations += 1
        self._call_hooks("pr", pr_number)

    # -------------------- PR Cache (by issue number) --------------------

    def get_pr_by_issue(self, issue_number: int) -> dict[str, Any] | None:
        """Get cached PR data by issue number."""
        with self._lock:
            entry = self._prs_by_issue.get(issue_number)
            if entry is None:
                self._stats.misses += 1
                return None
            if entry.is_stale():
                del self._prs_by_issue[issue_number]
                self._stats.misses += 1
                self._stats.evictions += 1
                return None
            self._stats.hits += 1
            return entry.value

    def set_pr_by_issue(
        self,
        issue_number: int,
        data: dict[str, Any],
        branch: str | None = None,
        ttl: float | None = None,
    ) -> None:
        """Cache PR data by issue number.

        Args:
            issue_number: The linked issue number.
            data: PR data to cache.
            branch: Optional branch name to also cache under.
            ttl: Optional TTL override.
        """
        entry = CacheEntry(
            value=data,
            ttl_seconds=ttl or self._default_ttl,
        )
        with self._lock:
            self._maybe_evict(self._prs_by_issue)
            self._prs_by_issue[issue_number] = entry
            if branch:
                self._maybe_evict(self._prs_by_branch)
                self._prs_by_branch[branch] = entry

    def invalidate_pr_by_issue(self, issue_number: int) -> None:
        """Invalidate cached PR data by issue number.

        Also invalidates the branch cache if the PR had a branch.
        """
        branch_to_remove = None
        with self._lock:
            if issue_number in self._prs_by_issue:
                cached = self._prs_by_issue.pop(issue_number)
                self._stats.invalidations += 1
                # Also remove from branch cache if present
                branch_to_remove = cached.value.get("branch")
                if branch_to_remove and branch_to_remove in self._prs_by_branch:
                    del self._prs_by_branch[branch_to_remove]
        self._call_hooks("pr_by_issue", issue_number)

    # -------------------- PR Cache (by branch) --------------------

    def get_pr_by_branch(self, branch: str) -> dict[str, Any] | None:
        """Get cached PR data by branch name."""
        with self._lock:
            entry = self._prs_by_branch.get(branch)
            if entry is None:
                self._stats.misses += 1
                return None
            if entry.is_stale():
                del self._prs_by_branch[branch]
                self._stats.misses += 1
                self._stats.evictions += 1
                return None
            self._stats.hits += 1
            return entry.value

    def set_pr_by_branch(
        self,
        branch: str,
        data: dict[str, Any],
        issue_number: int | None = None,
        ttl: float | None = None,
    ) -> None:
        """Cache PR data by branch name.

        Args:
            branch: The branch name.
            data: PR data to cache.
            issue_number: Optional issue number to also cache under.
            ttl: Optional TTL override.
        """
        entry = CacheEntry(
            value=data,
            ttl_seconds=ttl or self._default_ttl,
        )
        with self._lock:
            self._maybe_evict(self._prs_by_branch)
            self._prs_by_branch[branch] = entry
            if issue_number is not None:
                self._maybe_evict(self._prs_by_issue)
                self._prs_by_issue[issue_number] = entry

    def invalidate_pr_by_branch(self, branch: str) -> None:
        """Invalidate cached PR data by branch.

        Also invalidates the issue cache if the PR had a linked issue.
        """
        issue_to_remove = None
        with self._lock:
            if branch in self._prs_by_branch:
                cached = self._prs_by_branch.pop(branch)
                self._stats.invalidations += 1
                # Find and remove from issue cache if present
                issue_to_remove = cached.value.get("issue_number")
                if issue_to_remove and issue_to_remove in self._prs_by_issue:
                    del self._prs_by_issue[issue_to_remove]
        self._call_hooks("pr_by_branch", branch)

    # -------------------- Branches Cache --------------------

    def get_branches(self) -> list[str] | None:
        """Get cached branch list."""
        with self._lock:
            if self._branches is None:
                self._stats.misses += 1
                return None
            if self._branches.is_stale():
                self._branches = None
                self._stats.misses += 1
                self._stats.evictions += 1
                return None
            self._stats.hits += 1
            return self._branches.value

    def set_branches(self, branches: list[str], ttl: float | None = None) -> None:
        """Cache branch list."""
        with self._lock:
            self._branches = CacheEntry(
                value=branches,
                ttl_seconds=ttl or self._default_ttl,
            )

    def invalidate_branches(self) -> None:
        """Invalidate cached branch list."""
        with self._lock:
            if self._branches is not None:
                self._branches = None
                self._stats.invalidations += 1
        self._call_hooks("branches", None)

    # -------------------- Bulk Invalidation --------------------

    def invalidate_all(self) -> None:
        """Invalidate all cached data.

        Use sparingly - prefer targeted invalidation.
        """
        with self._lock:
            count = (
                len(self._issues)
                + len(self._issue_labels)
                + len(self._prs_by_number)
                + len(self._prs_by_issue)
                + len(self._prs_by_branch)
                + (1 if self._branches else 0)
            )
            self._issues.clear()
            self._issue_labels.clear()
            self._prs_by_number.clear()
            self._prs_by_issue.clear()
            self._prs_by_branch.clear()
            self._branches = None
            self._stats.invalidations += count
        logger.info("[CACHE] Invalidated all entries (count=%d)", count)

    # -------------------- Hooks --------------------

    def on_invalidate(self, cache_type: str, callback: Callable[[Any], None]) -> None:
        """Register a callback for cache invalidation.

        Args:
            cache_type: One of "issue", "issue_labels", "pr", "branches"
            callback: Function to call with the invalidated key
        """
        if cache_type not in self._hooks:
            raise ValueError(f"Unknown cache type: {cache_type}")
        self._hooks[cache_type].append(callback)

    def _call_hooks(self, cache_type: str, key: Any) -> None:
        """Call registered invalidation hooks."""
        for hook in self._hooks.get(cache_type, []):
            try:
                hook(key)
            except Exception as e:
                logger.warning("[CACHE] Hook error for %s[%s]: %s", cache_type, key, e)

    # -------------------- Internal --------------------

    def _maybe_evict(self, cache: dict) -> None:
        """Evict oldest entries if cache is full."""
        if len(cache) >= self._max_entries:
            # Evict 10% of entries (oldest first)
            to_evict = max(1, self._max_entries // 10)
            sorted_keys = sorted(
                cache.keys(),
                key=lambda k: cache[k].cached_at,
            )
            for key in sorted_keys[:to_evict]:
                del cache[key]
                self._stats.evictions += 1

    # -------------------- Statistics --------------------

    @property
    def stats(self) -> CacheStats:
        """Get cache statistics."""
        return self._stats

    def get_stats_summary(self) -> dict[str, Any]:
        """Get summary of cache statistics."""
        with self._lock:
            return {
                "hits": self._stats.hits,
                "misses": self._stats.misses,
                "invalidations": self._stats.invalidations,
                "evictions": self._stats.evictions,
                "hit_rate": self._stats.hit_rate(),
                "entries": {
                    "issues": len(self._issues),
                    "issue_labels": len(self._issue_labels),
                    "prs_by_number": len(self._prs_by_number),
                    "prs_by_issue": len(self._prs_by_issue),
                    "prs_by_branch": len(self._prs_by_branch),
                    "branches": 1 if self._branches else 0,
                },
            }
