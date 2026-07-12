from __future__ import annotations

import json

import pytest

from issue_orchestrator.execution.json_needs_human_clear_store import (
    CorruptNeedsHumanClearStoreError,
    JsonNeedsHumanClearStore,
)


def test_phase_transitions_survive_reopen(tmp_path) -> None:
    """The owed-clear phases are the durable provenance; they must survive a restart."""
    path = tmp_path / "state" / "needs_human_label_clears.json"
    store = JsonNeedsHumanClearStore(path)
    store.record_pending(903)
    store.confirm(903)
    store.record_pending(904)

    reopened = JsonNeedsHumanClearStore(path)
    assert reopened.confirmed_issue_numbers() == [903]
    assert reopened.pending_issue_numbers() == [904]

    reopened.withdraw(903)
    reloaded = JsonNeedsHumanClearStore(path)
    assert reloaded.confirmed_issue_numbers() == []
    assert reloaded.pending_issue_numbers() == [904]


def test_record_pending_is_idempotent_and_never_downgrades_confirmed(tmp_path) -> None:
    path = tmp_path / "needs_human_label_clears.json"
    store = JsonNeedsHumanClearStore(path)
    store.record_pending(903)
    store.record_pending(903)  # idempotent
    store.confirm(903)
    store.record_pending(903)  # must NOT drop 903 back to pending
    assert store.confirmed_issue_numbers() == [903]
    assert store.pending_issue_numbers() == []


def test_withdraw_absent_is_noop(tmp_path) -> None:
    path = tmp_path / "needs_human_label_clears.json"
    store = JsonNeedsHumanClearStore(path)
    store.record_pending(903)
    store.withdraw(999)  # not present
    assert store.pending_issue_numbers() == [903]


def test_missing_file_loads_empty(tmp_path) -> None:
    store = JsonNeedsHumanClearStore(tmp_path / "absent.json")
    assert store.pending_issue_numbers() == []
    assert store.confirmed_issue_numbers() == []


def test_snapshots_return_copies(tmp_path) -> None:
    """Callers (the per-tick reconciler) mutate the store while iterating; the
    returned snapshots must not alias internal state."""
    store = JsonNeedsHumanClearStore(tmp_path / "needs_human_label_clears.json")
    store.record_pending(903)
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


def test_corrupt_non_object_raises_loudly(tmp_path) -> None:
    path = tmp_path / "needs_human_label_clears.json"
    path.write_text(json.dumps([903]))  # legacy flat list is no longer valid
    with pytest.raises(CorruptNeedsHumanClearStoreError):
        JsonNeedsHumanClearStore(path)


def test_corrupt_non_integer_key_raises_loudly(tmp_path) -> None:
    path = tmp_path / "needs_human_label_clears.json"
    path.write_text(json.dumps({"nine-oh-three": "pending"}))
    with pytest.raises(CorruptNeedsHumanClearStoreError):
        JsonNeedsHumanClearStore(path)


def test_corrupt_unknown_phase_raises_loudly(tmp_path) -> None:
    path = tmp_path / "needs_human_label_clears.json"
    path.write_text(json.dumps({"903": "in-flight"}))
    with pytest.raises(CorruptNeedsHumanClearStoreError):
        JsonNeedsHumanClearStore(path)
