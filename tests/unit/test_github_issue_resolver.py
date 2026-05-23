"""Unit tests for GitHubIssueResolver.

Covers the post-#6354 design where resolver lookups on miss fall through to
a targeted /search/issues call (instead of re-paginating). Specifically:

- in-memory cache hit short-circuits search
- search hit populates in-memory cache
- search returning no matches memoizes a negative entry
- negative-cache hit within TTL skips the search call
- negative-cache entry past TTL re-fires the search
- search results that don't parse to the requested external_id are filtered out
- duplicate matches log + emit but still return a number
- transport errors do not negative-cache (caller can retry next tick)
- build_index seeds the cache from one list_issues page and clears negative entries
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from issue_orchestrator.adapters.github.github_issue import GitHubIssue
from issue_orchestrator.adapters.github.issue_resolver import (
    DEFAULT_NEGATIVE_TTL,
    GitHubIssueResolver,
    STATS_LOG_INTERVAL,
)
from issue_orchestrator.domain.issue_key import GitHubIssueKey
from issue_orchestrator.ports.repository_host import RepositoryHostError


REPO = "owner/repo"


def _issue(number: int, title: str) -> GitHubIssue:
    return GitHubIssue(number=number, repo=REPO, title=title)


def _key(external_id: str) -> GitHubIssueKey:
    return GitHubIssueKey(repo=REPO, external_id=external_id)


class FakeTracker:
    """Minimal IssueTracker fake — only the methods the resolver touches."""

    def __init__(self) -> None:
        self.search_calls: list[list[str]] = []
        self.list_calls: int = 0
        self._search_return: list[GitHubIssue] = []
        self._search_raise: Exception | None = None
        self._list_return: list[GitHubIssue] = []

    def set_search_return(self, issues: list[GitHubIssue]) -> None:
        self._search_return = issues
        self._search_raise = None

    def set_search_raise(self, exc: Exception) -> None:
        self._search_raise = exc

    def set_list_return(self, issues: list[GitHubIssue]) -> None:
        self._list_return = issues

    def search_issues_by_title(
        self, query_terms: list[str], *, limit: int = 30
    ) -> list[GitHubIssue]:
        self.search_calls.append(list(query_terms))
        if self._search_raise is not None:
            raise self._search_raise
        return list(self._search_return)

    def list_issues(self, **kwargs) -> list[GitHubIssue]:  # noqa: ARG002
        self.list_calls += 1
        return list(self._list_return)


class FakeEvents:
    def __init__(self) -> None:
        self.published: list = []

    def publish(self, event) -> None:
        self.published.append(event)


@pytest.fixture
def tracker() -> FakeTracker:
    return FakeTracker()


@pytest.fixture
def events() -> FakeEvents:
    return FakeEvents()


@pytest.fixture
def resolver(tracker: FakeTracker, events: FakeEvents) -> GitHubIssueResolver:
    return GitHubIssueResolver(repo=REPO, issue_tracker=tracker, events=events)


# ---------------------------------------------------------------------------
# In-memory cache
# ---------------------------------------------------------------------------

def test_in_memory_hit_does_not_call_search(
    resolver: GitHubIssueResolver, tracker: FakeTracker
) -> None:
    resolver._cache["M1-011"] = 42

    handle = resolver.resolve(_key("M1-011"))

    assert handle == 42
    assert tracker.search_calls == []
    assert resolver._stat_memory_hits == 1
    assert resolver._stat_search_calls == 0


# ---------------------------------------------------------------------------
# Search fallback — positive
# ---------------------------------------------------------------------------

def test_search_hit_populates_cache_and_returns_number(
    resolver: GitHubIssueResolver, tracker: FakeTracker
) -> None:
    tracker.set_search_return([_issue(42, "[M9-006] Foo")])

    handle = resolver.resolve(_key("M9-006"))

    assert handle == 42
    assert tracker.search_calls == [["M9-006"]]
    assert resolver._cache["M9-006"] == 42
    assert resolver._stat_search_positives == 1
    # Second resolve uses memory; no additional search call.
    handle2 = resolver.resolve(_key("M9-006"))
    assert handle2 == 42
    assert len(tracker.search_calls) == 1
    assert resolver._stat_memory_hits == 1


def test_search_results_filtered_by_exact_external_id_match(
    resolver: GitHubIssueResolver, tracker: FakeTracker
) -> None:
    # GitHub's "in:title" is substring — a title like "Notes on M9-006"
    # contains the substring but does NOT parse to external_id M9-006.
    # Only the bracketed-prefix form parses, so the non-prefixed match
    # must be ignored.
    tracker.set_search_return(
        [
            _issue(99, "Notes on M9-006 (deprecated)"),
            _issue(42, "[M9-006] Actual issue"),
        ]
    )

    handle = resolver.resolve(_key("M9-006"))

    assert handle == 42  # The bracketed one, not the substring noise.
    assert resolver._cache["M9-006"] == 42


def test_duplicate_matches_emit_event_and_pick_first(
    resolver: GitHubIssueResolver, tracker: FakeTracker, events: FakeEvents
) -> None:
    tracker.set_search_return(
        [_issue(42, "[M9-006] First"), _issue(99, "[M9-006] Duplicate")]
    )

    handle = resolver.resolve(_key("M9-006"))

    assert handle == 42
    assert resolver._duplicates["M9-006"] == [42, 99]
    assert len(events.published) == 1
    assert events.published[0].name == "resolver.duplicate_external_id"
    assert events.published[0].data["external_id"] == "M9-006"
    assert events.published[0].data["issue_numbers"] == [42, 99]


# ---------------------------------------------------------------------------
# Search fallback — negative + TTL
# ---------------------------------------------------------------------------

def test_search_empty_memoizes_negative_entry(
    resolver: GitHubIssueResolver, tracker: FakeTracker
) -> None:
    tracker.set_search_return([])

    handle = resolver.resolve(_key("M9-999"))

    assert handle is None
    assert "M9-999" in resolver._negative_cache
    assert resolver._stat_search_negatives == 1


def test_negative_cache_hit_skips_search(
    resolver: GitHubIssueResolver, tracker: FakeTracker
) -> None:
    tracker.set_search_return([])

    # First resolve: fires search, memoizes negative.
    resolver.resolve(_key("M9-999"))
    assert len(tracker.search_calls) == 1

    # Second resolve within TTL: must not fire another search.
    resolver.resolve(_key("M9-999"))
    assert len(tracker.search_calls) == 1
    assert resolver._stat_negative_hits == 1


def test_negative_cache_expiry_re_fires_search(
    resolver: GitHubIssueResolver, tracker: FakeTracker
) -> None:
    tracker.set_search_return([])

    resolver.resolve(_key("M9-999"))
    assert len(tracker.search_calls) == 1

    # Backdate the negative entry past the TTL.
    resolver._negative_cache["M9-999"] = datetime.now(timezone.utc) - (
        DEFAULT_NEGATIVE_TTL + timedelta(minutes=1)
    )
    # Make the next search a hit so we can also confirm recovery works.
    tracker.set_search_return([_issue(42, "[M9-999] Eventually-filed issue")])

    handle = resolver.resolve(_key("M9-999"))

    assert handle == 42
    assert len(tracker.search_calls) == 2
    # Expired negative entry should have been removed (not left to linger).
    assert "M9-999" not in resolver._negative_cache


def test_custom_negative_ttl_honored(tracker: FakeTracker, events: FakeEvents) -> None:
    resolver = GitHubIssueResolver(
        repo=REPO,
        issue_tracker=tracker,
        events=events,
        negative_ttl=timedelta(seconds=1),
    )
    tracker.set_search_return([])

    resolver.resolve(_key("M9-999"))
    resolver._negative_cache["M9-999"] = datetime.now(timezone.utc) - timedelta(
        seconds=5
    )

    resolver.resolve(_key("M9-999"))
    # Two fires because TTL was 1s and we backdated 5s.
    assert len(tracker.search_calls) == 2


# ---------------------------------------------------------------------------
# Infrastructure errors — must propagate (B2), not get swallowed into None
# ---------------------------------------------------------------------------

def test_repository_host_error_propagates_and_does_not_negative_cache(
    resolver: GitHubIssueResolver, tracker: FakeTracker
) -> None:
    """RepositoryHostError must propagate so the dep evaluator can distinguish
    "looked, found nothing" (None) from "could not query" (UNKNOWN).
    Swallowing it would silently recreate the dependency_blocked failure
    mode this rewrite was meant to fix.
    """
    tracker.set_search_raise(RepositoryHostError("rate limited"))

    with pytest.raises(RepositoryHostError):
        resolver.resolve(_key("M9-006"))

    assert "M9-006" not in resolver._negative_cache
    assert resolver._stat_search_calls == 1  # we did attempt

    # Recovery on the next call still works once the underlying error clears.
    tracker.set_search_return([_issue(42, "[M9-006] Foo")])
    assert resolver.resolve(_key("M9-006")) == 42


def test_programming_errors_propagate(
    resolver: GitHubIssueResolver, tracker: FakeTracker
) -> None:
    """A bug like a port-method TypeError must crash (fail-fast), not be
    laundered into a misleading 'not found' result.
    """
    tracker.set_search_raise(TypeError("bad call"))

    with pytest.raises(TypeError):
        resolver.resolve(_key("M9-006"))
    assert "M9-006" not in resolver._negative_cache


# ---------------------------------------------------------------------------
# invalidate()
# ---------------------------------------------------------------------------

def test_invalidate_drops_positive_and_negative_entries(
    resolver: GitHubIssueResolver,
) -> None:
    resolver._cache["M9-006"] = 42
    resolver._negative_cache["M9-999"] = datetime.now(timezone.utc)

    resolver.invalidate(_key("M9-006"))
    resolver.invalidate(_key("M9-999"))

    assert "M9-006" not in resolver._cache
    assert "M9-999" not in resolver._negative_cache


# ---------------------------------------------------------------------------
# build_index() (now an optional seed, not called automatically)
# ---------------------------------------------------------------------------

def test_build_index_seeds_cache_and_clears_matching_negatives(
    resolver: GitHubIssueResolver, tracker: FakeTracker
) -> None:
    # Pretend we previously memoized a miss that the seed will now resolve.
    resolver._negative_cache["M9-006"] = datetime.now(timezone.utc)
    tracker.set_list_return(
        [
            _issue(42, "[M9-006] Foo"),
            _issue(43, "[M9-007] Bar"),
            _issue(44, "Unprefixed title"),
        ]
    )

    resolver.build_index()

    assert resolver._cache == {"M9-006": 42, "M9-007": 43}
    assert "M9-006" not in resolver._negative_cache
    assert resolver._stat_build_index_calls == 1
    assert tracker.list_calls == 1


def test_resolver_does_not_auto_build_index_on_miss(
    resolver: GitHubIssueResolver, tracker: FakeTracker
) -> None:
    tracker.set_search_return([_issue(42, "[M9-006] Foo")])

    resolver.resolve(_key("M9-006"))

    # Pre-#6354 behavior re-scanned via list_issues on miss; new design must
    # go straight to search and never touch list_issues.
    assert tracker.list_calls == 0


def test_build_index_replaces_stale_entries(
    resolver: GitHubIssueResolver, tracker: FakeTracker
) -> None:
    """Per the IssueResolver port contract, build_index() *rebuilds* the
    cache — entries that were valid in a prior build but no longer appear
    in the current scan must NOT survive. Otherwise resolve() returns
    stale numbers after renames or removals (B1 finding on PR #6356).
    """
    # First build: M9-006 → 42, M9-007 → 43
    tracker.set_list_return(
        [_issue(42, "[M9-006] Foo"), _issue(43, "[M9-007] Bar")]
    )
    resolver.build_index()
    assert resolver._cache == {"M9-006": 42, "M9-007": 43}

    # Second build: M9-006 has been renamed away (now bare); M9-007 still
    # present but its number changed; M9-008 is new.
    tracker.set_list_return(
        [_issue(43, "Renamed-away — no prefix"),
         _issue(50, "[M9-007] Same title new number"),
         _issue(60, "[M9-008] New entry")]
    )
    resolver.build_index()

    # Stale M9-006 must be gone (not lingering at 42).
    assert "M9-006" not in resolver._cache
    # M9-007 must reflect the new number, not the old 43.
    assert resolver._cache["M9-007"] == 50
    # M9-008 was added.
    assert resolver._cache["M9-008"] == 60


def test_build_index_clears_stale_duplicate_state(
    resolver: GitHubIssueResolver, tracker: FakeTracker, events: FakeEvents
) -> None:
    """Resolved-duplicate keys must not keep emitting duplicate events on
    subsequent builds when the duplicate has been cleaned up.
    """
    # First build: M9-006 appears twice → duplicate.
    tracker.set_list_return(
        [_issue(42, "[M9-006] First"), _issue(43, "[M9-006] Dup")]
    )
    resolver.build_index()
    assert resolver._duplicates.get("M9-006") == [42, 43]
    events.published.clear()

    # Second build: duplicate has been resolved (only one M9-006 now).
    tracker.set_list_return([_issue(42, "[M9-006] First")])
    resolver.build_index()

    assert resolver._duplicates == {}
    # No new duplicate event should fire — the duplicate no longer exists.
    assert events.published == []


def test_build_index_only_clears_negatives_for_found_keys(
    resolver: GitHubIssueResolver, tracker: FakeTracker
) -> None:
    """Negative-cache entries for keys the scan resolved are dropped; entries
    for keys the scan did not encounter must remain (otherwise we'd lose
    quota-saving memoization on every seed call).
    """
    now = datetime.now(timezone.utc)
    resolver._negative_cache["M9-006"] = now  # scan will find this
    resolver._negative_cache["M9-still-missing"] = now  # scan will not

    tracker.set_list_return([_issue(42, "[M9-006] Foo")])
    resolver.build_index()

    assert "M9-006" not in resolver._negative_cache
    assert "M9-still-missing" in resolver._negative_cache


# ---------------------------------------------------------------------------
# Periodic stats log
# ---------------------------------------------------------------------------

def test_periodic_stats_summary_logged_every_N_search_calls(
    resolver: GitHubIssueResolver, tracker: FakeTracker, caplog
) -> None:
    tracker.set_search_return([])
    caplog.set_level("INFO", logger="issue_orchestrator.adapters.github.issue_resolver")

    for i in range(STATS_LOG_INTERVAL):
        resolver.resolve(_key(f"M9-miss-{i}"))

    summaries = [
        rec for rec in caplog.records if "[RESOLVER] stats" in rec.getMessage()
    ]
    assert len(summaries) == 1
    msg = summaries[0].getMessage()
    assert f"search_calls={STATS_LOG_INTERVAL}" in msg
    assert f"negatives={STATS_LOG_INTERVAL}" in msg
