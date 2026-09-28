"""Cold, read-only census of a validated-work database (#7490).

The audit's view of validated work: every record counted by state and
resolution, and every unresolved record with its admission instant. Read
through the same compatibility-checked read transaction as cold discovery
(:mod:`.validated_work_read_schema`), so it needs no claims, capabilities or
migrations, and cannot write.

Unresolved means :data:`~..domain.validated_work.UNRESOLVED_STATES`, the
domain's own set, not a timestamp: ``terminal_at`` is ``''`` (never NULL) on
an unresolved row.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

from ..domain.read_only_sqlite import ReadOnlySqliteAccessError, ReadOnlySqliteFailure
from ..domain.validated_work import UNRESOLVED_STATES
from ..ports.engine_audit import UnresolvedWorkRecord, ValidatedWorkCensus
from .sqlite_readonly import readonly_sqlite_transaction
from .validated_work_read_schema import require_validated_work_columns

_CENSUS_COLUMNS = {
    "validated_work_records": frozenset(
        {"record_id", "issue_number", "state", "resolution_kind", "created_at"}
    )
}


class SqliteValidatedWorkCensus:
    """One census read over a validated-work database file."""

    def __init__(self, database: Path, *, timeout: float) -> None:
        self._database = database
        self._timeout = timeout

    def census(self) -> ValidatedWorkCensus:
        unresolved_states = frozenset(state.value for state in UNRESOLVED_STATES)
        with readonly_sqlite_transaction(
            self._database, timeout=self._timeout, row_factory=sqlite3.Row
        ) as conn:
            require_validated_work_columns(conn, _CENSUS_COLUMNS)
            counts = conn.execute(
                "SELECT state, resolution_kind, COUNT(*) AS n FROM validated_work_records"
                " GROUP BY state, resolution_kind ORDER BY n DESC, state, resolution_kind"
            ).fetchall()
            records = conn.execute(
                "SELECT record_id, issue_number, state, created_at FROM validated_work_records"
                " ORDER BY created_at, record_id"
            ).fetchall()
        unresolved = [row for row in records if row["state"] in unresolved_states]
        return ValidatedWorkCensus(
            by_state_resolution=tuple(
                (row["state"], row["resolution_kind"], int(row["n"])) for row in counts
            ),
            unresolved=tuple(
                UnresolvedWorkRecord(
                    record_id=row["record_id"],
                    issue_number=int(row["issue_number"]),
                    state=row["state"],
                    created_at=_created_at(row["record_id"], row["created_at"]),
                )
                for row in unresolved
            ),
        )


def _created_at(record_id: str, stored: object) -> datetime:
    """A row's admission instant: written from an aware ISO instant, so a value
    that does not parse, or has no offset, is a damaged row the census refuses."""
    try:
        value = datetime.fromisoformat(str(stored))
    except ValueError:
        value = None
    if value is None or value.tzinfo is None:
        raise ReadOnlySqliteAccessError(
            ReadOnlySqliteFailure.UNREADABLE,
            f"validated-work record {record_id} has an unreadable created_at: {stored!r}",
        )
    return value


__all__ = ["SqliteValidatedWorkCensus"]
