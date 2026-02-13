"""Tests for LabelStore — SQLite persistence for orchestrator-applied labels."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from issue_orchestrator.execution.label_store import LabelStore


@pytest.fixture
def store(tmp_path: Path) -> LabelStore:
    return LabelStore(tmp_path / "label_store.sqlite")


class TestAddAndLoad:
    def test_add_and_load(self, store: LabelStore) -> None:
        store.add_label(42, "in-progress")
        store.add_label(42, "blocked-failed")
        assert store.load_labels(42) == {"in-progress", "blocked-failed"}

    def test_upsert(self, store: LabelStore) -> None:
        store.add_label(1, "in-progress")
        store.add_label(1, "in-progress")  # duplicate, should upsert
        assert store.load_labels(1) == {"in-progress"}

    def test_load_empty(self, store: LabelStore) -> None:
        assert store.load_labels(999) == set()


class TestRemoveLabel:
    def test_remove_existing(self, store: LabelStore) -> None:
        store.add_label(1, "a")
        store.add_label(1, "b")
        store.remove_label(1, "a")
        assert store.load_labels(1) == {"b"}

    def test_remove_nonexistent(self, store: LabelStore) -> None:
        store.remove_label(1, "nope")  # should not error


class TestSaveLabels:
    def test_replace(self, store: LabelStore) -> None:
        store.add_label(1, "old")
        store.save_labels(1, {"new-a", "new-b"})
        assert store.load_labels(1) == {"new-a", "new-b"}

    def test_empty_replace(self, store: LabelStore) -> None:
        store.add_label(1, "old")
        store.save_labels(1, set())
        assert store.load_labels(1) == set()


class TestLoadAll:
    def test_multiple_issues(self, store: LabelStore) -> None:
        store.add_label(1, "a")
        store.add_label(1, "b")
        store.add_label(2, "c")
        result = store.load_all()
        assert result == {1: {"a", "b"}, 2: {"c"}}

    def test_empty(self, store: LabelStore) -> None:
        assert store.load_all() == {}


class TestRemoveIssue:
    def test_removes_all_labels(self, store: LabelStore) -> None:
        store.add_label(1, "a")
        store.add_label(1, "b")
        store.add_label(2, "c")
        store.remove_issue(1)
        assert store.load_labels(1) == set()
        assert store.load_labels(2) == {"c"}


class TestClear:
    def test_clears_everything(self, store: LabelStore) -> None:
        store.add_label(1, "a")
        store.add_label(2, "b")
        store.clear()
        assert store.load_all() == {}


class TestThreadSafety:
    def test_concurrent_writes(self, store: LabelStore) -> None:
        """Verify multiple threads can write without corruption."""
        errors: list[Exception] = []

        def worker(issue: int) -> None:
            try:
                for i in range(20):
                    store.add_label(issue, f"label-{i}")
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        for n in range(5):
            assert len(store.load_labels(n)) == 20


class TestWALMode:
    def test_wal_mode(self, store: LabelStore) -> None:
        """Verify WAL journal mode is enabled."""
        conn = store._get_connection()
        mode = conn.execute("PRAGMA journal_mode").fetchone()
        assert mode[0] == "wal"
