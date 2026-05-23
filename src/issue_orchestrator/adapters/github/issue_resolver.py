"""GitHub issue resolver - translates IssueKeys to GitHub issue numbers.

This is the GitHub-specific implementation of IssueResolver.

Lookup order on resolve():
1. In-memory cache (instant; populated by past resolves or build_index).
2. Negative-result cache (skip search call if we recently asked and got nothing).
3. Targeted /search/issues call against GitHub's index (one call per unique key).

Notably, on a miss we do NOT re-scan the issue list. The legacy behavior
(full pagination on every cache miss) couldn't find older closed issues
without paginating through huge windows, and burned REST quota doing so.
Search is one targeted call regardless of repo size — at the cost of moving
onto the search quota lane (30/min), which is why the negative-result cache
exists: a typo'd dep ref would otherwise re-fire a search every tick.

build_index() is still callable as an optional seed (one REST page of
recent issues) but is not invoked automatically.
"""

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from ...domain.issue_key import IssueKey, IssueHandle, GitHubIssueKey, parse_external_id
from ...events import EventName
from ...ports import IssueTracker, EventSink, TraceEvent
from ...infra import gh_audit

if TYPE_CHECKING:
    from ...domain.models import Issue

logger = logging.getLogger(__name__)

# How long a "we searched and didn't find it" answer stays cached before
# we'll search again. Long enough that persistently-missing keys (typos,
# deleted issues) don't burn search quota; short enough that newly-created
# issues become resolvable within a day.
DEFAULT_NEGATIVE_TTL = timedelta(hours=24)

# Periodic cumulative-stats log every Nth search call so cold-start cost is
# visible in ops without per-call grep gymnastics.
STATS_LOG_INTERVAL = 10


@dataclass
class GitHubIssueResolver:
    """Resolves IssueKeys to GitHub issue numbers via cache + targeted search.

    Attributes:
        repo: The repository in owner/repo format.
        issue_tracker: IssueTracker port for fetching/searching issues.
        events: EventSink for trace events.
        negative_ttl: How long to remember "searched, not found" results.
    """

    repo: str
    issue_tracker: IssueTracker
    events: EventSink
    negative_ttl: timedelta = DEFAULT_NEGATIVE_TTL

    _cache: dict[str, int] = field(default_factory=dict, init=False)
    _duplicates: dict[str, list[int]] = field(default_factory=dict, init=False)
    _negative_cache: dict[str, datetime] = field(default_factory=dict, init=False)

    # Cumulative counters since process start. Surfaced via _log_stats_summary.
    _stat_memory_hits: int = field(default=0, init=False)
    _stat_negative_hits: int = field(default=0, init=False)
    _stat_search_calls: int = field(default=0, init=False)
    _stat_search_positives: int = field(default=0, init=False)
    _stat_search_negatives: int = field(default=0, init=False)
    _stat_search_errors: int = field(default=0, init=False)
    _stat_build_index_calls: int = field(default=0, init=False)

    def resolve(self, key: IssueKey) -> IssueHandle:
        """Resolve an IssueKey to its GitHub issue number.

        Order: in-memory → negative cache → targeted search. Returns None
        if all three say no.
        """
        external_id = key.stable_id()

        if external_id in self._cache:
            self._stat_memory_hits += 1
            return self._cache[external_id]

        neg_at = self._negative_cache.get(external_id)
        if neg_at is not None:
            if datetime.now(timezone.utc) - neg_at < self.negative_ttl:
                self._stat_negative_hits += 1
                return None
            del self._negative_cache[external_id]

        return self._search_and_cache(external_id)

    def _search_and_cache(self, external_id: str) -> int | None:
        """Targeted /search/issues call for a single external_id."""
        self._stat_search_calls += 1
        start = time.monotonic()
        try:
            with gh_audit.context(
                reason=gh_audit.AuditReason.EXTERNAL_ID_RESOLVE,
                scope=gh_audit.AuditScope.ON_DEMAND,
            ):
                results = self.issue_tracker.search_issues_by_title(
                    [external_id], limit=10
                )
        except Exception as e:
            elapsed_ms = int((time.monotonic() - start) * 1000)
            self._stat_search_errors += 1
            logger.warning(
                "[RESOLVER] search key=%s outcome=error elapsed_ms=%d error=%s",
                external_id, elapsed_ms, e,
            )
            self._maybe_log_summary()
            # Do not negative-cache on transport errors — try again next time.
            return None
        elapsed_ms = int((time.monotonic() - start) * 1000)

        matches = [
            issue.number
            for issue in results
            if parse_external_id(issue.title).external_id == external_id
        ]

        if not matches:
            self._stat_search_negatives += 1
            self._negative_cache[external_id] = datetime.now(timezone.utc)
            logger.info(
                "[RESOLVER] search key=%s outcome=empty elapsed_ms=%d "
                "raw_results=%d",
                external_id, elapsed_ms, len(results),
            )
            self._maybe_log_summary()
            return None

        if len(matches) > 1:
            self._duplicates[external_id] = matches
            logger.warning(
                "[RESOLVER] search key=%s outcome=duplicate matches=%s "
                "elapsed_ms=%d (using first)",
                external_id, matches, elapsed_ms,
            )
            self.events.publish(
                TraceEvent(
                    EventName.RESOLVER_DUPLICATE_EXTERNAL_ID,
                    {"external_id": external_id, "issue_numbers": matches},
                )
            )

        chosen = matches[0]
        self._cache[external_id] = chosen
        self._stat_search_positives += 1
        logger.info(
            "[RESOLVER] search key=%s outcome=found number=%d elapsed_ms=%d",
            external_id, chosen, elapsed_ms,
        )
        self._maybe_log_summary()
        return chosen

    def _maybe_log_summary(self) -> None:
        if self._stat_search_calls % STATS_LOG_INTERVAL != 0:
            return
        logger.info(
            "[RESOLVER] stats search_calls=%d positives=%d negatives=%d "
            "errors=%d memory_hits=%d negative_hits=%d cache_size=%d "
            "negative_cache_size=%d build_index_calls=%d",
            self._stat_search_calls, self._stat_search_positives,
            self._stat_search_negatives, self._stat_search_errors,
            self._stat_memory_hits, self._stat_negative_hits,
            len(self._cache), len(self._negative_cache),
            self._stat_build_index_calls,
        )

    def build_index(self) -> None:
        """Optional seed scan: populates the cache from one REST page of issues.

        Not called automatically. Available for callers that want to amortize
        search calls by pre-warming the cache for the most-recent issues.
        """
        self._stat_build_index_calls += 1
        logger.info("Building issue resolution index for %s", self.repo)

        with gh_audit.context(
            reason=gh_audit.AuditReason.EXTERNAL_ID_RESOLVE,
            scope=gh_audit.AuditScope.ON_DEMAND,
        ):
            issues = self.issue_tracker.list_issues(state="all", limit=100)

        seeded = 0
        for issue in issues:
            parsed = parse_external_id(issue.title)
            if not parsed.external_id:
                continue
            ext_id = parsed.external_id
            if ext_id in self._cache:
                self._duplicates.setdefault(ext_id, [self._cache[ext_id]]).append(
                    issue.number
                )
                continue
            self._cache[ext_id] = issue.number
            # If this key was previously memoized negative, the seed scan
            # found it — drop the negative entry.
            self._negative_cache.pop(ext_id, None)
            seeded += 1

        for ext_id, numbers in self._duplicates.items():
            logger.warning(
                "Duplicate external_id %s found in issues: %s", ext_id, numbers,
            )
            self.events.publish(
                TraceEvent(
                    EventName.RESOLVER_DUPLICATE_EXTERNAL_ID,
                    {"external_id": ext_id, "issue_numbers": numbers},
                )
            )

        logger.info(
            "[RESOLVER] build_index seeded=%d cache_size=%d duplicates=%d",
            seeded, len(self._cache), len(self._duplicates),
        )

    def invalidate(self, key: IssueKey) -> None:
        """Drop both positive and negative cache entries for this key."""
        external_id = key.stable_id()
        self._cache.pop(external_id, None)
        self._negative_cache.pop(external_id, None)
        logger.debug("Invalidated cache for %s", external_id)

    def get_key_for_issue(self, issue: "Issue") -> GitHubIssueKey | None:
        parsed = parse_external_id(issue.title)
        if parsed.external_id:
            return GitHubIssueKey(repo=self.repo, external_id=parsed.external_id)
        return None

    def get_all_keys(self) -> list[GitHubIssueKey]:
        return [
            GitHubIssueKey(repo=self.repo, external_id=ext_id)
            for ext_id in self._cache.keys()
        ]
