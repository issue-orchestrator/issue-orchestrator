"""The SQLite action liveness store: durable, keyed on the fingerprint (#7350)."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

from issue_orchestrator.domain.action_liveness import (
    ActionIdentity,
    LivenessKey,
    LivenessRow,
    OutcomeKind,
)
from issue_orchestrator.execution.action_liveness_store import SQLiteActionLivenessStore

NOW = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)


def _row(subject="issue:410", action="remove_label", fingerprint="a" * 32,
         issue=410, parked=True, escalated=False) -> LivenessRow:
    return LivenessRow(
        key=LivenessKey(ActionIdentity(subject, action), fingerprint, issue),
        attempts=3,
        first_failed_at=NOW,
        last_failed_at=NOW + timedelta(minutes=5),
        last_outcome=OutcomeKind.TRANSIENT,
        last_reason="3 attempts failed",
        next_attempt_at=None if parked else NOW + timedelta(minutes=8),
        escalated=escalated,
    )


def test_a_row_survives_reopening_the_database(tmp_path) -> None:
    path = tmp_path / "state" / "action_liveness.sqlite"
    row = _row(escalated=True)
    SQLiteActionLivenessStore(path).put(row)

    reopened = SQLiteActionLivenessStore(path)

    assert reopened.row(row.key) == row
    assert reopened.parked_rows() == (row,)


def test_rows_are_keyed_on_the_fingerprint(tmp_path) -> None:
    store = SQLiteActionLivenessStore(tmp_path / "l.sqlite")
    first = _row(fingerprint="a" * 32)
    second = _row(fingerprint="b" * 32, parked=False)
    store.put(first)
    store.put(second)

    assert store.row(first.key) == first
    assert store.row(second.key) == second
    assert store.parked_rows() == (first,)


def test_put_replaces_the_row_for_its_key(tmp_path) -> None:
    store = SQLiteActionLivenessStore(tmp_path / "l.sqlite")
    store.put(_row(parked=False))
    parked = _row(parked=True)
    store.put(parked)

    assert store.row(parked.key) == parked


def test_an_operator_release_deletes_every_fingerprint_and_owes_its_announcement(tmp_path) -> None:
    store = SQLiteActionLivenessStore(tmp_path / "l.sqlite")
    a, b = _row(fingerprint="a" * 32), _row(fingerprint="b" * 32)
    other = _row(action="add_label")
    for row in (a, b, other):
        store.put(row)

    cleared = store.release_identity(a.key.identity)

    assert set(cleared) == {a, b}
    assert store.row(a.key) is None and store.row(b.key) is None
    assert store.row(other.key) == other
    announced = [row.key for _id, row in SQLiteActionLivenessStore(tmp_path / "l.sqlite").pending_announcements()]
    assert sorted(announced, key=lambda k: k.fingerprint) == [a.key, b.key]


def test_escalation_queries_and_release_by_issue(tmp_path) -> None:
    store = SQLiteActionLivenessStore(tmp_path / "l.sqlite")
    escalated = _row(escalated=True)
    silent = _row(action="add_label")
    elsewhere = _row(subject="issue:7", issue=7, escalated=True)
    no_issue = replace(_row(subject="engine", issue=None))
    for row in (escalated, silent, elsewhere, no_issue):
        store.put(row)

    assert store.escalated_rows_for_issue(410) == (escalated,)
    assert set(store.clear_escalation_issue(410)) == {escalated, silent}
    assert set(store.parked_rows()) == {elsewhere, no_issue}
