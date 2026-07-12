from __future__ import annotations

import json

import pytest

from issue_orchestrator.execution.json_needs_human_clear_store import (
    CorruptNeedsHumanClearStoreError,
    JsonNeedsHumanClearStore,
)


def test_record_discard_roundtrip_survives_reopen(tmp_path) -> None:
    """The owed-clear set is the durable provenance; it must survive a restart."""
    path = tmp_path / "state" / "needs_human_label_clears.json"
    store = JsonNeedsHumanClearStore(path)
    store.record(903)
    store.record(904)

    reopened = JsonNeedsHumanClearStore(path)
    assert reopened.pending_issue_numbers() == [903, 904]

    reopened.discard(903)
    assert JsonNeedsHumanClearStore(path).pending_issue_numbers() == [904]


def test_record_is_idempotent_and_order_preserving(tmp_path) -> None:
    path = tmp_path / "needs_human_label_clears.json"
    store = JsonNeedsHumanClearStore(path)
    store.record(903)
    store.record(903)
    store.record(905)
    assert store.pending_issue_numbers() == [903, 905]


def test_discard_absent_is_noop(tmp_path) -> None:
    path = tmp_path / "needs_human_label_clears.json"
    store = JsonNeedsHumanClearStore(path)
    store.record(903)
    store.discard(999)  # not present
    assert store.pending_issue_numbers() == [903]


def test_missing_file_loads_empty(tmp_path) -> None:
    store = JsonNeedsHumanClearStore(tmp_path / "absent.json")
    assert store.pending_issue_numbers() == []


def test_pending_returns_a_copy(tmp_path) -> None:
    """Callers (the per-tick reconciler) mutate the store while iterating; the
    returned snapshot must not alias the internal list."""
    store = JsonNeedsHumanClearStore(tmp_path / "needs_human_label_clears.json")
    store.record(903)
    snapshot = store.pending_issue_numbers()
    snapshot.append(999)
    assert store.pending_issue_numbers() == [903]


def test_corrupt_non_json_raises_loudly(tmp_path) -> None:
    """A present-but-unreadable file must fail loudly, never degrade to empty —
    that would silently abandon a stale needs-human label on a live investigation."""
    path = tmp_path / "needs_human_label_clears.json"
    path.write_text("{not json")
    with pytest.raises(CorruptNeedsHumanClearStoreError):
        JsonNeedsHumanClearStore(path)


def test_corrupt_non_list_raises_loudly(tmp_path) -> None:
    path = tmp_path / "needs_human_label_clears.json"
    path.write_text(json.dumps({"903": True}))
    with pytest.raises(CorruptNeedsHumanClearStoreError):
        JsonNeedsHumanClearStore(path)


def test_corrupt_non_integer_entry_raises_loudly(tmp_path) -> None:
    path = tmp_path / "needs_human_label_clears.json"
    path.write_text(json.dumps([903, "904"]))
    with pytest.raises(CorruptNeedsHumanClearStoreError):
        JsonNeedsHumanClearStore(path)


def test_corrupt_boolean_entry_raises_loudly(tmp_path) -> None:
    """bool is an int subclass; a stray true/false is corruption, not issue 1/0."""
    path = tmp_path / "needs_human_label_clears.json"
    path.write_text(json.dumps([True]))
    with pytest.raises(CorruptNeedsHumanClearStoreError):
        JsonNeedsHumanClearStore(path)
