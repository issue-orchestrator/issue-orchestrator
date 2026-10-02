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
    LivenessAnnouncement,
    LivenessKey,
    LivenessRow,
    OutcomeKind,
)
from ..infra.sqlite_connection import open_sqlite
from ..domain.owed_write import EffectDebt
from ..ports.action_liveness import PendingPause, PendingRelease

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
    escalation_retry_at TEXT,
    escalation_first_failed_at TEXT,
    parked_on_engine TEXT,
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
    last_reason TEXT NOT NULL,
    kind TEXT NOT NULL
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
    attempted_at TEXT,
    retry_at TEXT,
    first_failed_at TEXT
);
CREATE TABLE IF NOT EXISTS action_liveness_pause (
    issue_number INTEGER PRIMARY KEY CHECK (issue_number > 0),
    reason TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    attempted_at TEXT,
    retry_at TEXT,
    first_failed_at TEXT
);
"""

#: Columns added after a table first shipped: (table, column, ALTER statement).
#: Additive only, applied when missing, so an existing database keeps its rows.
_ADDED_COLUMNS = (
    ("action_liveness", "escalation_retry_at",
     "ALTER TABLE action_liveness ADD COLUMN escalation_retry_at TEXT"),
    ("action_liveness", "escalation_first_failed_at",
     "ALTER TABLE action_liveness ADD COLUMN escalation_first_failed_at TEXT"),
    ("action_liveness", "parked_on_engine",
     "ALTER TABLE action_liveness ADD COLUMN parked_on_engine TEXT"),
    ("action_liveness_release", "retry_at",
     "ALTER TABLE action_liveness_release ADD COLUMN retry_at TEXT"),
    ("action_liveness_release", "first_failed_at",
     "ALTER TABLE action_liveness_release ADD COLUMN first_failed_at TEXT"),
)
_TABLE_COLUMNS = {
    "action_liveness": "SELECT name FROM pragma_table_info('action_liveness')",
    "action_liveness_release": "SELECT name FROM pragma_table_info('action_liveness_release')",
}

_SELECT = (
    "SELECT subject, action, fingerprint, escalation_issue, attempts, first_failed_at,"
    " last_failed_at, last_outcome, last_reason, next_attempt_at, escalated,"
    " explained, escalation_attempts, escalation_attempted_at, last_planned_at,"
    " escalation_retry_at, escalation_first_failed_at, parked_on_engine"
    " FROM action_liveness"
)
_ORDER = " ORDER BY last_failed_at, subject, action, fingerprint"
_BY_KEY = _SELECT + " WHERE subject=? AND action=? AND fingerprint=?"
_BY_IDENTITY = _SELECT + " WHERE subject=? AND action=?"
_BY_ISSUE = _SELECT + " WHERE escalation_issue=?"
_PARKED_ON_ISSUE = (
    _SELECT + " WHERE escalation_issue=? AND next_attempt_at IS NULL" + _ORDER
)
_PARKED = _SELECT + " WHERE next_attempt_at IS NULL" + _ORDER
_ALL = _SELECT + _ORDER
_WAITING = (
    _SELECT + " WHERE next_attempt_at IS NOT NULL AND last_outcome='waiting'" + _ORDER
)
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
    " escalation_attempted_at, last_planned_at, escalation_retry_at,"
    " escalation_first_failed_at, parked_on_engine)"
    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)
_CLAIM_ENGINE_REPROBE = (
    "UPDATE action_liveness SET parked_on_engine=?"
    " WHERE subject=? AND action=? AND fingerprint=? AND next_attempt_at IS NULL"
    " AND (parked_on_engine IS NULL OR parked_on_engine != ?)"
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
    " escalation_attempted_at=?, escalation_retry_at=?, escalation_first_failed_at=?"
    " WHERE subject=? AND action=? AND fingerprint=?"
    " AND first_failed_at=? AND next_attempt_at IS NULL"
)
_DELETE_KEY = "DELETE FROM action_liveness WHERE subject=? AND action=? AND fingerprint=?"
_DELETE_ISSUE = "DELETE FROM action_liveness WHERE escalation_issue=?"
_FORGET_RELEASE = "DELETE FROM action_liveness_release WHERE issue_number=?"
_ESCALATED_PARK_ON_ISSUE = (
    "SELECT 1 FROM action_liveness WHERE escalation_issue=?"
    " AND next_attempt_at IS NULL AND escalated=1"
)
_FORGET_RELEASE_UNDER_ESCALATED_PARK = (
    _FORGET_RELEASE + " AND EXISTS (" + _ESCALATED_PARK_ON_ISSUE + ")"
)
_OWE_ANNOUNCEMENT = (
    "INSERT INTO action_liveness_announcement (subject, action, fingerprint,"
    " escalation_issue, attempts, first_failed_at, last_failed_at, last_outcome,"
    " last_reason, kind) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)
_ANNOUNCEMENTS = (
    "SELECT announcement_id, subject, action, fingerprint, escalation_issue, attempts,"
    " first_failed_at, last_failed_at, last_outcome, last_reason, kind"
    " FROM action_liveness_announcement ORDER BY announcement_id"
)
_FORGET_ANNOUNCEMENT = "DELETE FROM action_liveness_announcement WHERE announcement_id=?"
_OWE_RELEASE = "INSERT OR IGNORE INTO action_liveness_release (issue_number) VALUES (?)"
_RELEASES = (
    "SELECT issue_number, attempts, attempted_at, retry_at, first_failed_at"
    " FROM action_liveness_release ORDER BY issue_number"
)
_SET_RELEASE_DEBT = (
    "UPDATE action_liveness_release SET attempts=?, attempted_at=?, retry_at=?,"
    " first_failed_at=? WHERE issue_number=?"
)
_OWE_PAUSE = (
    "INSERT OR IGNORE INTO action_liveness_pause (issue_number, reason) VALUES (?, ?)"
)
_PAUSE_COLUMNS = "SELECT issue_number, reason, attempts, attempted_at, retry_at, first_failed_at"
_PAUSES = _PAUSE_COLUMNS + " FROM action_liveness_pause ORDER BY issue_number"
_PAUSE = _PAUSE_COLUMNS + " FROM action_liveness_pause WHERE issue_number=?"
_SET_PAUSE_DEBT = (
    "UPDATE action_liveness_pause SET attempts=?, attempted_at=?, retry_at=?,"
    " first_failed_at=? WHERE issue_number=?"
)
_FORGET_PAUSE = "DELETE FROM action_liveness_pause WHERE issue_number=?"


#: The tables a cold reader (``io engine-audit``, #7490) needs to find in a
#: copy of this store's database before opening the store on it: opening
#: creates missing tables, which would read a damaged file as an empty one.
AUDIT_TABLES = ("action_liveness", "action_liveness_pause")


class SQLiteActionLivenessStore:
    """One connection per thread; writes serialized by a process lock."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._local = threading.local()
        self._write_lock = threading.Lock()
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._connection().executescript(_SCHEMA)
        with self._write() as conn:
            for table, column, statement in _ADDED_COLUMNS:
                present = {found["name"] for found in conn.execute(_TABLE_COLUMNS[table])}
                if column not in present:
                    conn.execute(statement)

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
        """One write transaction that owns the database from its first read.

        ``BEGIN IMMEDIATE`` takes SQLite's write lock before anything is read,
        so a decision read inside the transaction (``settle``'s expected row,
        a release's rows) cannot be overtaken by another connection's commit
        -- the operator CLI's, say -- before the write that depends on it.
        """
        with self._write_lock:
            conn = self._connection()
            with conn:
                conn.execute("BEGIN IMMEDIATE")
                yield conn

    def row(self, key: LivenessKey) -> LivenessRow | None:
        found = self._connection().execute(
            _BY_KEY, (key.identity.subject, key.identity.action, key.fingerprint)
        ).fetchone()
        return None if found is None else _row(found)

    def put(self, row: LivenessRow) -> None:
        with self._write() as conn:
            _upsert(conn, row)

    def clear_key(self, key: LivenessKey, *, done_at: datetime) -> tuple[LivenessRow, ...]:
        return self._forget(
            _BY_KEY,
            (key.identity.subject, key.identity.action, key.fingerprint),
            progress=(key.identity.subject, key.identity.action, done_at.isoformat()),
        )

    def claim_engine_reprobe(self, key: LivenessKey, engine: str) -> bool:
        with self._write() as conn:
            claimed = conn.execute(
                _CLAIM_ENGINE_REPROBE,
                (engine, key.identity.subject, key.identity.action, key.fingerprint, engine),
            ).rowcount
        return claimed == 1

    def update_escalation(self, row: LivenessRow) -> bool:
        key = row.key
        with self._write() as conn:
            updated = conn.execute(
                _UPDATE_ESCALATION,
                (
                    int(row.escalated), int(row.explained), *_debt_columns(row.escalation),
                    key.identity.subject, key.identity.action, key.fingerprint,
                    row.first_failed_at.isoformat(),
                ),
            ).rowcount
        return updated == 1

    def release_identity(self, identity: ActionIdentity) -> tuple[LivenessRow, ...]:
        return self._forget(_BY_IDENTITY, (identity.subject, identity.action))

    def settle(
        self, expected: LivenessRow | None, row: LivenessRow, *, announce_parked: bool
    ) -> bool:
        key = row.key
        with self._write() as conn:
            current = conn.execute(
                _BY_KEY, (key.identity.subject, key.identity.action, key.fingerprint)
            ).fetchone()
            if expected is None and current is not None:
                return False
            if expected is not None and (
                current is None
                or current["first_failed_at"] != expected.first_failed_at.isoformat()
                or current["attempts"] != expected.attempts
            ):
                return False
            _upsert(conn, row)
            if announce_parked:
                _owe_announcement(conn, row, LivenessAnnouncement.PARKED)
        return True

    def pending_announcements(
        self,
    ) -> tuple[tuple[int, LivenessAnnouncement, LivenessRow], ...]:
        return tuple(
            (
                found["announcement_id"],
                LivenessAnnouncement(found["kind"]),
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
                if row.parked:
                    _owe_announcement(conn, row, LivenessAnnouncement.RELEASED)
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
            conn.execute(_FORGET_PAUSE, (issue_number,))
            for row in rows:
                if row.parked:
                    _owe_announcement(conn, row, LivenessAnnouncement.RELEASED)
        return rows

    def parked_rows_for_issue(self, issue_number: int) -> tuple[LivenessRow, ...]:
        return self._select(_PARKED_ON_ISSUE, (issue_number,))

    def parked_rows(self) -> tuple[LivenessRow, ...]:
        return self._select(_PARKED, ())

    def waiting_rows(self) -> tuple[LivenessRow, ...]:
        return self._select(_WAITING, ())

    def all_rows(self) -> tuple[LivenessRow, ...]:
        """Every row: parks, waits and backing-off failures (the engine audit, #7490)."""
        return self._select(_ALL, ())

    def visible_rows(self) -> tuple[LivenessRow, ...]:
        """What the tech-lead board shows: every park, then every wait."""
        return self.parked_rows() + self.waiting_rows()

    def rows_owing_escalation(self) -> tuple[LivenessRow, ...]:
        return self._select(_OWING_ESCALATION, ())

    def request_release(self, issue_number: int) -> None:
        with self._write() as conn:
            conn.execute(_OWE_RELEASE, (issue_number,))

    def pending_releases(self) -> tuple[PendingRelease, ...]:
        return tuple(
            PendingRelease(found["issue_number"], _debt(found))
            for found in self._connection().execute(_RELEASES).fetchall()
        )

    def set_release_debt(self, issue_number: int, debt: EffectDebt) -> None:
        with self._write() as conn:
            conn.execute(_SET_RELEASE_DEBT, (*_debt_columns(debt), issue_number))

    def request_pause(self, issue_number: int, reason: str) -> PendingPause:
        with self._write() as conn:
            conn.execute(_OWE_PAUSE, (issue_number, reason))
            return _pause(conn.execute(_PAUSE, (issue_number,)).fetchone())

    def pending_pauses(self) -> tuple[PendingPause, ...]:
        return tuple(_pause(found) for found in self._connection().execute(_PAUSES).fetchall())

    def pending_pause(self, issue_number: int) -> PendingPause | None:
        found = self._connection().execute(_PAUSE, (issue_number,)).fetchone()
        return None if found is None else _pause(found)

    def set_pause_debt(self, issue_number: int, debt: EffectDebt) -> None:
        with self._write() as conn:
            conn.execute(_SET_PAUSE_DEBT, (*_debt_columns(debt), issue_number))

    def clear_pause(self, issue_number: int) -> None:
        with self._write() as conn:
            conn.execute(_FORGET_PAUSE, (issue_number,))

    def clear_release(self, issue_number: int) -> None:
        with self._write() as conn:
            conn.execute(_FORGET_RELEASE, (issue_number,))

    def clear_release_if_escalated_park(self, issue_number: int) -> bool:
        with self._write() as conn:
            # One statement decides and deletes, so a park released by another
            # connection between a read and the delete cannot lose its debt;
            # it also opens the write transaction the answer is read inside.
            conn.execute(_FORGET_RELEASE_UNDER_ESCALATED_PARK, (issue_number, issue_number))
            return conn.execute(_ESCALATED_PARK_ON_ISSUE, (issue_number,)).fetchone() is not None

    def _select(self, query: str, params: tuple[object, ...]) -> tuple[LivenessRow, ...]:
        rows = self._connection().execute(query, params).fetchall()
        return tuple(_row(found) for found in rows)

def _upsert(conn: sqlite3.Connection, row: LivenessRow) -> None:
    key = row.key
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
            row.escalation.attempts,
            _iso(row.escalation.attempted_at),
            _iso(row.last_planned_at),
            _iso(row.escalation.retry_at),
            _iso(row.escalation.first_failed_at),
            row.parked_on_engine,
        ),
    )


def _owe_announcement(
    conn: sqlite3.Connection, row: LivenessRow, kind: LivenessAnnouncement
) -> None:
    identity = row.key.identity
    conn.execute(
        _OWE_ANNOUNCEMENT,
        (
            identity.subject, identity.action, row.key.fingerprint,
            row.key.escalation_issue, row.attempts,
            row.first_failed_at.isoformat(), row.last_failed_at.isoformat(),
            row.last_outcome.value, row.last_reason, kind.value,
        ),
    )


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
        escalation=EffectDebt(
            attempts=found["escalation_attempts"],
            attempted_at=_parse(found["escalation_attempted_at"]),
            retry_at=_parse(found["escalation_retry_at"]),
            first_failed_at=_parse(found["escalation_first_failed_at"]),
        ),
        last_planned_at=_parse(found["last_planned_at"]),
        parked_on_engine=found["parked_on_engine"],
    )


def _debt_columns(debt: EffectDebt) -> tuple[object, ...]:
    """(attempts, attempted_at, retry_at, first_failed_at), in column order."""
    return (
        debt.attempts, _iso(debt.attempted_at), _iso(debt.retry_at),
        _iso(debt.first_failed_at),
    )


def _debt(found: sqlite3.Row) -> EffectDebt:
    return EffectDebt(
        attempts=found["attempts"],
        attempted_at=_parse(found["attempted_at"]),
        retry_at=_parse(found["retry_at"]),
        first_failed_at=_parse(found["first_failed_at"]),
    )


def _pause(found: sqlite3.Row) -> PendingPause:
    return PendingPause(found["issue_number"], found["reason"], _debt(found))


__all__ = ["SQLiteActionLivenessStore"]
