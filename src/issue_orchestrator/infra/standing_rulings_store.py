"""SQLite index of each issue's standing rulings (#8141).

The local half of the record: GitHub's issue-body block is the truth, and
this is the engine's mirror of it (``ports/standing_rulings``), so a prompt or
a review check never needs a GitHub call. It lives in its own database file,
so nothing about it touches another store's schema version.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from ..domain.standing_ruling import StandingRuling
from ..ports.standing_rulings import SyncedRulings
from .repo_identity import state_dir
from .sqlite_connection import open_sqlite

_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS standing_rulings (
        issue_number INTEGER PRIMARY KEY,
        rulings TEXT NOT NULL,
        synced_at TEXT NOT NULL
    )
    """,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SqliteStandingRulingsIndex:
    """:class:`~..ports.standing_rulings.StandingRulingsIndex` over one SQLite file."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._local = threading.local()
        self._write_lock = threading.Lock()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._transaction() as tx:
            for statement in _SCHEMA:
                tx.execute(statement)

    @classmethod
    def for_repo(cls, repo_root: Path) -> "SqliteStandingRulingsIndex":
        """The index in a repository's orchestrator state directory (composition root only)."""
        return cls(state_dir(repo_root) / "standing_rulings.sqlite")

    def _connection(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = open_sqlite(self._db_path, row_factory=sqlite3.Row)
            self._local.conn = conn
        return conn

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._write_lock:
            conn = self._connection()
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def load(self, issue_number: int) -> tuple[StandingRuling, ...] | None:
        row = self._connection().execute(
            "SELECT rulings FROM standing_rulings WHERE issue_number = ?", (issue_number,)
        ).fetchone()
        return None if row is None else _decode(str(row["rulings"]))

    def save(self, issue_number: int, rulings: tuple[StandingRuling, ...]) -> None:
        payload = json.dumps([ruling.to_dict() for ruling in rulings], sort_keys=True)
        with self._transaction() as tx:
            tx.execute(
                "INSERT OR REPLACE INTO standing_rulings (issue_number, rulings, synced_at) VALUES (?, ?, ?)",
                (issue_number, payload, _now()),
            )

    def synced(self) -> dict[int, SyncedRulings]:
        rows = self._connection().execute(
            "SELECT issue_number, rulings, synced_at FROM standing_rulings ORDER BY issue_number"
        ).fetchall()
        found = {
            int(row["issue_number"]): SyncedRulings(_decode(str(row["rulings"])), str(row["synced_at"]))
            for row in rows
        }
        return {number: synced for number, synced in found.items() if synced.rulings}


def _decode(payload: str) -> tuple[StandingRuling, ...]:
    data = json.loads(payload)
    if not isinstance(data, list):
        raise ValueError("a standing-rulings index row is not a list")
    return tuple(StandingRuling.from_dict(item) for item in data)
