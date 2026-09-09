"""Shared no-create/application-read-only SQLite profile."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from issue_orchestrator.domain.read_only_sqlite import (
    ReadOnlySqliteAccessError,
    ReadOnlySqliteFailure,
)
from issue_orchestrator.infra.sqlite_connection import (
    open_sqlite_readonly,
    readonly_sqlite_transaction,
)
from issue_orchestrator.infra import sqlite_readonly


def _database(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE facts (value TEXT NOT NULL)")
        connection.execute("INSERT INTO facts VALUES ('preserved')")


def test_readonly_profile_encodes_filesystem_uri_characters(tmp_path: Path) -> None:
    path = tmp_path / "repo ?#% ü" / "state.sqlite"
    _database(path)

    with readonly_sqlite_transaction(
        path, timeout=1, row_factory=sqlite3.Row
    ) as connection:
        assert connection.execute("SELECT value FROM facts").fetchone()["value"] == (
            "preserved"
        )


def test_missing_database_and_parent_are_not_created(tmp_path: Path) -> None:
    parent = tmp_path / "missing" / "nested"
    path = parent / "state.sqlite"

    with pytest.raises(ReadOnlySqliteAccessError) as caught:
        open_sqlite_readonly(path, timeout=1, row_factory=sqlite3.Row)

    assert caught.value.reason is ReadOnlySqliteFailure.DATABASE_ABSENT
    assert not parent.exists()


def test_present_non_file_database_path_is_unreadable(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite"
    path.mkdir()

    with pytest.raises(ReadOnlySqliteAccessError) as caught:
        open_sqlite_readonly(path, timeout=1, row_factory=sqlite3.Row)

    assert caught.value.reason is ReadOnlySqliteFailure.UNREADABLE


def test_connection_rejects_application_and_schema_writes(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite"
    _database(path)

    with readonly_sqlite_transaction(
        path, timeout=1, row_factory=sqlite3.Row
    ) as connection:
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("INSERT INTO facts VALUES ('changed')")
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("CREATE TABLE mutation (value TEXT)")
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("PRAGMA user_version=12")

    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT value FROM facts").fetchall() == [
            ("preserved",)
        ]
        assert connection.execute("PRAGMA user_version").fetchone() == (0,)


def test_reader_observes_committed_live_wal_and_closes_cleanly(
    tmp_path: Path,
) -> None:
    path = tmp_path / "state.sqlite"
    writer = sqlite3.connect(path)
    try:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("CREATE TABLE facts (value TEXT NOT NULL)")
        writer.execute("INSERT INTO facts VALUES ('from-wal')")
        writer.commit()

        with readonly_sqlite_transaction(
            path, timeout=1, row_factory=sqlite3.Row
        ) as reader:
            assert reader.execute("SELECT value FROM facts").fetchone()[0] == "from-wal"
    finally:
        writer.close()

    with sqlite3.connect(path) as reopened:
        reopened.execute("INSERT INTO facts VALUES ('after-reader-close')")
        reopened.commit()


def test_query_deadline_is_reported_as_typed_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "state.sqlite"
    _database(path)
    calls = 0

    def clock() -> float:
        nonlocal calls
        calls += 1
        return 0.0 if calls <= 2 else 2.0

    monkeypatch.setattr(sqlite_readonly.time, "monotonic", clock)

    with pytest.raises(ReadOnlySqliteAccessError) as caught:
        with readonly_sqlite_transaction(
            path, timeout=1, row_factory=sqlite3.Row
        ) as connection:
            connection.execute(
                "WITH RECURSIVE numbers(value) AS ("
                "VALUES(1) UNION ALL SELECT value+1 FROM numbers WHERE value<100000"
                ") SELECT sum(value) FROM numbers"
            ).fetchone()

    assert caught.value.reason is ReadOnlySqliteFailure.TIMEOUT


def test_short_transaction_cannot_return_success_after_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "state.sqlite"
    _database(path)
    now = [0.0]
    monkeypatch.setattr(sqlite_readonly.time, "monotonic", lambda: now[0])

    with pytest.raises(ReadOnlySqliteAccessError) as caught:
        with readonly_sqlite_transaction(
            path, timeout=1, row_factory=sqlite3.Row
        ) as connection:
            assert connection.execute("SELECT value FROM facts").fetchone()[0] == (
                "preserved"
            )
            now[0] = 2.0

    assert caught.value.reason is ReadOnlySqliteFailure.TIMEOUT


def test_lock_wait_obeys_deadline_and_reader_connection_closes(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite"
    _database(path)
    writer = sqlite3.connect(path)
    writer.execute("BEGIN EXCLUSIVE")
    started = time.monotonic()
    try:
        with pytest.raises(ReadOnlySqliteAccessError) as caught:
            with readonly_sqlite_transaction(
                path, timeout=0.03, row_factory=sqlite3.Row
            ) as connection:
                connection.execute("SELECT value FROM facts").fetchone()
        assert caught.value.reason is ReadOnlySqliteFailure.TIMEOUT
        assert time.monotonic() - started < 0.5
    finally:
        writer.rollback()
        writer.close()

    with readonly_sqlite_transaction(
        path, timeout=1, row_factory=sqlite3.Row
    ) as connection:
        assert connection.execute("SELECT value FROM facts").fetchone()[0] == (
            "preserved"
        )


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan"), True])
def test_readonly_profile_rejects_invalid_timeouts(
    tmp_path: Path, timeout: float
) -> None:
    with pytest.raises(ValueError):
        open_sqlite_readonly(
            tmp_path / "unused.sqlite", timeout=timeout, row_factory=sqlite3.Row
        )
