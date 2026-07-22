"""Tests for LabelStoreReconciler — keeping the label_store mirror honest."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

import pytest

from issue_orchestrator.control.label_manager import LabelManager
from issue_orchestrator.control.label_store_reconciler import (
    FreshLabelSnapshot,
    LabelStoreReconciler,
)
from issue_orchestrator.execution.label_store import LabelStore
from issue_orchestrator.infra.config import Config


@dataclass
class _FakeIssue:
    number: int
    labels: Sequence[str]


class _FakeRepositoryHost:
    """Records get_issue calls and serves canned issues."""

    def __init__(self, issues: dict[int, _FakeIssue | None]) -> None:
        self._issues = issues
        self.fetched: list[int] = []

    def get_issue(self, issue_number: int):
        self.fetched.append(issue_number)
        return self._issues.get(issue_number)


@pytest.fixture
def label_manager() -> LabelManager:
    return LabelManager(Config(repo="o/r"))


@pytest.fixture
def store(tmp_path: Path) -> LabelStore:
    return LabelStore(tmp_path / "label_store.sqlite")


def _reconciler(store, label_manager, repo, budget=100):
    return LabelStoreReconciler(
        label_store=store,
        label_manager=label_manager,
        repository_host=repo,
        fetch_budget=budget,
    )


class TestReconcileFromCache:
    def test_prunes_stale_rows_using_cache(self, store, label_manager):
        # Store believes 228 has publish-failed/pr-pending, but GitHub (cache)
        # only has agent:backend — the orchestrator labels are stale.
        store.add_label(228, "publish-failed")
        store.add_label(228, "pr-pending")
        repo = _FakeRepositoryHost({})

        result = _reconciler(store, label_manager, repo).reconcile(
            FreshLabelSnapshot.from_github_sync({228: ["agent:backend"]})
        )

        assert store.load_labels(228) == set()
        assert result.labels_removed == 2
        assert result.issues_changed == 1
        # Zero-cost: a cache hit must not fetch.
        assert repo.fetched == []

    def test_adds_missing_rows_using_cache(self, store, label_manager):
        # GitHub has blocked + tech_lead provenance + pr-pending; the store
        # under-records them. Reconcile fills the gap so the mirror matches.
        store.add_label(228, "pr-pending")
        repo = _FakeRepositoryHost({})

        result = _reconciler(store, label_manager, repo).reconcile(
            FreshLabelSnapshot.from_github_sync(
                {
                    228: [
                        "blocked",
                        "tech-lead-needs-human",
                        "pr-pending",
                        "agent:web",
                    ]
                }
            )
        )

        assert store.load_labels(228) == {
            "pr-pending",
            "blocked",
            "tech-lead-needs-human",
        }
        assert result.labels_added == 2
        assert result.labels_removed == 0

    def test_matching_rows_unchanged(self, store, label_manager):
        store.add_label(228, "pr-pending")
        repo = _FakeRepositoryHost({})

        result = _reconciler(store, label_manager, repo).reconcile(
            FreshLabelSnapshot.from_github_sync({228: ["pr-pending", "agent:web"]})
        )

        assert store.load_labels(228) == {"pr-pending"}
        assert result.issues_changed == 0

    def test_non_orchestrator_store_rows_are_left_alone(self, store, label_manager):
        # A store row that isn't orchestrator-owned is never reconciled away.
        store.add_label(228, "agent:backend")
        repo = _FakeRepositoryHost({})

        result = _reconciler(store, label_manager, repo).reconcile(
            FreshLabelSnapshot.from_github_sync({228: []})
        )

        assert store.load_labels(228) == {"agent:backend"}
        assert result.issues_changed == 0
        assert repo.fetched == []


class TestReconcileWithFetch:
    def test_fetches_out_of_cache_issues(self, store, label_manager):
        # Closed/out-of-scope issue absent from the warm cache: a single fetch
        # confirms GitHub no longer carries the orchestrator labels.
        store.add_label(274, "publish-fail-count-1")
        store.add_label(274, "pr-pending")
        repo = _FakeRepositoryHost({274: _FakeIssue(274, ["bug"])})

        result = _reconciler(store, label_manager, repo).reconcile(
            FreshLabelSnapshot.from_github_sync({})
        )

        assert store.load_labels(274) == set()
        assert repo.fetched == [274]
        assert result.issues_fetched == 1
        assert result.labels_removed == 2

    def test_fetch_budget_is_respected(self, store, label_manager):
        for n in (1, 2, 3):
            store.add_label(n, "pr-pending")
        repo = _FakeRepositoryHost({
            1: _FakeIssue(1, []),
            2: _FakeIssue(2, []),
            3: _FakeIssue(3, []),
        })

        result = _reconciler(store, label_manager, repo, budget=2).reconcile(
            FreshLabelSnapshot.from_github_sync({})
        )

        assert result.issues_fetched == 2
        assert result.issues_skipped_budget == 1
        # The two fetched issues were pruned; the third is deferred.
        pruned = sum(1 for n in (1, 2, 3) if store.load_labels(n) == set())
        assert pruned == 2

    def test_degraded_snapshot_forces_fresh_reads(self, store, label_manager):
        # A degraded snapshot carries no trusted labels, so a stored issue is
        # read fresh instead of reconciled against cache. (Contrast with the
        # from_github_sync cache-hit tests, which never fetch.)
        store.add_label(228, "pr-pending")
        repo = _FakeRepositoryHost({228: _FakeIssue(228, ["agent:backend"])})

        result = _reconciler(store, label_manager, repo).reconcile(
            FreshLabelSnapshot.degraded()
        )

        assert store.load_labels(228) == set()
        assert repo.fetched == [228]
        assert result.issues_fetched == 1

    def test_missing_issue_leaves_store_untouched(self, store, label_manager):
        # get_issue returns None (transient/unreadable): don't guess.
        store.add_label(99, "publish-failed")
        repo = _FakeRepositoryHost({99: None})

        result = _reconciler(store, label_manager, repo).reconcile(
            FreshLabelSnapshot.from_github_sync({})
        )

        assert store.load_labels(99) == {"publish-failed"}
        assert result.issues_changed == 0
        assert repo.fetched == [99]
