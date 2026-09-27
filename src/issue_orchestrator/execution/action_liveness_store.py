"""SQLite-backed :class:`~..ports.action_liveness.ActionLivenessStore` (#7350).

Its own file, ``state/action_liveness.sqlite``, registered in the sqlite
registry for integrity checks, pragmas and backups. Deliberately NOT a table in
``timeline.sqlite``: that store drops its table on any schema-version change,
and a liveness budget that can be dropped is a budget that resets.

Schema changes are additive only (``CREATE ... IF NOT EXISTS`` now, an
``ALTER TABLE ... ADD COLUMN`` list later), never a version bump.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from ..domain.action_liveness import (
    ActionIdentity,
    LivenessKey,
    LivenessRow,
    OutcomeKind,
)
from ..infra.sqlite_connection import open_sqlite

_SCHEMA = """
CREATE TABLE IF NOT EXISTS action_liveness (
    subject TEXT NOT NULL,
    action TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    escalation_issue INTEGER,
    attempts INTEGER NOT NULL CHECK (attempts >= 0),
    first_failed_at TEXT NOT NULL,
    last_failed_at TEXT NOT NULL,
    last_outcome TEXT NOT NULL,
    last_reason TEXT NOT NULL,
    next_attempt_at TEXT,
    escalated INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (subject, action, fingerprint)
);
CREATE INDEX IF NOT EXISTS action_liveness_escalation_issue
    ON action_liveness (escalation_issue);
"""

_COLUMNS = (
    "subject, action, fingerprint, escalation_issue, attempts, first_failed_at,"
    " last_failed_at, last_outcome, last_reason, next_attempt_at, escalated"
)


class SQLiteActionLivenessStore:
    """One connection per thread; writes serialized by a process lock."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._local = threading.local()
        self._write_lock = threading.Lock()
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._connection().executescript(_SCHEMA)

    def _connection(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = open_sqlite(
                self._db_path,
                timeout=30.0,
                check_same_thread=False,
                row_factory=sqlite3.Row,
            )
            self._local.conn = conn
        return conn

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        with self._write_lock:
            conn = self._connection()
            with conn:
                yield conn

    def row(self, key: LivenessKey) -> LivenessRow | None:
        found = self._connection().execute(
            f"SELECT {_COLUMNS} FROM action_liveness"
            " WHERE subject=? AND action=? AND fingerprint=?",
            (key.identity.subject, key.identity.action, key.fingerprint),
        ).fetchone()
        return None if found is None else _row(found)

    def put(self, row: LivenessRow) -> None:
        key = row.key
        with self._write() as conn:
            conn.execute(
                f"INSERT OR REPLACE INTO action_liveness ({_COLUMNS})"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    key.identity.subject,
                    key.identity.action,
                    key.fingerprint,
                    key.escalation_issue,
                    row.attempts,
                    row.first_failed_at.isoformat(),
                    row.last_failed_at.isoformat(),
                    row.last_outcome.value,
                    row.last_reason,
                    None if row.next_attempt_at is None else row.next_attempt_at.isoformat(),
                    int(row.escalated),
                ),
            )

    def clear_identity(self, identity: ActionIdentity) -> tuple[LivenessRow, ...]:
        return self._delete_where(
            "subject=? AND action=?", (identity.subject, identity.action)
        )

    def clear_escalation_issue(self, issue_number: int) -> tuple[LivenessRow, ...]:
        return self._delete_where("escalation_issue=?", (issue_number,))

    def escalated_rows_for_issue(self, issue_number: int) -> tuple[LivenessRow, ...]:
        return self._select(
            "escalation_issue=? AND escalated=1 AND next_attempt_at IS NULL",
            (issue_number,),
        )

    def parked_rows(self) -> tuple[LivenessRow, ...]:
        return self._select("next_attempt_at IS NULL", ())

    def _select(self, where: str, params: tuple[object, ...]) -> tuple[LivenessRow, ...]:
        rows = self._connection().execute(
            f"SELECT {_COLUMNS} FROM action_liveness WHERE {where}"
            " ORDER BY last_failed_at, subject, action, fingerprint",
            params,
        ).fetchall()
        return tuple(_row(found) for found in rows)

    def _delete_where(
        self, where: str, params: tuple[object, ...]
    ) -> tuple[LivenessRow, ...]:
        with self._write() as conn:
            rows = conn.execute(
                f"SELECT {_COLUMNS} FROM action_liveness WHERE {where}", params
            ).fetchall()
            conn.execute(f"DELETE FROM action_liveness WHERE {where}", params)
        return tuple(_row(found) for found in rows)


def _row(found: sqlite3.Row) -> LivenessRow:
    next_attempt_at = found["next_attempt_at"]
    return LivenessRow(
        key=LivenessKey(
            identity=ActionIdentity(found["subject"], found["action"]),
            fingerprint=found["fingerprint"],
            escalation_issue=found["escalation_issue"],
        ),
        attempts=found["attempts"],
        first_failed_at=datetime.fromisoformat(found["first_failed_at"]),
        last_failed_at=datetime.fromisoformat(found["last_failed_at"]),
        last_outcome=OutcomeKind(found["last_outcome"]),
        last_reason=found["last_reason"],
        next_attempt_at=(
            None if next_attempt_at is None else datetime.fromisoformat(next_attempt_at)
        ),
        escalated=bool(found["escalated"]),
    )


__all__ = ["SQLiteActionLivenessStore"]
