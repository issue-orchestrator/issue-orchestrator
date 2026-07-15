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

from ..domain.models import DiscoveredFailure
from ..domain.triage_session import (
    StoredTriageOp,
    TriageLaunchAuthority,
    TriageShippedFixSummary,
)
from ..ports.triage_authority import (
    TriageAuthorityConflictError,
    TriageOpConflictError,
    TriagePatternConflictError,
    TriageShippedFixConflictError,
    TriageStormCohortConflictError,
)
from .repo_identity import state_dir
from .sqlite_connection import open_sqlite

logger = logging.getLogger(__name__)


def _cohort_from_payload(payload: str) -> tuple[DiscoveredFailure, ...]:
    """Rehydrate a stored cohort payload into typed failure facts."""
    return tuple(
        DiscoveredFailure.from_dict(item) for item in json.loads(payload)
    )

_SCHEMA = """
CREATE TABLE IF NOT EXISTS triage_launch_authority (
    run_id TEXT NOT NULL,
    session_name TEXT NOT NULL,
    authority TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    PRIMARY KEY (run_id, session_name)
);
CREATE TABLE IF NOT EXISTS triage_proposal_ops (
    issue_number INTEGER PRIMARY KEY,
    op TEXT NOT NULL,
    recorded_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS triage_patterns (
    signature TEXT PRIMARY KEY,
    issue_number INTEGER NOT NULL,
    recorded_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS triage_shipped_fixes (
    issue_number INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    pr_url TEXT NOT NULL,
    area TEXT NOT NULL,
    merged_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS triage_storm_cohorts (
    anchor_issue_number INTEGER PRIMARY KEY,
    cohort TEXT NOT NULL,
    recorded_at TEXT NOT NULL
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
                existing = TriageLaunchAuthority.from_dict(json.loads(row[0]))
                if existing == authority:
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
            "focus=%s manifest_prs=%s problem_issues=%s",
            run_id,
            session_name,
            authority.flavor.value,
            authority.focus_issue_number,
            list(authority.manifest_pr_numbers),
            list(authority.problem_issue_numbers),
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

    # -- Gated proposal ops (#6778, ADR-0031 §2 amendment) -----------------

    def record_op(self, *, issue_number: int, op: StoredTriageOp) -> None:
        """Persist a proposal issue's executable op (create-once).

        Identical payload for an existing key: no-op. Different payload:
        :class:`TriageOpConflictError` — the approver's consent binds to
        exactly one recorded payload, which must never silently change.
        """
        payload = json.dumps(op.to_dict(), sort_keys=True)
        with self._transaction() as tx:
            row = tx.execute(
                "SELECT op FROM triage_proposal_ops WHERE issue_number = ?",
                (issue_number,),
            ).fetchone()
            if row is not None:
                if json.dumps(json.loads(row[0]), sort_keys=True) == payload:
                    return
                raise TriageOpConflictError(
                    f"a different triage op is already recorded for proposal"
                    f" issue #{issue_number}"
                )
            tx.execute(
                "INSERT INTO triage_proposal_ops (issue_number, op, recorded_at)"
                " VALUES (?, ?, ?)",
                (issue_number, payload, datetime.now(timezone.utc).isoformat()),
            )
        logger.info(
            "[triage] Recorded proposal op: issue=#%d op=%s target=#%d action=%s",
            issue_number,
            op.op_type,
            op.target_issue_number,
            op.source_action_id,
        )

    def load_op(self, *, issue_number: int) -> StoredTriageOp | None:
        """Load a proposal issue's op, or None when absent.

        Malformed stored content raises ValueError loudly — the store is
        orchestrator-owned, so corruption is a bug, never agent input.
        """
        conn = self._get_connection()
        row = conn.execute(
            "SELECT op FROM triage_proposal_ops WHERE issue_number = ?",
            (issue_number,),
        ).fetchone()
        if row is None:
            return None
        return StoredTriageOp.from_dict(json.loads(row["op"]))

    def discard_op(self, *, issue_number: int) -> None:
        """Remove a proposal issue's op row (once-only owner; no-op if absent)."""
        with self._transaction() as tx:
            deleted = tx.execute(
                "DELETE FROM triage_proposal_ops WHERE issue_number = ?",
                (issue_number,),
            ).rowcount
        if deleted:
            logger.info("[triage] Discarded proposal op: issue=#%d", issue_number)

    def list_ops(self) -> tuple[tuple[int, StoredTriageOp], ...]:
        """All (proposal_issue_number, op) rows — the open-proposal ledger."""
        conn = self._get_connection()
        rows = conn.execute(
            "SELECT issue_number, op FROM triage_proposal_ops ORDER BY issue_number",
        ).fetchall()
        return tuple(
            (int(row["issue_number"]), StoredTriageOp.from_dict(json.loads(row["op"])))
            for row in rows
        )

    # -- Pattern case files (#6781) -----------------------------------------

    def record_pattern(self, *, signature: str, issue_number: int) -> None:
        """Persist a signature's case-file issue (create-once).

        Same issue for an existing signature: no-op. Different issue:
        :class:`TriagePatternConflictError` — a signature keys exactly one
        evidence trail, which must never silently move.
        """
        with self._transaction() as tx:
            row = tx.execute(
                "SELECT issue_number FROM triage_patterns WHERE signature = ?",
                (signature,),
            ).fetchone()
            if row is not None:
                if int(row[0]) == issue_number:
                    return
                raise TriagePatternConflictError(
                    f"pattern signature {signature!r} is already recorded for"
                    f" case-file issue #{int(row[0])}"
                )
            tx.execute(
                "INSERT INTO triage_patterns (signature, issue_number,"
                " recorded_at) VALUES (?, ?, ?)",
                (
                    signature,
                    issue_number,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
        logger.info(
            "[triage] Recorded pattern case file: signature=%r issue=#%d",
            signature,
            issue_number,
        )

    def lookup_pattern(self, *, signature: str) -> int | None:
        """Return the case-file issue for a signature, or None when absent."""
        conn = self._get_connection()
        row = conn.execute(
            "SELECT issue_number FROM triage_patterns WHERE signature = ?",
            (signature,),
        ).fetchone()
        return int(row["issue_number"]) if row is not None else None

    def list_patterns(self) -> tuple[tuple[str, int], ...]:
        """All (signature, case_file_issue_number) rows — the pattern ledger."""
        conn = self._get_connection()
        rows = conn.execute(
            "SELECT signature, issue_number FROM triage_patterns ORDER BY signature",
        ).fetchall()
        return tuple(
            (str(row["signature"]), int(row["issue_number"])) for row in rows
        )

    # -- Problem-storm cohorts (#6780) ------------------------------------

    def record_storm_cohort(
        self, *, anchor_issue_number: int, cohort: tuple[DiscoveredFailure, ...]
    ) -> None:
        """Persist an anchor's problem cohort (create-once).

        Identical cohort for an existing anchor: no-op. Different cohort:
        :class:`TriageStormCohortConflictError` — the cohort is the health
        review's act-level authority and the retention scope for the members'
        run artifacts, so it must never silently change after creation.
        """
        payload = json.dumps(
            [problem.to_dict() for problem in cohort], sort_keys=True
        )
        with self._transaction() as tx:
            row = tx.execute(
                "SELECT cohort FROM triage_storm_cohorts"
                " WHERE anchor_issue_number = ?",
                (anchor_issue_number,),
            ).fetchone()
            if row is not None:
                if json.dumps(json.loads(row[0]), sort_keys=True) == payload:
                    return
                raise TriageStormCohortConflictError(
                    f"a different storm cohort is already recorded for anchor"
                    f" issue #{anchor_issue_number}"
                )
            tx.execute(
                "INSERT INTO triage_storm_cohorts (anchor_issue_number, cohort,"
                " recorded_at) VALUES (?, ?, ?)",
                (
                    anchor_issue_number,
                    payload,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
        logger.info(
            "[triage] Recorded storm cohort for anchor #%d: %d problem issue(s)",
            anchor_issue_number,
            len(cohort),
        )

    def load_storm_cohort(
        self, *, anchor_issue_number: int
    ) -> tuple[DiscoveredFailure, ...] | None:
        """Return an anchor's persisted cohort, or None when absent."""
        conn = self._get_connection()
        row = conn.execute(
            "SELECT cohort FROM triage_storm_cohorts WHERE anchor_issue_number = ?",
            (anchor_issue_number,),
        ).fetchone()
        if row is None:
            return None
        return _cohort_from_payload(row["cohort"])

    def discard_storm_cohort(self, *, anchor_issue_number: int) -> None:
        """Remove an anchor's cohort row. No-op if absent (retention owner)."""
        with self._transaction() as tx:
            tx.execute(
                "DELETE FROM triage_storm_cohorts WHERE anchor_issue_number = ?",
                (anchor_issue_number,),
            )

    def list_storm_cohorts(
        self,
    ) -> tuple[tuple[int, tuple[DiscoveredFailure, ...]], ...]:
        """All (anchor_issue_number, cohort) rows — the cleanup-hold read."""
        conn = self._get_connection()
        rows = conn.execute(
            "SELECT anchor_issue_number, cohort FROM triage_storm_cohorts"
            " ORDER BY anchor_issue_number",
        ).fetchall()
        return tuple(
            (int(row["anchor_issue_number"]), _cohort_from_payload(row["cohort"]))
            for row in rows
        )

    # -- Shipped-fix operational memory (#6781 amendment) -----------------

    def record_shipped_fix(
        self, *, issue_number: int, title: str, pr_url: str, area: str
    ) -> None:
        """Persist an area-tagged merged fix (create-once by issue)."""
        with self._transaction() as tx:
            row = tx.execute(
                "SELECT pr_url, area FROM triage_shipped_fixes "
                "WHERE issue_number = ?",
                (issue_number,),
            ).fetchone()
            if row is not None:
                if (
                    str(row["pr_url"]) == pr_url
                    and str(row["area"]) == area
                ):
                    return
                raise TriageShippedFixConflictError(
                    f"different shipped-fix evidence is already recorded for"
                    f" issue #{issue_number}"
                )
            tx.execute(
                "INSERT INTO triage_shipped_fixes "
                "(issue_number, title, pr_url, area, merged_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    issue_number,
                    title,
                    pr_url,
                    area,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
        logger.info(
            "[triage] Recorded shipped fix: issue=#%d area=%r pr=%s",
            issue_number,
            area,
            pr_url,
        )

    def list_recent_shipped_fixes(
        self, *, limit: int
    ) -> tuple[TriageShippedFixSummary, ...]:
        """Return the newest durable shipped-fix facts."""
        if limit <= 0:
            raise ValueError("shipped-fix limit must be positive")
        rows = self._get_connection().execute(
            "SELECT issue_number, title, pr_url, area, merged_at "
            "FROM triage_shipped_fixes "
            "ORDER BY merged_at DESC, issue_number DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return tuple(
            TriageShippedFixSummary(
                issue_number=int(row["issue_number"]),
                title=str(row["title"]),
                pr_url=str(row["pr_url"]),
                area=str(row["area"]),
                merged_at=str(row["merged_at"]),
            )
            for row in rows
        )
