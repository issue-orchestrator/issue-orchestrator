"""Trigger-side lifecycle for the periodic health review (ADR-0031 §4, #6763).

These tests drive the real ``health_review_trigger`` functions through fake
ports (a durable store and a repository host), never internal helpers, and
cover the two reliability findings that the batch/planner tests cannot reach:

* **finding 6** — a lost persist of ``last_health_review_at`` must NOT re-fire
  the review after the anchor closes; a restart reconciles the interval from
  the anchor issue's own creation time, the crash-safe truth.
* **finding 7** — one scoped, exhaustive anchor-discovery owner backs both
  fact gathering and startup recovery, so neither strands an older anchor nor
  crosses the ``filtering.label`` boundary.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from issue_orchestrator.control.actions import CreateTriageIssueAction
from issue_orchestrator.control.health_review_trigger import (
    HEALTH_REVIEW_ISSUE_TITLE,
    discover_open_health_review_anchor,
    discover_open_triage_anchor_issues,
    health_review_due,
    hydrate_last_health_review_at,
    most_recent_health_anchor_created_at,
    record_health_review_creation,
)
from issue_orchestrator.control.triage_issue_policy import (
    health_review_issue_labels,
)
from issue_orchestrator.domain.models import Issue, OrchestratorState
from issue_orchestrator.domain.triage_session import HEALTH_REVIEW_MARKER_LABEL
from issue_orchestrator.infra.config import Config


def _config(interval_minutes: int = 60, *, filter_label: str | None = None) -> Config:
    config = Config()
    config.triage_review_agent = "agent:triage"
    config.triage.health_review.interval_minutes = interval_minutes
    config.filtering.label = filter_label
    return config


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


class _FakeStore:
    """QueueCacheStore fake for the durable last-fired timestamp."""

    def __init__(self, stored: float = 0.0, *, raise_on_save: bool = False) -> None:
        self._stored = stored
        self.raise_on_save = raise_on_save
        self.saved: list[float] = []

    def load_last_health_review_at(self) -> float:
        return self._stored

    def save_last_health_review_at(self, value: float) -> None:
        if self.raise_on_save:
            raise OSError("disk full")
        self._stored = value
        self.saved.append(value)


class _AnchorTracker:
    """RepositoryHost fake honoring GitHub's label AND-filter, state, and limit."""

    def __init__(self, issues) -> None:
        self._issues = list(issues)
        self.calls: list[dict] = []

    def list_issues(self, labels=None, state="open", limit=100, **kwargs):
        self.calls.append(
            {"labels": list(labels or []), "state": state, "limit": limit}
        )
        wanted = {label.casefold() for label in (labels or [])}
        matched = [
            issue
            for issue in self._issues
            if state == "all" or issue.state == state
            if wanted <= {label.casefold() for label in issue.labels}
        ]
        return matched[:limit]


def _anchor(number: int, epoch: float, *, state: str = "closed", config: Config):
    return Issue(
        number=number,
        title=HEALTH_REVIEW_ISSUE_TITLE,
        labels=list(health_review_issue_labels(config)),
        state=state,
        created_at=_iso(epoch),
    )


class TestPersistFailureReconciliation:
    """Finding 6: a lost persist can never re-fire the review early."""

    def test_record_swallows_persist_failure_but_stamps_memory(self) -> None:
        """The created issue must not be reported as an apply failure, yet the
        in-memory stamp still guards the current process."""
        config = _config()
        store = _FakeStore(raise_on_save=True)
        state = OrchestratorState()
        action = CreateTriageIssueAction(
            title=HEALTH_REVIEW_ISSUE_TITLE,
            labels=health_review_issue_labels(config),
        )

        # Must not raise even though the store persist fails.
        record_health_review_creation(action, state, store, now=1_000_000.0)

        assert state.last_health_review_at == 1_000_000.0
        assert store.load_last_health_review_at() == 0.0  # persist was lost

    def test_restart_after_lost_persist_does_not_refire_before_interval(self) -> None:
        """The full failure path: fire -> persist fails -> anchor closes ->
        restart hydrates 0.0 from the store -> reconcile from the anchor's
        created_at, so the review is NOT due again until the interval elapses.
        """
        config = _config(interval_minutes=60)
        store = _FakeStore(stored=0.0, raise_on_save=True)
        fired_at = 1_000_000.0
        action = CreateTriageIssueAction(
            title=HEALTH_REVIEW_ISSUE_TITLE,
            labels=health_review_issue_labels(config),
        )

        live = OrchestratorState()
        record_health_review_creation(action, live, store, now=fired_at)
        assert store.load_last_health_review_at() == 0.0  # nothing durable landed

        # Contrast: plain store hydration (0.0) WOULD re-fire immediately.
        naive = OrchestratorState()
        naive.last_health_review_at = store.load_last_health_review_at()
        assert health_review_due(config, naive, now=fired_at + 1800) is True

        # Reconciled restart: the closed anchor carries the true fire time.
        tracker = _AnchorTracker([_anchor(200, fired_at, config=config)])
        restarted = OrchestratorState()
        hydrate_last_health_review_at(config, restarted, store, tracker)

        assert restarted.last_health_review_at == pytest.approx(fired_at)
        assert health_review_due(config, restarted, now=fired_at + 1800) is False
        assert health_review_due(config, restarted, now=fired_at + 3700) is True

    def test_reconciliation_self_heals_the_store_when_persist_recovers(self) -> None:
        """Once the store can write again, the reconciled value is persisted so
        the next restart takes the fast path."""
        config = _config(interval_minutes=60)
        fired_at = 2_000_000.0
        store = _FakeStore(stored=0.0)  # save now succeeds
        tracker = _AnchorTracker([_anchor(200, fired_at, config=config)])

        state = OrchestratorState()
        hydrate_last_health_review_at(config, state, store, tracker)

        assert state.last_health_review_at == pytest.approx(fired_at)
        assert store.load_last_health_review_at() == pytest.approx(fired_at)

    def test_store_ahead_of_anchor_is_not_downgraded(self) -> None:
        """The store is the fast path; reconciliation only pulls FORWARD, never
        back to an older anchor."""
        config = _config(interval_minutes=60)
        newer = 5_000_000.0
        older = 4_000_000.0
        store = _FakeStore(stored=newer)
        tracker = _AnchorTracker([_anchor(200, older, config=config)])

        state = OrchestratorState()
        hydrate_last_health_review_at(config, state, store, tracker)

        assert state.last_health_review_at == pytest.approx(newer)

    def test_no_anchor_leaves_store_value_untouched(self) -> None:
        config = _config(interval_minutes=60)
        store = _FakeStore(stored=1234.0)
        tracker = _AnchorTracker([])

        state = OrchestratorState()
        hydrate_last_health_review_at(config, state, store, tracker)

        assert state.last_health_review_at == pytest.approx(1234.0)
        assert most_recent_health_anchor_created_at(tracker, config) == 0.0

    def test_disabled_interval_hydrates_store_without_a_github_call(self) -> None:
        """A disabled trigger reconciles nothing and pays no anchor scan."""
        config = _config(interval_minutes=0)
        store = _FakeStore(stored=42.0)
        tracker = _AnchorTracker([_anchor(200, 9_000_000.0, config=config)])

        state = OrchestratorState()
        hydrate_last_health_review_at(config, state, store, tracker)

        assert state.last_health_review_at == pytest.approx(42.0)
        assert tracker.calls == []  # no GitHub scan when disabled


class TestSharedAnchorDiscovery:
    """Finding 7: one scoped, exhaustive discovery owner for both paths."""

    def test_discovery_ignores_anchors_outside_the_filter_scope(self) -> None:
        config = _config(filter_label="io:e2e:run-1")
        in_scope = Issue(
            number=1,
            title="Batch",
            labels=["agent:triage", "io:e2e:run-1"],
            state="open",
        )
        out_of_scope = Issue(
            number=2,
            title="Health Review — walk the floor",
            labels=["agent:triage", HEALTH_REVIEW_MARKER_LABEL],
            state="open",
        )
        tracker = _AnchorTracker([in_scope, out_of_scope])

        found = discover_open_triage_anchor_issues(tracker, config)

        assert [issue.number for issue in found] == [1]

    def test_discovery_is_exhaustive_past_a_twenty_item_page(self) -> None:
        """More than twenty candidate anchors are ALL discovered — no fixed
        first page strands an older anchor (the pre-fix startup limit=20 bug)."""
        config = _config()
        crowd = [
            Issue(number=n, title=f"Batch {n}", labels=["agent:triage"], state="open")
            for n in range(1, 26)
        ]
        tracker = _AnchorTracker(crowd)

        found = discover_open_triage_anchor_issues(tracker, config)

        assert len(found) == 25
        assert all(call["limit"] >= 25 for call in tracker.calls)

    def test_marker_scoped_lookup_finds_anchor_beyond_the_broad_page(self) -> None:
        config = _config()
        crowd = [
            Issue(number=n, title=f"Batch {n}", labels=["agent:triage"], state="open")
            for n in range(1, 15)
        ]
        anchor = Issue(
            number=200,
            title=HEALTH_REVIEW_ISSUE_TITLE,
            labels=["agent:triage", HEALTH_REVIEW_MARKER_LABEL],
            state="open",
        )
        tracker = _AnchorTracker([*crowd, anchor])

        assert discover_open_health_review_anchor(tracker, config) == 200
        assert any(
            HEALTH_REVIEW_MARKER_LABEL in call["labels"] for call in tracker.calls
        )
