"""SQLite-backed warm cache for persisting in-scope issues across restarts."""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import traceback
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Iterator, Sequence

from ..adapters.github.github_issue import GitHubIssue
from ..infra.sqlite_connection import open_sqlite

if TYPE_CHECKING:
    from ..ports.issue import Issue

logger = logging.getLogger(__name__)

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
    """SQLite-backed store for the in-scope issue snapshot used for warm restarts."""

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

    def load_last_health_review_at(self) -> float:
        """Load the epoch timestamp of the last health-review anchor creation.

        Returns 0.0 when no health review has ever fired (ADR-0031 §4).
        """
        conn = self._get_connection()
        row = conn.execute(
            "SELECT value FROM meta WHERE key = 'last_health_review_at'"
        ).fetchone()
        if row is None:
            return 0.0
        return float(row["value"])

    def save_last_health_review_at(self, value: float) -> None:
        """Persist the last health-review anchor creation timestamp.

        Durable marker so an orchestrator restart does not re-fire the
        periodic health review before the configured interval elapses
        (ADR-0031 §4). Stored in the same ``meta`` key/value table as the
        delta watermark; ``clear()`` wipes it along with the rest of the
        cache (worst case: one early health review after a cache reset).
        """
        with self._transaction() as tx:
            tx.execute(
                "INSERT INTO meta (key, value) VALUES ('last_health_review_at', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (repr(value),),
            )

    def load_last_reviewed_board_fingerprint(self) -> str:
        """Load the board fingerprint reviewed by the last health review.

        Returns "" when none was ever recorded. On any loss (unset, or a wiped
        cache) the empty string makes the next due periodic review fire — the
        gate fails toward reviewing, never toward silently suppressing one.
        """
        conn = self._get_connection()
        row = conn.execute(
            "SELECT value FROM meta WHERE key = 'last_reviewed_board_fingerprint'"
        ).fetchone()
        if row is None:
            return ""
        return str(row["value"])

    def save_last_reviewed_board_fingerprint(self, value: str) -> None:
        """Persist the reviewed board fingerprint so a restart does not re-fire
        the periodic health review over an unchanged board (ADR-0031 §4).

        Stored in the same ``meta`` key/value table as ``last_health_review_at``;
        ``clear()`` wipes it (worst case: one extra health review after a cache
        reset — the fail-toward-reviewing side).
        """
        with self._transaction() as tx:
            tx.execute(
                "INSERT INTO meta (key, value) VALUES "
                "('last_reviewed_board_fingerprint', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (value,),
            )

    def save_snapshot(
        self,
        issues: Sequence["Issue"],
        watermark: str | None,
        repo: str = "",
    ) -> None:
        """Replace all cached issues and update watermark in a single transaction.

        Args:
            issues: Full in-scope issues to persist for warm restore.
            watermark: Delta sync watermark (ISO timestamp).
            repo: Repository identifier stored with each issue row.
        """
        new_count = len(issues)
        with self._transaction() as tx:
            prior_count = tx.execute("SELECT COUNT(*) FROM queue_issues").fetchone()[0]
            if prior_count > 0 and new_count == 0:
                # Wiping persisted queue cache is suspicious — a following warm
                # restart will have no issues to hydrate from and, if the
                # watermark survives, will delta-sync against a stale baseline.
                logger.warning(
                    "[QUEUE_CACHE] save_snapshot wiping persisted queue: "
                    "prior=%d new=0 watermark=%s repo=%s\nstack:\n%s",
                    prior_count, watermark, repo,
                    "".join(traceback.format_stack(limit=8)),
                )
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
            prior_count = tx.execute("SELECT COUNT(*) FROM queue_issues").fetchone()[0]
            if prior_count > 0:
                logger.warning(
                    "[QUEUE_CACHE] clear() wiping %d persisted queue issues\nstack:\n%s",
                    prior_count,
                    "".join(traceback.format_stack(limit=8)),
                )
            tx.execute("DELETE FROM queue_issues")
            tx.execute("DELETE FROM meta")
