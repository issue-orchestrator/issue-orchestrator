"""SQLite-backed queue cache store for persisting queue issues across restarts."""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Iterator, Sequence

from ..adapters.github.github_issue import GitHubIssue
from ..infra.sqlite_connection import open_sqlite

if TYPE_CHECKING:
    from ..ports.issue import Issue

_SCHEMA = """
CREATE TABLE IF NOT EXISTS queue_issues (
    number INTEGER PRIMARY KEY,
    repo TEXT NOT NULL,
    title TEXT NOT NULL,
    labels TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'open',
    body TEXT,
    milestone TEXT,
    milestone_number INTEGER,
    milestone_due_on TEXT
);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


class QueueCacheStore:
    """SQLite-backed store for queue cache persistence."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._local = threading.local()
        self._write_lock = threading.Lock()
        self.initialize()

    def initialize(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = self._get_connection()
        conn.executescript(_SCHEMA)

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

    def load_issues(self, repo: str) -> list[GitHubIssue]:
        """Load all cached issues, reconstructing GitHubIssue objects."""
        conn = self._get_connection()
        rows = conn.execute(
            "SELECT number, title, labels, state, body, milestone, "
            "milestone_number, milestone_due_on FROM queue_issues"
        ).fetchall()
        issues: list[GitHubIssue] = []
        for row in rows:
            issues.append(GitHubIssue(
                number=row["number"],
                repo=repo,
                title=row["title"],
                labels=tuple(json.loads(row["labels"])),
                state=row["state"],
                body=row["body"],
                milestone=row["milestone"],
                milestone_number=row["milestone_number"],
                milestone_due_on=row["milestone_due_on"],
            ))
        return issues

    def load_watermark(self) -> str | None:
        """Load the delta sync watermark."""
        conn = self._get_connection()
        row = conn.execute(
            "SELECT value FROM meta WHERE key = 'watermark'"
        ).fetchone()
        if row is None:
            return None
        return row["value"]

    def save_snapshot(
        self,
        issues: Sequence["Issue"],
        watermark: str | None,
        repo: str = "",
    ) -> None:
        """Replace all cached issues and update watermark in a single transaction.

        Args:
            issues: Issues to persist.
            watermark: Delta sync watermark (ISO timestamp).
            repo: Repository identifier stored with each issue row.
        """
        with self._transaction() as tx:
            tx.execute("DELETE FROM queue_issues")
            for issue in issues:
                tx.execute(
                    "INSERT INTO queue_issues "
                    "(number, repo, title, labels, state, body, milestone, "
                    "milestone_number, milestone_due_on) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        issue.number,
                        repo or getattr(issue, "repo", ""),
                        issue.title,
                        json.dumps(list(issue.labels)),
                        issue.state,
                        issue.body,
                        issue.milestone,
                        issue.milestone_number,
                        issue.milestone_due_on,
                    ),
                )
            if watermark is not None:
                tx.execute(
                    "INSERT INTO meta (key, value) VALUES ('watermark', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (watermark,),
                )

    def clear(self) -> None:
        """Delete all cached data."""
        with self._transaction() as tx:
            tx.execute("DELETE FROM queue_issues")
            tx.execute("DELETE FROM meta")
