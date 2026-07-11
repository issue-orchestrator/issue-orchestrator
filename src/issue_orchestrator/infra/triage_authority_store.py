"""SQLite adapter for the ``TriageAuthorityStore`` port.

The agent-writable worktree carries copies of the triage assignment and PR
manifest for the *agent* to read; the orchestrator must never treat those
copies as authority (an agent can rewrite them mid-session — #6761 re-review
finding 1). This adapter persists the :class:`TriageLaunchAuthority` recorded
at launch, keyed by session run identity, in the per-repo state directory —
the same orchestrator-owned home as ``queue_cache.sqlite`` /
``label_store.sqlite`` — so it survives restarts. It is constructed ONCE at
the composition root (``entrypoints/bootstrap.py``) and injected behind
``ports/triage_authority.py`` into the launch and completion seams (#6769
finding 2); the database is registered in ``infra/sqlite_registry.py`` for
doctor checks, backups, and startup maintenance (#6769 finding 3).

Why not the existing stores:

* ``QueueCacheStore`` owns the in-scope issue snapshot with replace-all
  semantics (``save_snapshot`` wipes, ``clear()`` resets the warm cache);
  piggybacking launch authority onto its meta table couples two unrelated
  lifecycles and a cache reset would destroy authority mid-session.
* ``JsonSessionStore`` persists best-effort (save errors are swallowed) and
  is reachable only where SessionStore is injected — the completion action
  planner is constructed inside ``completion_handler`` with (config,
  repository_host, label_manager) only, so an injected instance cannot reach
  it; concurrent JSON read-modify-write from launcher + completion would
  also race.
* ``OrchestratorState`` + label recovery is in-memory/label-shaped; labels
  cannot carry a per-run manifest PR set.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from ..domain.triage_session import TriageLaunchAuthority
from ..ports.triage_authority import TriageAuthorityConflictError
from .repo_identity import state_dir
from .sqlite_connection import open_sqlite

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS triage_launch_authority (
    run_id TEXT NOT NULL,
    session_name TEXT NOT NULL,
    authority TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    PRIMARY KEY (run_id, session_name)
);
"""


class SqliteTriageAuthorityStore:
    """Persists per-run triage launch authority across restarts."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._local = threading.local()
        self._write_lock = threading.Lock()
        self.initialize()

    @classmethod
    def for_repo(cls, repo_root: Path) -> "SqliteTriageAuthorityStore":
        """Store handle for a repository's orchestrator state directory.

        Called only by the composition root (and adapter tests); control code
        depends on the injected ``TriageAuthorityStore`` port instead.
        """
        return cls(state_dir(repo_root) / "triage_authority.sqlite")

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

    def record(
        self, *, run_id: str, session_name: str, authority: TriageLaunchAuthority
    ) -> None:
        """Persist the launch authority for one session run (create-once).

        Identical payload for an existing key: no-op. Different payload:
        :class:`TriageAuthorityConflictError` — the scope must never
        silently change after launch (#6769 round 4).
        """
        payload = json.dumps(authority.to_dict(), sort_keys=True)
        with self._transaction() as tx:
            row = tx.execute(
                "SELECT authority FROM triage_launch_authority "
                "WHERE run_id = ? AND session_name = ?",
                (run_id, session_name),
            ).fetchone()
            if row is not None:
                if json.dumps(json.loads(row[0]), sort_keys=True) == payload:
                    return
                raise TriageAuthorityConflictError(
                    f"launch authority already recorded for run_id={run_id!r} "
                    f"session={session_name!r} with a different payload"
                )
            tx.execute(
                "INSERT INTO triage_launch_authority "
                "(run_id, session_name, authority, recorded_at) "
                "VALUES (?, ?, ?, ?)",
                (
                    run_id,
                    session_name,
                    payload,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
        logger.info(
            "[triage] Recorded launch authority: run_id=%s session=%s flavor=%s "
            "focus=%s manifest_prs=%s",
            run_id,
            session_name,
            authority.flavor.value,
            authority.focus_issue_number,
            list(authority.manifest_pr_numbers),
        )

    def load(
        self, *, run_id: str, session_name: str
    ) -> TriageLaunchAuthority | None:
        """Load the launch authority for a session run, or None when absent.

        Malformed stored content raises ValueError loudly — the store is
        orchestrator-owned, so corruption is a bug, never agent input to
        fail-safe around.
        """
        conn = self._get_connection()
        row = conn.execute(
            "SELECT authority FROM triage_launch_authority "
            "WHERE run_id = ? AND session_name = ?",
            (run_id, session_name),
        ).fetchone()
        if row is None:
            return None
        return TriageLaunchAuthority.from_dict(json.loads(row["authority"]))

    def discard(self, *, run_id: str, session_name: str) -> None:
        """Remove a run's authority row (retention owner; no-op if absent).

        Called when the run reaches a terminal state — completion
        finalization, or a launch that failed after recording — so authority
        rows never outlive their session run (#6769 finding 3).
        """
        with self._transaction() as tx:
            deleted = tx.execute(
                "DELETE FROM triage_launch_authority "
                "WHERE run_id = ? AND session_name = ?",
                (run_id, session_name),
            ).rowcount
        if deleted:
            logger.info(
                "[triage] Discarded launch authority: run_id=%s session=%s",
                run_id,
                session_name,
            )
