"""SQLite-backed label persistence store.

Tracks which labels the orchestrator has applied to issues, enabling
accurate cleanup on reset and UI display of orchestrator-owned labels.

Follows the same patterns as ``provider_circuit_store.py``.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from ..infra.sqlite_connection import open_sqlite

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS issue_labels (
    issue_number INTEGER NOT NULL,
    label TEXT NOT NULL,
    applied_at TEXT NOT NULL,
    PRIMARY KEY (issue_number, label)
);
"""


class LabelStore:
    """SQLite-backed persistence for orchestrator-applied labels."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._local = threading.local()
        self._write_lock = threading.Lock()
        self.initialize()

    def initialize(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = self._get_connection()
        conn.executescript(_SCHEMA)
        try:
            inode = os.stat(self._db_path).st_ino
        except FileNotFoundError:
            inode = None
        logger.info(
            "Label store initialized: db=%s inode=%s pid=%d",
            self._db_path,
            inode,
            os.getpid(),
        )

    def _get_connection(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = open_sqlite(self._db_path, row_factory=sqlite3.Row)
            self._local.conn = conn
        return conn

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._write_lock:
            conn = self._get_connection()
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def add_label(self, issue_number: int, label: str) -> None:
        """Record that a label was applied to an issue (upsert)."""
        now = datetime.now(timezone.utc).isoformat()
        with self._transaction() as tx:
            tx.execute(
                """
                INSERT INTO issue_labels (issue_number, label, applied_at)
                VALUES (?, ?, ?)
                ON CONFLICT(issue_number, label) DO UPDATE SET
                    applied_at = excluded.applied_at
                """,
                (issue_number, label, now),
            )

    def remove_label(self, issue_number: int, label: str) -> None:
        """Remove a label record for an issue."""
        with self._transaction() as tx:
            tx.execute(
                "DELETE FROM issue_labels WHERE issue_number = ? AND label = ?",
                (issue_number, label),
            )

    def save_labels(self, issue_number: int, labels: set[str]) -> None:
        """Replace all labels for an issue (atomic)."""
        now = datetime.now(timezone.utc).isoformat()
        with self._transaction() as tx:
            tx.execute(
                "DELETE FROM issue_labels WHERE issue_number = ?",
                (issue_number,),
            )
            for label in sorted(labels):
                tx.execute(
                    "INSERT INTO issue_labels (issue_number, label, applied_at) VALUES (?, ?, ?)",
                    (issue_number, label, now),
                )

    def load_labels(self, issue_number: int) -> set[str]:
        """Load all labels for an issue."""
        conn = self._get_connection()
        rows = conn.execute(
            "SELECT label FROM issue_labels WHERE issue_number = ?",
            (issue_number,),
        ).fetchall()
        return {row["label"] for row in rows}

    def load_all(self) -> dict[int, set[str]]:
        """Load labels for all issues."""
        conn = self._get_connection()
        rows = conn.execute("SELECT issue_number, label FROM issue_labels").fetchall()
        result: dict[int, set[str]] = {}
        for row in rows:
            result.setdefault(row["issue_number"], set()).add(row["label"])
        return result

    def remove_issue(self, issue_number: int) -> None:
        """Remove all label records for an issue."""
        with self._transaction() as tx:
            tx.execute(
                "DELETE FROM issue_labels WHERE issue_number = ?",
                (issue_number,),
            )

    def clear(self) -> None:
        """Remove all records."""
        with self._transaction() as tx:
            tx.execute("DELETE FROM issue_labels")
