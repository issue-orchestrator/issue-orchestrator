"""Tests for QueueCacheStore - SQLite-backed queue cache persistence."""

from __future__ import annotations

import pytest
from pathlib import Path

from issue_orchestrator.adapters.github.github_issue import GitHubIssue
from issue_orchestrator.execution.queue_cache_store import QueueCacheStore


@pytest.fixture
def store(tmp_path: Path) -> QueueCacheStore:
    return QueueCacheStore(tmp_path / "queue_cache.sqlite")


def _issue(
    number: int,
    title: str = "Test issue",
    labels: tuple[str, ...] = (),
    repo: str = "owner/repo",
    state: str = "open",
    body: str | None = None,
    milestone: str | None = None,
    milestone_number: int | None = None,
    milestone_due_on: str | None = None,
) -> GitHubIssue:
    return GitHubIssue(
        number=number,
        repo=repo,
        title=title,
        labels=labels,
        state=state,
        body=body,
        milestone=milestone,
        milestone_number=milestone_number,
        milestone_due_on=milestone_due_on,
    )


class TestRoundTrip:
    """Save and load issues preserving all fields."""

    def test_round_trip_all_fields(self, store: QueueCacheStore) -> None:
        issues = [
            _issue(1, title="Bug fix", labels=("bug", "priority:high"), body="Fix it",
                   milestone="v1.0", milestone_number=42, milestone_due_on="2025-12-31"),
            _issue(2, title="Feature", labels=("enhancement",)),
            _issue(3, title="Plain issue"),
        ]
        store.save_snapshot(issues, "2025-06-01T00:00:00Z", repo="owner/repo")

        loaded = store.load_issues("owner/repo")
        assert len(loaded) == 3

        by_num = {i.number: i for i in loaded}

        i1 = by_num[1]
        assert i1.title == "Bug fix"
        assert i1.labels == ("bug", "priority:high")
        assert i1.body == "Fix it"
        assert i1.milestone == "v1.0"
        assert i1.milestone_number == 42
        assert i1.milestone_due_on == "2025-12-31"
        assert i1.state == "open"
        assert i1.repo == "owner/repo"

        i2 = by_num[2]
        assert i2.labels == ("enhancement",)
        assert i2.body is None

        i3 = by_num[3]
        assert i3.labels == ()

    def test_labels_as_tuple(self, store: QueueCacheStore) -> None:
        """Labels must be restored as tuples, not lists."""
        store.save_snapshot([_issue(1, labels=("a", "b"))], None, repo="owner/repo")
        loaded = store.load_issues("owner/repo")
        assert isinstance(loaded[0].labels, tuple)

    def test_repo_param_applied_on_load(self, store: QueueCacheStore) -> None:
        """The repo argument to load_issues sets .repo on every loaded issue."""
        store.save_snapshot([_issue(1)], None, repo="owner/repo")
        loaded = store.load_issues("different/repo")
        assert loaded[0].repo == "different/repo"


class TestWatermark:
    """Watermark persistence."""

    def test_watermark_round_trip(self, store: QueueCacheStore) -> None:
        store.save_snapshot([], "2025-06-01T12:00:00Z")
        assert store.load_watermark() == "2025-06-01T12:00:00Z"

    def test_watermark_none_on_empty(self, store: QueueCacheStore) -> None:
        assert store.load_watermark() is None

    def test_watermark_updated_on_second_save(self, store: QueueCacheStore) -> None:
        store.save_snapshot([], "2025-01-01T00:00:00Z")
        store.save_snapshot([], "2025-06-01T00:00:00Z")
        assert store.load_watermark() == "2025-06-01T00:00:00Z"

    def test_watermark_none_not_written(self, store: QueueCacheStore) -> None:
        """When watermark is None, don't overwrite an existing watermark."""
        store.save_snapshot([], "2025-01-01T00:00:00Z")
        store.save_snapshot([], None)
        # No upsert for None, so original watermark stays
        assert store.load_watermark() == "2025-01-01T00:00:00Z"


class TestLastHealthReviewAt:
    """Durable last-health-review marker (ADR-0031 §4)."""

    def test_round_trip(self, store: QueueCacheStore) -> None:
        store.save_last_health_review_at(1750000000.25)
        assert store.load_last_health_review_at() == 1750000000.25

    def test_defaults_to_zero_on_empty_store(self, store: QueueCacheStore) -> None:
        assert store.load_last_health_review_at() == 0.0

    def test_second_save_overwrites(self, store: QueueCacheStore) -> None:
        store.save_last_health_review_at(100.0)
        store.save_last_health_review_at(200.0)
        assert store.load_last_health_review_at() == 200.0

    def test_survives_snapshot_save(self, store: QueueCacheStore) -> None:
        """save_snapshot (queue + watermark) must not clobber the marker."""
        store.save_last_health_review_at(100.0)
        store.save_snapshot([_issue(1)], "2025-06-01T00:00:00Z", repo="owner/repo")
        assert store.load_last_health_review_at() == 100.0
        assert store.load_watermark() == "2025-06-01T00:00:00Z"

    def test_independent_of_watermark(self, store: QueueCacheStore) -> None:
        store.save_last_health_review_at(100.0)
        assert store.load_watermark() is None

    def test_cleared_with_store(self, store: QueueCacheStore) -> None:
        store.save_last_health_review_at(100.0)
        store.clear()
        assert store.load_last_health_review_at() == 0.0


class TestLastReviewedBoardFingerprint:
    """Durable reviewed-board fingerprint (ADR-0031 §4 activity gate)."""

    def test_round_trip(self, store: QueueCacheStore) -> None:
        store.save_last_reviewed_board_fingerprint("abc123")
        assert store.load_last_reviewed_board_fingerprint() == "abc123"

    def test_defaults_to_empty_on_empty_store(self, store: QueueCacheStore) -> None:
        assert store.load_last_reviewed_board_fingerprint() == ""

    def test_second_save_overwrites(self, store: QueueCacheStore) -> None:
        store.save_last_reviewed_board_fingerprint("first")
        store.save_last_reviewed_board_fingerprint("second")
        assert store.load_last_reviewed_board_fingerprint() == "second"

    def test_independent_of_last_health_review_at(
        self, store: QueueCacheStore
    ) -> None:
        store.save_last_reviewed_board_fingerprint("fp")
        store.save_last_health_review_at(100.0)
        assert store.load_last_reviewed_board_fingerprint() == "fp"
        assert store.load_last_health_review_at() == 100.0

    def test_survives_snapshot_save(self, store: QueueCacheStore) -> None:
        store.save_last_reviewed_board_fingerprint("fp")
        store.save_snapshot([_issue(1)], "2025-06-01T00:00:00Z", repo="owner/repo")
        assert store.load_last_reviewed_board_fingerprint() == "fp"

    def test_cleared_with_store(self, store: QueueCacheStore) -> None:
        store.save_last_reviewed_board_fingerprint("fp")
        store.clear()
        assert store.load_last_reviewed_board_fingerprint() == ""


class TestStuckSweepState:
    """Durable tech-lead stuck-sweep timer + recovery counters (#6823)."""

    def test_last_stuck_sweep_at_round_trip(self, store: QueueCacheStore) -> None:
        store.save_last_stuck_sweep_at(1750000000.5)
        assert store.load_last_stuck_sweep_at() == 1750000000.5

    def test_last_stuck_sweep_at_defaults_to_zero(
        self, store: QueueCacheStore
    ) -> None:
        assert store.load_last_stuck_sweep_at() == 0.0

    def test_recovery_attempts_round_trip_with_int_keys(
        self, store: QueueCacheStore
    ) -> None:
        store.save_recovery_attempts({7: 1, 42: 3})
        loaded = store.load_recovery_attempts()
        assert loaded == {7: 1, 42: 3}
        # JSON keys serialize as strings; they must round-trip back to ints.
        assert all(isinstance(key, int) for key in loaded)

    def test_recovery_attempts_defaults_to_empty(
        self, store: QueueCacheStore
    ) -> None:
        assert store.load_recovery_attempts() == {}

    def test_recovery_attempts_second_save_overwrites(
        self, store: QueueCacheStore
    ) -> None:
        store.save_recovery_attempts({1: 1})
        store.save_recovery_attempts({1: 2, 2: 1})
        assert store.load_recovery_attempts() == {1: 2, 2: 1}

    def test_stuck_sweep_state_independent_of_health_review(
        self, store: QueueCacheStore
    ) -> None:
        store.save_last_stuck_sweep_at(500.0)
        store.save_recovery_attempts({9: 2})
        store.save_last_health_review_at(100.0)
        assert store.load_last_stuck_sweep_at() == 500.0
        assert store.load_recovery_attempts() == {9: 2}
        assert store.load_last_health_review_at() == 100.0

    def test_stuck_sweep_state_survives_snapshot_save(
        self, store: QueueCacheStore
    ) -> None:
        store.save_last_stuck_sweep_at(500.0)
        store.save_recovery_attempts({9: 2})
        store.save_snapshot([_issue(1)], "2025-06-01T00:00:00Z", repo="owner/repo")
        assert store.load_last_stuck_sweep_at() == 500.0
        assert store.load_recovery_attempts() == {9: 2}

    def test_stuck_sweep_state_cleared_with_store(
        self, store: QueueCacheStore
    ) -> None:
        store.save_last_stuck_sweep_at(500.0)
        store.save_recovery_attempts({9: 2})
        store.clear()
        assert store.load_last_stuck_sweep_at() == 0.0
        assert store.load_recovery_attempts() == {}


class TestReplaceSemantics:
    """save_snapshot replaces all issues."""

    def test_replace_issues(self, store: QueueCacheStore) -> None:
        store.save_snapshot(
            [_issue(1), _issue(2), _issue(3)], "w1", repo="owner/repo",
        )
        store.save_snapshot(
            [_issue(10), _issue(20)], "w2", repo="owner/repo",
        )
        loaded = store.load_issues("owner/repo")
        nums = {i.number for i in loaded}
        assert nums == {10, 20}

    def test_replace_with_empty(self, store: QueueCacheStore) -> None:
        store.save_snapshot([_issue(1)], "w1", repo="owner/repo")
        store.save_snapshot([], "w2")
        assert store.load_issues("owner/repo") == []


class TestEmptyStore:
    """Behavior when store is empty."""

    def test_load_issues_empty(self, store: QueueCacheStore) -> None:
        assert store.load_issues("owner/repo") == []

    def test_load_watermark_empty(self, store: QueueCacheStore) -> None:
        assert store.load_watermark() is None


class TestClear:
    """clear() removes all data."""

    def test_clear_removes_issues_and_watermark(self, store: QueueCacheStore) -> None:
        store.save_snapshot(
            [_issue(1), _issue(2)], "2025-06-01T00:00:00Z", repo="owner/repo",
        )
        store.clear()
        assert store.load_issues("owner/repo") == []
        assert store.load_watermark() is None


class TestWipeLogging:
    """Wipes of a non-empty persisted queue leave a trail for diagnosis."""

    def test_save_snapshot_logs_warning_when_wiping_non_empty(
        self, store: QueueCacheStore, caplog: pytest.LogCaptureFixture,
    ) -> None:
        store.save_snapshot([_issue(1), _issue(2)], "w1", repo="r")
        caplog.clear()
        with caplog.at_level("WARNING", logger="issue_orchestrator.execution.queue_cache_store"):
            store.save_snapshot([], "w2", repo="r")
        assert any("wiping persisted queue" in r.message for r in caplog.records), caplog.text
        assert any("prior=2" in r.message for r in caplog.records), caplog.text

    def test_save_snapshot_silent_when_empty_to_empty(
        self, store: QueueCacheStore, caplog: pytest.LogCaptureFixture,
    ) -> None:
        with caplog.at_level("WARNING", logger="issue_orchestrator.execution.queue_cache_store"):
            store.save_snapshot([], None, repo="r")
        assert not any("wiping persisted queue" in r.message for r in caplog.records)

    def test_save_snapshot_silent_when_writing_issues(
        self, store: QueueCacheStore, caplog: pytest.LogCaptureFixture,
    ) -> None:
        store.save_snapshot([_issue(1)], "w1", repo="r")
        caplog.clear()
        with caplog.at_level("WARNING", logger="issue_orchestrator.execution.queue_cache_store"):
            store.save_snapshot([_issue(2)], "w2", repo="r")
        assert not any("wiping persisted queue" in r.message for r in caplog.records)

    def test_clear_logs_warning_when_wiping_non_empty(
        self, store: QueueCacheStore, caplog: pytest.LogCaptureFixture,
    ) -> None:
        store.save_snapshot([_issue(1), _issue(2), _issue(3)], "w1", repo="r")
        caplog.clear()
        with caplog.at_level("WARNING", logger="issue_orchestrator.execution.queue_cache_store"):
            store.clear()
        assert any("clear() wiping 3 persisted" in r.message for r in caplog.records), caplog.text

    def test_clear_silent_when_already_empty(
        self, store: QueueCacheStore, caplog: pytest.LogCaptureFixture,
    ) -> None:
        with caplog.at_level("WARNING", logger="issue_orchestrator.execution.queue_cache_store"):
            store.clear()
        assert not any("clear() wiping" in r.message for r in caplog.records)


class TestLabelSerialization:
    """Edge cases for label JSON serialization."""

    def test_special_characters(self, store: QueueCacheStore) -> None:
        labels = ("bug:\"quoted\"", "has spaces", "emoji-🎯", "slash/label")
        store.save_snapshot([_issue(1, labels=labels)], None, repo="r")
        loaded = store.load_issues("r")
        assert loaded[0].labels == labels

    def test_empty_labels(self, store: QueueCacheStore) -> None:
        store.save_snapshot([_issue(1, labels=())], None, repo="r")
        loaded = store.load_issues("r")
        assert loaded[0].labels == ()

    def test_many_labels(self, store: QueueCacheStore) -> None:
        labels = tuple(f"label-{i}" for i in range(50))
        store.save_snapshot([_issue(1, labels=labels)], None, repo="r")
        loaded = store.load_issues("r")
        assert loaded[0].labels == labels
