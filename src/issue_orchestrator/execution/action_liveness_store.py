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
from ..ports.action_liveness import PendingRelease

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
    explained INTEGER NOT NULL DEFAULT 0,
    escalation_attempts INTEGER NOT NULL DEFAULT 0 CHECK (escalation_attempts >= 0),
    escalation_attempted_at TEXT,
    last_planned_at TEXT,
    PRIMARY KEY (subject, action, fingerprint)
);
CREATE INDEX IF NOT EXISTS action_liveness_escalation_issue
    ON action_liveness (escalation_issue);
CREATE TABLE IF NOT EXISTS action_liveness_announcement (
    announcement_id INTEGER PRIMARY KEY AUTOINCREMENT,
    subject TEXT NOT NULL,
    action TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    escalation_issue INTEGER,
    attempts INTEGER NOT NULL,
    first_failed_at TEXT NOT NULL,
    last_failed_at TEXT NOT NULL,
    last_outcome TEXT NOT NULL,
    last_reason TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS action_liveness_progress (
    subject TEXT NOT NULL,
    action TEXT NOT NULL,
    last_done_at TEXT NOT NULL,
    PRIMARY KEY (subject, action)
);
CREATE TABLE IF NOT EXISTS action_liveness_release (
    issue_number INTEGER PRIMARY KEY CHECK (issue_number > 0),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    attempted_at TEXT
);
"""

_SELECT = (
    "SELECT subject, action, fingerprint, escalation_issue, attempts, first_failed_at,"
    " last_failed_at, last_outcome, last_reason, next_attempt_at, escalated,"
    " explained, escalation_attempts, escalation_attempted_at, last_planned_at"
    " FROM action_liveness"
)
_ORDER = " ORDER BY last_failed_at, subject, action, fingerprint"
_BY_KEY = _SELECT + " WHERE subject=? AND action=? AND fingerprint=?"
_BY_IDENTITY = _SELECT + " WHERE subject=? AND action=?"
_BY_ISSUE = _SELECT + " WHERE escalation_issue=?"
_ESCALATED_ON_ISSUE = (
    _SELECT + " WHERE escalation_issue=? AND escalated=1 AND next_attempt_at IS NULL" + _ORDER
)
_PARKED = _SELECT + " WHERE next_attempt_at IS NULL" + _ORDER
_OWING_ESCALATION = (
    _SELECT
    + " WHERE next_attempt_at IS NULL AND escalation_issue IS NOT NULL"
    + " AND (escalated=0 OR explained=0)"
    + _ORDER
)
_UPSERT = (
    "INSERT OR REPLACE INTO action_liveness (subject, action, fingerprint,"
    " escalation_issue, attempts, first_failed_at, last_failed_at, last_outcome,"
    " last_reason, next_attempt_at, escalated, explained, escalation_attempts,"
    " escalation_attempted_at, last_planned_at)"
    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)
_TOUCH = (
    "UPDATE action_liveness SET last_planned_at=?"
    " WHERE subject=? AND action=? AND fingerprint=?"
)
_RETIRABLE_WHERE = (
    " WHERE COALESCE(last_planned_at, last_failed_at) < ?"
    " OR (COALESCE(last_planned_at, last_failed_at) < ? AND EXISTS ("
    "SELECT 1 FROM action_liveness_progress p WHERE p.subject = action_liveness.subject"
    " AND p.action = action_liveness.action"
    " AND p.last_done_at > COALESCE(action_liveness.last_planned_at,"
    " action_liveness.last_failed_at)))"
)
_RETIRABLE = _SELECT + _RETIRABLE_WHERE
_NOTE_PROGRESS = (
    "INSERT OR REPLACE INTO action_liveness_progress (subject, action, last_done_at)"
    " VALUES (?, ?, ?)"
)
_UPDATE_ESCALATION = (
    "UPDATE action_liveness SET escalated=?, explained=?, escalation_attempts=?,"
    " escalation_attempted_at=? WHERE subject=? AND action=? AND fingerprint=?"
    " AND first_failed_at=? AND next_attempt_at IS NULL"
)
_DELETE_KEY = "DELETE FROM action_liveness WHERE subject=? AND action=? AND fingerprint=?"
_DELETE_ISSUE = "DELETE FROM action_liveness WHERE escalation_issue=?"
_FORGET_RELEASE = "DELETE FROM action_liveness_release WHERE issue_number=?"
_OWE_ANNOUNCEMENT = (
    "INSERT INTO action_liveness_announcement (subject, action, fingerprint,"
    " escalation_issue, attempts, first_failed_at, last_failed_at, last_outcome,"
    " last_reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
)
_ANNOUNCEMENTS = (
    "SELECT announcement_id, subject, action, fingerprint, escalation_issue, attempts,"
    " first_failed_at, last_failed_at, last_outcome, last_reason"
    " FROM action_liveness_announcement ORDER BY announcement_id"
)
_FORGET_ANNOUNCEMENT = "DELETE FROM action_liveness_announcement WHERE announcement_id=?"
_OWE_RELEASE = "INSERT OR IGNORE INTO action_liveness_release (issue_number) VALUES (?)"


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
            _BY_KEY, (key.identity.subject, key.identity.action, key.fingerprint)
        ).fetchone()
        return None if found is None else _row(found)

    def put(self, row: LivenessRow) -> None:
        key = row.key
        with self._write() as conn:
            conn.execute(
                _UPSERT,
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
                    _iso(row.next_attempt_at),
                    int(row.escalated),
                    int(row.explained),
                    row.escalation_attempts,
                    _iso(row.escalation_attempted_at),
                    _iso(row.last_planned_at),
                ),
            )

    def clear_key(self, key: LivenessKey, *, done_at: datetime) -> tuple[LivenessRow, ...]:
        return self._forget(
            _BY_KEY,
            (key.identity.subject, key.identity.action, key.fingerprint),
            progress=(key.identity.subject, key.identity.action, done_at.isoformat()),
        )

    def update_escalation(self, row: LivenessRow) -> bool:
        key = row.key
        with self._write() as conn:
            updated = conn.execute(
                _UPDATE_ESCALATION,
                (
                    int(row.escalated), int(row.explained), row.escalation_attempts,
                    _iso(row.escalation_attempted_at),
                    key.identity.subject, key.identity.action, key.fingerprint,
                    row.first_failed_at.isoformat(),
                ),
            ).rowcount
        return updated == 1

    def release_identity(self, identity: ActionIdentity) -> tuple[LivenessRow, ...]:
        rows = self._forget(
            _BY_IDENTITY, (identity.subject, identity.action), announce=True
        )
        return rows

    def pending_announcements(self) -> tuple[tuple[int, LivenessRow], ...]:
        return tuple(
            (
                found["announcement_id"],
                LivenessRow(
                    key=LivenessKey(
                        ActionIdentity(found["subject"], found["action"]),
                        found["fingerprint"],
                        found["escalation_issue"],
                    ),
                    attempts=found["attempts"],
                    first_failed_at=datetime.fromisoformat(found["first_failed_at"]),
                    last_failed_at=datetime.fromisoformat(found["last_failed_at"]),
                    last_outcome=OutcomeKind(found["last_outcome"]),
                    last_reason=found["last_reason"],
                    next_attempt_at=None,
                ),
            )
            for found in self._connection().execute(_ANNOUNCEMENTS).fetchall()
        )

    def clear_announcement(self, announcement_id: int) -> None:
        with self._write() as conn:
            conn.execute(_FORGET_ANNOUNCEMENT, (announcement_id,))

    def retire_unplanned(
        self, *, abandoned_before: datetime, superseded_before: datetime
    ) -> tuple[LivenessRow, ...]:
        return self._forget(
            _RETIRABLE, (abandoned_before.isoformat(), superseded_before.isoformat())
        )

    def touch(self, key: LivenessKey, planned_at: datetime) -> None:
        with self._write() as conn:
            conn.execute(
                _TOUCH,
                (planned_at.isoformat(), key.identity.subject, key.identity.action,
                 key.fingerprint),
            )

    def _forget(
        self,
        select: str,
        params: tuple[object, ...],
        *,
        announce: bool = False,
        progress: tuple[str, str, str] | None = None,
    ) -> tuple[LivenessRow, ...]:
        """Delete the selected rows and owe their blocks' release, atomically.

        A crash between forgetting an escalated park and recording that its
        block must come off would leave the block on the issue with nothing
        left to take it off.
        """
        with self._write() as conn:
            if progress is not None:
                # A success notes its progress in the SAME transaction that
                # forgets its park: neither can land without the other.
                conn.execute(_NOTE_PROGRESS, progress)
            rows = tuple(_row(found) for found in conn.execute(select, params))
            for row in rows:
                identity = row.key.identity
                conn.execute(_DELETE_KEY, (identity.subject, identity.action, row.key.fingerprint))
                if announce and row.parked:
                    conn.execute(
                        _OWE_ANNOUNCEMENT,
                        (
                            identity.subject, identity.action, row.key.fingerprint,
                            row.key.escalation_issue, row.attempts,
                            row.first_failed_at.isoformat(), row.last_failed_at.isoformat(),
                            row.last_outcome.value, row.last_reason,
                        ),
                    )
            for issue in sorted(
                {
                    row.key.escalation_issue
                    for row in rows
                    if row.escalated and row.key.escalation_issue is not None
                }
            ):
                conn.execute(_OWE_RELEASE, (issue,))
        return rows

    def clear_escalation_issue(self, issue_number: int) -> tuple[LivenessRow, ...]:
        with self._write() as conn:
            rows = tuple(_row(found) for found in conn.execute(_BY_ISSUE, (issue_number,)))
            conn.execute(_DELETE_ISSUE, (issue_number,))
            conn.execute(_FORGET_RELEASE, (issue_number,))
        return rows

    def escalated_rows_for_issue(self, issue_number: int) -> tuple[LivenessRow, ...]:
        return self._select(_ESCALATED_ON_ISSUE, (issue_number,))

    def parked_rows(self) -> tuple[LivenessRow, ...]:
        return self._select(_PARKED, ())

    def rows_owing_escalation(self) -> tuple[LivenessRow, ...]:
        return self._select(_OWING_ESCALATION, ())

    def request_release(self, issue_number: int) -> None:
        with self._write() as conn:
            conn.execute(_OWE_RELEASE, (issue_number,))

    def pending_releases(self) -> tuple[PendingRelease, ...]:
        rows = self._connection().execute(
            "SELECT issue_number, attempts, attempted_at FROM action_liveness_release"
            " ORDER BY issue_number"
        ).fetchall()
        return tuple(
            PendingRelease(row["issue_number"], row["attempts"], _parse(row["attempted_at"]))
            for row in rows
        )

    def record_release_attempt(self, issue_number: int, attempted_at: datetime) -> None:
        with self._write() as conn:
            conn.execute(
                "UPDATE action_liveness_release SET attempts=attempts+1, attempted_at=?"
                " WHERE issue_number=?",
                (attempted_at.isoformat(), issue_number),
            )

    def clear_release(self, issue_number: int) -> None:
        with self._write() as conn:
            conn.execute(_FORGET_RELEASE, (issue_number,))

    def _select(self, query: str, params: tuple[object, ...]) -> tuple[LivenessRow, ...]:
        rows = self._connection().execute(query, params).fetchall()
        return tuple(_row(found) for found in rows)

def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def _parse(value: str | None) -> datetime | None:
    return None if value is None else datetime.fromisoformat(value)


def _row(found: sqlite3.Row) -> LivenessRow:
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
        next_attempt_at=_parse(found["next_attempt_at"]),
        escalated=bool(found["escalated"]),
        explained=bool(found["explained"]),
        escalation_attempts=found["escalation_attempts"],
        escalation_attempted_at=_parse(found["escalation_attempted_at"]),
        last_planned_at=_parse(found["last_planned_at"]),
    )


__all__ = ["SQLiteActionLivenessStore"]
