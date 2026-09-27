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
    announced = [
        row.key
        for _id, _kind, row in SQLiteActionLivenessStore(tmp_path / "l.sqlite").pending_announcements()
    ]
    assert sorted(announced, key=lambda k: k.fingerprint) == [a.key, b.key]


def test_escalation_queries_and_release_by_issue(tmp_path) -> None:
    store = SQLiteActionLivenessStore(tmp_path / "l.sqlite")
    escalated = _row(escalated=True)
    silent = _row(action="add_label")
    elsewhere = _row(subject="issue:7", issue=7, escalated=True)
    no_issue = replace(_row(subject="engine", issue=None))
    for row in (escalated, silent, elsewhere, no_issue):
        store.put(row)

    assert set(store.parked_rows_for_issue(410)) == {escalated, silent}
    assert set(store.clear_escalation_issue(410)) == {escalated, silent}
    assert set(store.parked_rows()) == {elsewhere, no_issue}


def test_retirement_supersedes_only_after_progress_and_abandons_only_after_long(tmp_path) -> None:
    store = SQLiteActionLivenessStore(tmp_path / "l.sqlite")
    asked_long_ago = _row(fingerprint="a" * 32)
    other_operation = _row(action="add_label", fingerprint="c" * 32)
    store.put(asked_long_ago)
    store.put(other_operation)
    later = NOW + timedelta(hours=3)

    # No progress on the identity: an hours-old question is kept.
    assert store.retire_unplanned(
        abandoned_before=NOW - timedelta(days=7), superseded_before=later
    ) == ()

    # Its operation then succeeds under other facts: the old row is superseded,
    # the unrelated operation is not.
    store.clear_key(
        LivenessKey(asked_long_ago.key.identity, "b" * 32, 410), done_at=later
    )
    retired = store.retire_unplanned(
        abandoned_before=NOW - timedelta(days=7), superseded_before=later
    )
    assert retired == (asked_long_ago,)
    assert store.row(other_operation.key) == other_operation

    # Nobody asks for long enough: abandoned regardless of progress.
    assert store.retire_unplanned(
        abandoned_before=later, superseded_before=NOW - timedelta(days=7)
    ) == (other_operation,)


def test_success_notes_progress_and_forgets_the_park_in_one_transaction(
    tmp_path, monkeypatch
) -> None:
    """A crash inside a success leaves both or neither (review r12)."""
    path = tmp_path / "l.sqlite"
    store = SQLiteActionLivenessStore(path)
    parked = _row()
    store.put(parked)

    class _Crash(Exception):
        pass

    class _CrashOnDelete:
        """The engine's connection, dying between noting progress and deleting."""

        def __init__(self, conn) -> None:
            self._conn = conn

        def execute(self, sql, *args):
            if sql.startswith("DELETE FROM action_liveness WHERE"):
                raise _Crash()
            return self._conn.execute(sql, *args)

        def commit(self):
            self._conn.commit()

        def __enter__(self):
            self._conn.__enter__()
            return self

        def __exit__(self, *exc):
            return self._conn.__exit__(*exc)

    crashing = SQLiteActionLivenessStore(path)
    connection = _CrashOnDelete(crashing._connection())
    monkeypatch.setattr(crashing, "_connection", lambda: connection)
    try:
        crashing.clear_key(parked.key, done_at=NOW + timedelta(hours=3))
    except _Crash:
        pass

    reopened = SQLiteActionLivenessStore(path)
    assert reopened.row(parked.key) == parked
    assert reopened.retire_unplanned(
        abandoned_before=NOW - timedelta(days=7), superseded_before=NOW + timedelta(days=1)
    ) == (), "no progress was noted, so the park is not superseded"


def test_every_park_and_unpark_owes_an_announcement(tmp_path) -> None:
    """Announcements are owed in the transactions that park and unpark, so a
    crash before publishing delays them but never loses them (review r14)."""
    from issue_orchestrator.domain.action_liveness import LivenessAnnouncement

    path = tmp_path / "l.sqlite"
    store = SQLiteActionLivenessStore(path)
    parked = _row()
    store.settle(None, parked, announce_parked=True)
    other = _row(subject="issue:7", issue=7)
    store.settle(None, other, announce_parked=True)
    store.clear_key(parked.key, done_at=NOW + timedelta(hours=1))
    store.clear_escalation_issue(7)

    owed = [(kind, row.key) for _id, kind, row in SQLiteActionLivenessStore(path).pending_announcements()]
    assert owed == [
        (LivenessAnnouncement.PARKED, parked.key),
        (LivenessAnnouncement.PARKED, other.key),
        (LivenessAnnouncement.RELEASED, parked.key),
        (LivenessAnnouncement.RELEASED, other.key),
    ]


def test_settlement_is_conditional_on_the_row_it_read(tmp_path) -> None:
    store = SQLiteActionLivenessStore(tmp_path / "l.sqlite")
    first = _row(parked=False)
    assert store.settle(None, first, announce_parked=False)
    # Expected "no row" but one exists: a stale first-failure settlement.
    assert not store.settle(None, _row(parked=True), announce_parked=True)
    assert store.row(first.key) == first
    # Expected the row that is there: applied.
    assert store.settle(first, _row(parked=True), announce_parked=True)


def test_an_owed_withdrawal_is_forgotten_only_under_an_escalated_park(tmp_path) -> None:
    store = SQLiteActionLivenessStore(tmp_path / "l.sqlite")
    store.request_release(410)
    store.put(_row(escalated=False))

    assert not store.clear_release_if_escalated_park(410)
    assert [p.issue_number for p in store.pending_releases()] == [410]

    store.put(_row(action="add_label", escalated=True))
    assert store.clear_release_if_escalated_park(410)
    assert store.pending_releases() == ()
