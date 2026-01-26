"""E2E test results database - SQLite-based persistence.

Stores E2E test run results and per-test outcomes for dashboard visibility.
"""

import json
import logging
import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

logger = logging.getLogger(__name__)


# SQLite schema for e2e results
_SCHEMA = """
CREATE TABLE IF NOT EXISTS e2e_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_root TEXT NOT NULL,
    orchestrator_id TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    exit_code INTEGER,
    pytest_args TEXT NOT NULL,
    commit_sha TEXT,
    branch TEXT,
    retry_of INTEGER,
    is_retry_run INTEGER DEFAULT 0,
    duration_seconds REAL,
    note TEXT,
    log_path TEXT,
    artifacts_dir TEXT,
    worker_pid INTEGER,
    total_tests INTEGER,
    current_test TEXT
);

CREATE TABLE IF NOT EXISTS e2e_test_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    nodeid TEXT NOT NULL,
    outcome TEXT NOT NULL,
    duration_seconds REAL,
    longrepr TEXT,
    retry_outcome TEXT,
    is_quarantined INTEGER DEFAULT 0,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(run_id) REFERENCES e2e_runs(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_e2e_runs_orch_started
    ON e2e_runs(orchestrator_id, started_at DESC);

CREATE INDEX IF NOT EXISTS idx_e2e_test_results_run
    ON e2e_test_results(run_id, outcome);

CREATE UNIQUE INDEX IF NOT EXISTS idx_e2e_test_results_run_nodeid
    ON e2e_test_results(run_id, nodeid);

-- E2E Issue Tracking: Links test failures to GitHub issues
CREATE TABLE IF NOT EXISTS e2e_failure_issues (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    nodeid TEXT NOT NULL,
    github_issue_number INTEGER NOT NULL,
    parent_issue_number INTEGER NOT NULL,
    first_failing_run_id INTEGER NOT NULL,
    first_failing_sha TEXT NOT NULL,
    last_passing_sha TEXT,
    resolved_at TEXT,
    resolution TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY(first_failing_run_id) REFERENCES e2e_runs(id) ON DELETE CASCADE
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_e2e_failure_issues_nodeid_sha
    ON e2e_failure_issues(nodeid, first_failing_sha);

CREATE INDEX IF NOT EXISTS idx_e2e_failure_issues_parent
    ON e2e_failure_issues(parent_issue_number);

-- E2E Issue Tracking: Tracks E2E run parent issues
CREATE TABLE IF NOT EXISTS e2e_run_issues (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    github_issue_number INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    closed_at TEXT,
    FOREIGN KEY(run_id) REFERENCES e2e_runs(id) ON DELETE CASCADE
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_e2e_run_issues_run
    ON e2e_run_issues(run_id);

-- E2E Flakiness Tracking: Records flaky test occurrences
CREATE TABLE IF NOT EXISTS e2e_flake_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    nodeid TEXT NOT NULL,
    run_id INTEGER NOT NULL,
    was_flaky INTEGER NOT NULL,
    recorded_at TEXT NOT NULL,
    FOREIGN KEY(run_id) REFERENCES e2e_runs(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_e2e_flake_history_nodeid
    ON e2e_flake_history(nodeid, recorded_at DESC);
"""


@dataclass
class E2ERun:
    """A single E2E test run."""

    id: int
    repo_root: str
    orchestrator_id: str
    started_at: str
    finished_at: Optional[str]
    status: str
    exit_code: Optional[int]
    pytest_args: list[str]
    commit_sha: Optional[str]
    branch: Optional[str]
    retry_of: Optional[int]
    is_retry_run: bool
    duration_seconds: Optional[float]
    note: Optional[str]
    log_path: Optional[str]
    artifacts_dir: Optional[str]
    worker_pid: Optional[int]
    total_tests: Optional[int]
    current_test: Optional[str]

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "E2ERun":
        return cls(
            id=row["id"],
            repo_root=row["repo_root"],
            orchestrator_id=row["orchestrator_id"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            status=row["status"],
            exit_code=row["exit_code"],
            pytest_args=json.loads(row["pytest_args"]),
            commit_sha=row["commit_sha"],
            branch=row["branch"],
            retry_of=row["retry_of"],
            is_retry_run=bool(row["is_retry_run"]),
            duration_seconds=row["duration_seconds"],
            note=row["note"],
            log_path=row["log_path"],
            artifacts_dir=row["artifacts_dir"],
            worker_pid=row["worker_pid"],
            total_tests=row["total_tests"],
            current_test=row["current_test"],
        )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "repo_root": self.repo_root,
            "orchestrator_id": self.orchestrator_id,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "status": self.status,
            "exit_code": self.exit_code,
            "pytest_args": self.pytest_args,
            "commit_sha": self.commit_sha,
            "branch": self.branch,
            "retry_of": self.retry_of,
            "is_retry_run": self.is_retry_run,
            "duration_seconds": self.duration_seconds,
            "note": self.note,
            "log_path": self.log_path,
            "artifacts_dir": self.artifacts_dir,
            "worker_pid": self.worker_pid,
            "total_tests": self.total_tests,
            "current_test": self.current_test,
        }


@dataclass
class E2ETestResult:
    """A single test result within a run."""

    id: int
    run_id: int
    nodeid: str
    outcome: str
    duration_seconds: Optional[float]
    longrepr: Optional[str]
    retry_outcome: Optional[str]
    is_quarantined: bool
    updated_at: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "E2ETestResult":
        return cls(
            id=row["id"],
            run_id=row["run_id"],
            nodeid=row["nodeid"],
            outcome=row["outcome"],
            duration_seconds=row["duration_seconds"],
            longrepr=row["longrepr"],
            retry_outcome=row["retry_outcome"],
            is_quarantined=bool(row["is_quarantined"]),
            updated_at=row["updated_at"],
        )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "run_id": self.run_id,
            "nodeid": self.nodeid,
            "outcome": self.outcome,
            "duration_seconds": self.duration_seconds,
            "longrepr": self.longrepr,
            "retry_outcome": self.retry_outcome,
            "is_quarantined": self.is_quarantined,
            "updated_at": self.updated_at,
        }


@dataclass
class E2EFailureIssue:
    """Links a test failure to a GitHub sub-issue."""

    id: int
    nodeid: str
    github_issue_number: int
    parent_issue_number: int
    first_failing_run_id: int
    first_failing_sha: str
    last_passing_sha: Optional[str]
    resolved_at: Optional[str]
    resolution: Optional[str]  # 'passed', 'quarantined', 'manual'
    created_at: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "E2EFailureIssue":
        return cls(
            id=row["id"],
            nodeid=row["nodeid"],
            github_issue_number=row["github_issue_number"],
            parent_issue_number=row["parent_issue_number"],
            first_failing_run_id=row["first_failing_run_id"],
            first_failing_sha=row["first_failing_sha"],
            last_passing_sha=row["last_passing_sha"],
            resolved_at=row["resolved_at"],
            resolution=row["resolution"],
            created_at=row["created_at"],
        )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "nodeid": self.nodeid,
            "github_issue_number": self.github_issue_number,
            "parent_issue_number": self.parent_issue_number,
            "first_failing_run_id": self.first_failing_run_id,
            "first_failing_sha": self.first_failing_sha,
            "last_passing_sha": self.last_passing_sha,
            "resolved_at": self.resolved_at,
            "resolution": self.resolution,
            "created_at": self.created_at,
        }


@dataclass
class E2ERunIssue:
    """Links an E2E run to its parent GitHub issue."""

    id: int
    run_id: int
    github_issue_number: int
    created_at: str
    closed_at: Optional[str]

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "E2ERunIssue":
        return cls(
            id=row["id"],
            run_id=row["run_id"],
            github_issue_number=row["github_issue_number"],
            created_at=row["created_at"],
            closed_at=row["closed_at"],
        )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "run_id": self.run_id,
            "github_issue_number": self.github_issue_number,
            "created_at": self.created_at,
            "closed_at": self.closed_at,
        }


@dataclass
class E2EFlakeRecord:
    """Records a flaky test occurrence."""

    id: int
    nodeid: str
    run_id: int
    was_flaky: bool
    recorded_at: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "E2EFlakeRecord":
        return cls(
            id=row["id"],
            nodeid=row["nodeid"],
            run_id=row["run_id"],
            was_flaky=bool(row["was_flaky"]),
            recorded_at=row["recorded_at"],
        )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "nodeid": self.nodeid,
            "run_id": self.run_id,
            "was_flaky": self.was_flaky,
            "recorded_at": self.recorded_at,
        }


class AlreadyRunning(Exception):
    """Raised when attempting to start a run while one is already running."""

    def __init__(self, orchestrator_id: str, existing_run_id: int):
        self.orchestrator_id = orchestrator_id
        self.existing_run_id = existing_run_id
        super().__init__(
            f"E2E run already in progress for {orchestrator_id} (run_id={existing_run_id})"
        )


class E2EDB:
    """SQLite database for E2E test results.

    Thread-safe through connection-per-operation pattern.
    """

    def __init__(self, db_path: Path):
        """Initialize E2E database.

        Args:
            db_path: Path to SQLite database file. Created if doesn't exist.
        """
        self.db_path = db_path
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        """Create tables if they don't exist."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """Get a database connection with row factory."""
        conn = sqlite3.connect(str(self.db_path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _now_iso(self) -> str:
        """Return current UTC time as ISO string."""
        return datetime.now(timezone.utc).isoformat()

    # -------------------------------------------------------------------------
    # Run lifecycle
    # -------------------------------------------------------------------------

    def start_run(
        self,
        repo_root: str,
        orchestrator_id: str,
        pytest_args: list[str],
        commit_sha: Optional[str] = None,
        branch: Optional[str] = None,
        retry_of: Optional[int] = None,
        worker_pid: Optional[int] = None,
    ) -> int:
        """Start a new E2E run.

        Enforces single running run per orchestrator_id. If an existing run
        has a dead worker process, it's marked as 'interrupted' (resumable).

        Args:
            repo_root: Path to repo root
            orchestrator_id: Unique orchestrator identifier
            pytest_args: Arguments to pass to pytest
            commit_sha: Git commit SHA (optional)
            branch: Git branch name (optional)
            retry_of: If this is a retry, the original run_id
            worker_pid: PID of the worker process (for orphan detection)

        Returns:
            The new run's ID

        Raises:
            AlreadyRunning: If a run is already in progress for this orchestrator
        """
        with self._connect() as conn:
            # Check for existing running run
            cursor = conn.execute(
                """
                SELECT id, worker_pid FROM e2e_runs
                WHERE orchestrator_id = ? AND status = 'running'
                LIMIT 1
                """,
                (orchestrator_id,),
            )
            row = cursor.fetchone()
            if row:
                existing_pid = row["worker_pid"]
                # Check if the worker process is still alive
                if existing_pid and not self._is_process_alive(existing_pid):
                    # Orphaned run - mark as interrupted (resumable)
                    conn.execute(
                        """
                        UPDATE e2e_runs SET
                            status = 'interrupted',
                            finished_at = ?,
                            note = ?
                        WHERE id = ?
                        """,
                        (self._now_iso(), f"Worker process died (PID {existing_pid})", row["id"]),
                    )
                    logger.warning(
                        "Marked orphaned E2E run %d as interrupted (PID %s dead)",
                        row["id"],
                        existing_pid,
                    )
                else:
                    raise AlreadyRunning(orchestrator_id, row["id"])

            # Create new run
            cursor = conn.execute(
                """
                INSERT INTO e2e_runs (
                    repo_root, orchestrator_id, started_at, status,
                    pytest_args, commit_sha, branch, retry_of, is_retry_run,
                    worker_pid
                ) VALUES (?, ?, ?, 'running', ?, ?, ?, ?, ?, ?)
                """,
                (
                    repo_root,
                    orchestrator_id,
                    self._now_iso(),
                    json.dumps(pytest_args),
                    commit_sha,
                    branch,
                    retry_of,
                    1 if retry_of else 0,
                    worker_pid,
                ),
            )
            run_id = cursor.lastrowid
            assert run_id is not None, "INSERT failed to return lastrowid"
            logger.info(
                "Started E2E run %d for %s (commit=%s, branch=%s, pid=%s)",
                run_id,
                orchestrator_id,
                commit_sha,
                branch,
                worker_pid,
            )
            return run_id

    def _is_process_alive(self, pid: int) -> bool:
        """Check if a process is still running."""
        try:
            os.kill(pid, 0)  # Signal 0 doesn't kill, just checks
            return True
        except OSError:
            return False

    def update_worker_pid(self, run_id: int, worker_pid: int) -> None:
        """Update the worker PID for a run (called after subprocess starts)."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE e2e_runs SET worker_pid = ? WHERE id = ?",
                (worker_pid, run_id),
            )

    def update_progress(
        self,
        run_id: int,
        total_tests: Optional[int] = None,
        current_test: Optional[str] = None,
    ) -> None:
        """Update progress info for a running test.

        Args:
            run_id: The run to update
            total_tests: Total tests collected (set once after collection)
            current_test: Currently executing test nodeid
        """
        with self._connect() as conn:
            if total_tests is not None and current_test is not None:
                conn.execute(
                    "UPDATE e2e_runs SET total_tests = ?, current_test = ? WHERE id = ?",
                    (total_tests, current_test, run_id),
                )
            elif total_tests is not None:
                conn.execute(
                    "UPDATE e2e_runs SET total_tests = ? WHERE id = ?",
                    (total_tests, run_id),
                )
            elif current_test is not None:
                conn.execute(
                    "UPDATE e2e_runs SET current_test = ? WHERE id = ?",
                    (current_test, run_id),
                )

    def get_progress(self, run_id: int) -> dict:
        """Get progress stats for a run.

        Returns:
            Dict with total_tests, completed, passed, failed, skipped, current_test
        """
        with self._connect() as conn:
            # Get run info
            cursor = conn.execute(
                "SELECT total_tests, current_test FROM e2e_runs WHERE id = ?",
                (run_id,),
            )
            row = cursor.fetchone()
            if not row:
                return {}

            total_tests = row["total_tests"]
            current_test = row["current_test"]

            # Count results by outcome
            cursor = conn.execute(
                """
                SELECT outcome, COUNT(*) as cnt
                FROM e2e_test_results
                WHERE run_id = ?
                GROUP BY outcome
                """,
                (run_id,),
            )
            counts = {r["outcome"]: r["cnt"] for r in cursor.fetchall()}

            passed = counts.get("passed", 0)
            failed = counts.get("failed", 0)
            skipped = counts.get("skipped", 0)
            error = counts.get("error", 0)
            completed = passed + failed + skipped + error

            return {
                "total_tests": total_tests,
                "completed": completed,
                "passed": passed,
                "failed": failed,
                "skipped": skipped,
                "error": error,
                "current_test": current_test,
                "percent": round(completed / total_tests * 100) if total_tests else None,
            }

    def finish_run(
        self,
        run_id: int,
        status: str,
        exit_code: Optional[int] = None,
        duration_seconds: Optional[float] = None,
        log_path: Optional[str] = None,
        artifacts_dir: Optional[str] = None,
        note: Optional[str] = None,
    ) -> None:
        """Mark a run as finished.

        Args:
            run_id: The run to finish
            status: Final status (passed, failed, canceled, error)
            exit_code: Pytest exit code
            duration_seconds: Total run duration
            log_path: Path to log file
            artifacts_dir: Path to artifacts directory
            note: Optional note
        """
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE e2e_runs SET
                    finished_at = ?,
                    status = ?,
                    exit_code = ?,
                    duration_seconds = ?,
                    log_path = ?,
                    artifacts_dir = ?,
                    note = ?
                WHERE id = ?
                """,
                (
                    self._now_iso(),
                    status,
                    exit_code,
                    duration_seconds,
                    log_path,
                    artifacts_dir,
                    note,
                    run_id,
                ),
            )
            logger.info("Finished E2E run %d with status=%s", run_id, status)

    def cancel_running(self, orchestrator_id: str) -> Optional[int]:
        """Cancel any running run for an orchestrator.

        Returns:
            The canceled run's ID, or None if no running run
        """
        with self._connect() as conn:
            cursor = conn.execute(
                """
                SELECT id FROM e2e_runs
                WHERE orchestrator_id = ? AND status = 'running'
                LIMIT 1
                """,
                (orchestrator_id,),
            )
            row = cursor.fetchone()
            if not row:
                return None

            run_id = row["id"]
            conn.execute(
                """
                UPDATE e2e_runs SET
                    finished_at = ?,
                    status = 'canceled',
                    note = 'Canceled by user'
                WHERE id = ?
                """,
                (self._now_iso(), run_id),
            )
            logger.info("Canceled E2E run %d for %s", run_id, orchestrator_id)
            return run_id

    # -------------------------------------------------------------------------
    # Resume support
    # -------------------------------------------------------------------------

    def get_interrupted_run(self, orchestrator_id: str) -> Optional[E2ERun]:
        """Get the most recent interrupted run that can be resumed."""
        with self._connect() as conn:
            cursor = conn.execute(
                """
                SELECT * FROM e2e_runs
                WHERE orchestrator_id = ? AND status = 'interrupted'
                ORDER BY started_at DESC
                LIMIT 1
                """,
                (orchestrator_id,),
            )
            row = cursor.fetchone()
            return E2ERun.from_row(row) if row else None

    def get_passed_nodeids(self, run_id: int) -> set[str]:
        """Get nodeids that passed in a run (for resume - skip these tests)."""
        with self._connect() as conn:
            cursor = conn.execute(
                """
                SELECT nodeid FROM e2e_test_results
                WHERE run_id = ? AND outcome = 'passed'
                """,
                (run_id,),
            )
            return {row["nodeid"] for row in cursor.fetchall()}

    def resume_run(self, run_id: int, worker_pid: int) -> bool:
        """Resume an interrupted run.

        Args:
            run_id: The interrupted run to resume
            worker_pid: PID of the new worker process

        Returns:
            True if resumed successfully, False if run wasn't interrupted
        """
        with self._connect() as conn:
            cursor = conn.execute(
                "SELECT status FROM e2e_runs WHERE id = ?",
                (run_id,),
            )
            row = cursor.fetchone()
            if not row or row["status"] != "interrupted":
                return False

            conn.execute(
                """
                UPDATE e2e_runs SET
                    status = 'running',
                    worker_pid = ?,
                    note = COALESCE(note, '') || ' | Resumed'
                WHERE id = ?
                """,
                (worker_pid, run_id),
            )
            logger.info("Resumed E2E run %d with worker PID %d", run_id, worker_pid)
            return True

    # -------------------------------------------------------------------------
    # Test results
    # -------------------------------------------------------------------------

    def upsert_test_result(
        self,
        run_id: int,
        nodeid: str,
        outcome: str,
        duration_seconds: Optional[float] = None,
        longrepr: Optional[str] = None,
        retry_outcome: Optional[str] = None,
        is_quarantined: bool = False,
    ) -> None:
        """Insert or update a test result.

        Uses UPSERT to handle retries updating the same nodeid.
        """
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO e2e_test_results (
                    run_id, nodeid, outcome, duration_seconds,
                    longrepr, retry_outcome, is_quarantined, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id, nodeid) DO UPDATE SET
                    outcome = excluded.outcome,
                    duration_seconds = excluded.duration_seconds,
                    longrepr = excluded.longrepr,
                    retry_outcome = excluded.retry_outcome,
                    updated_at = excluded.updated_at
                """,
                (
                    run_id,
                    nodeid,
                    outcome,
                    duration_seconds,
                    longrepr,
                    retry_outcome,
                    1 if is_quarantined else 0,
                    self._now_iso(),
                ),
            )

    def update_retry_outcome(
        self, run_id: int, nodeid: str, retry_outcome: str
    ) -> None:
        """Update just the retry outcome for a test."""
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE e2e_test_results SET
                    retry_outcome = ?,
                    updated_at = ?
                WHERE run_id = ? AND nodeid = ?
                """,
                (retry_outcome, self._now_iso(), run_id, nodeid),
            )

    # -------------------------------------------------------------------------
    # Queries
    # -------------------------------------------------------------------------

    def latest_run(self, orchestrator_id: str) -> Optional[E2ERun]:
        """Get the most recent run for an orchestrator."""
        with self._connect() as conn:
            cursor = conn.execute(
                """
                SELECT * FROM e2e_runs
                WHERE orchestrator_id = ?
                ORDER BY started_at DESC
                LIMIT 1
                """,
                (orchestrator_id,),
            )
            row = cursor.fetchone()
            return E2ERun.from_row(row) if row else None

    def get_running(self, orchestrator_id: str) -> Optional[E2ERun]:
        """Get the currently running run for an orchestrator, if any."""
        with self._connect() as conn:
            cursor = conn.execute(
                """
                SELECT * FROM e2e_runs
                WHERE orchestrator_id = ? AND status = 'running'
                LIMIT 1
                """,
                (orchestrator_id,),
            )
            row = cursor.fetchone()
            return E2ERun.from_row(row) if row else None

    def list_runs(self, orchestrator_id: str, limit: int = 20) -> list[E2ERun]:
        """List recent runs for an orchestrator."""
        with self._connect() as conn:
            cursor = conn.execute(
                """
                SELECT * FROM e2e_runs
                WHERE orchestrator_id = ?
                ORDER BY started_at DESC
                LIMIT ?
                """,
                (orchestrator_id, limit),
            )
            return [E2ERun.from_row(row) for row in cursor.fetchall()]

    def get_run(self, run_id: int) -> Optional[E2ERun]:
        """Get a run by ID.

        Args:
            run_id: Run ID

        Returns:
            E2ERun or None if not found
        """
        with self._connect() as conn:
            cursor = conn.execute(
                "SELECT * FROM e2e_runs WHERE id = ?", (run_id,)
            )
            row = cursor.fetchone()
            return E2ERun.from_row(row) if row else None

    def run_details(self, run_id: int) -> Optional[dict]:
        """Get a run with its test results.

        Returns:
            Dict with 'run' and 'results' keys, or None if not found
        """
        with self._connect() as conn:
            cursor = conn.execute(
                "SELECT * FROM e2e_runs WHERE id = ?", (run_id,)
            )
            row = cursor.fetchone()
            if not row:
                return None

            run = E2ERun.from_row(row)

            cursor = conn.execute(
                """
                SELECT * FROM e2e_test_results
                WHERE run_id = ?
                ORDER BY nodeid
                """,
                (run_id,),
            )
            results = [E2ETestResult.from_row(r) for r in cursor.fetchall()]

            return {
                "run": run.to_dict(),
                "results": [r.to_dict() for r in results],
            }

    def get_failed_tests(self, run_id: int) -> list[E2ETestResult]:
        """Get just the failed tests from a run (excluding quarantined)."""
        with self._connect() as conn:
            cursor = conn.execute(
                """
                SELECT * FROM e2e_test_results
                WHERE run_id = ?
                    AND outcome = 'failed'
                    AND is_quarantined = 0
                    AND (retry_outcome IS NULL OR retry_outcome = 'failed')
                ORDER BY nodeid
                """,
                (run_id,),
            )
            return [E2ETestResult.from_row(row) for row in cursor.fetchall()]

    def get_test_result(self, run_id: int, nodeid: str) -> E2ETestResult | None:
        """Get a specific test result by run ID and nodeid."""
        with self._connect() as conn:
            cursor = conn.execute(
                "SELECT * FROM e2e_test_results WHERE run_id = ? AND nodeid = ?",
                (run_id, nodeid),
            )
            row = cursor.fetchone()
            return E2ETestResult.from_row(row) if row else None

    def get_test_history(self, nodeid: str, limit: int = 10) -> list[dict]:
        """Get historical results for a specific test across recent runs.

        Returns list of dicts with run_id, outcome, started_at for display.
        """
        with self._connect() as conn:
            cursor = conn.execute(
                """
                SELECT
                    r.id AS run_id,
                    t.outcome,
                    t.retry_outcome,
                    r.started_at
                FROM e2e_test_results t
                JOIN e2e_runs r ON t.run_id = r.id
                WHERE t.nodeid = ?
                ORDER BY r.started_at DESC
                LIMIT ?
                """,
                (nodeid, limit),
            )
            results = []
            for row in cursor.fetchall():
                # Determine effective outcome (retry_outcome takes precedence)
                outcome = row["retry_outcome"] or row["outcome"]
                results.append({
                    "run_id": row["run_id"],
                    "outcome": outcome,
                    "started_at": row["started_at"],
                })
            return results

    def get_test_summary(self, run_id: int) -> dict:
        """Get comprehensive test summary for a run.

        Returns:
            Dict with:
                - passed: tests that passed (or passed on retry)
                - failed: tests that failed (even after retry, non-quarantined)
                - passed_on_retry: tests that failed first but passed on retry
                - quarantined: quarantined tests (excluded from failure count)
                - skipped: skipped tests
                - counts: summary counts
        """
        with self._connect() as conn:
            cursor = conn.execute(
                "SELECT * FROM e2e_test_results WHERE run_id = ? ORDER BY nodeid",
                (run_id,),
            )
            results = [E2ETestResult.from_row(row) for row in cursor.fetchall()]

        passed = []
        failed = []
        passed_on_retry = []
        quarantined = []
        skipped = []

        for r in results:
            if r.is_quarantined:
                quarantined.append(r)
            elif r.outcome == "skipped":
                skipped.append(r)
            elif r.outcome == "passed":
                passed.append(r)
            elif r.outcome == "failed":
                if r.retry_outcome == "passed":
                    passed_on_retry.append(r)
                else:
                    failed.append(r)

        return {
            "passed": [t.to_dict() for t in passed],
            "failed": [t.to_dict() for t in failed],
            "passed_on_retry": [t.to_dict() for t in passed_on_retry],
            "quarantined": [t.to_dict() for t in quarantined],
            "skipped": [t.to_dict() for t in skipped],
            "counts": {
                "total": len(results),
                "passed": len(passed),
                "failed": len(failed),
                "passed_on_retry": len(passed_on_retry),
                "quarantined": len(quarantined),
                "skipped": len(skipped),
            },
        }

    # -------------------------------------------------------------------------
    # Signal score
    # -------------------------------------------------------------------------

    def compute_signal_score(
        self, orchestrator_id: str, last_n_runs: int = 30
    ) -> dict:
        """Compute stability metrics for an orchestrator.

        Returns:
            Dict with pass_rate, runs_analyzed, quarantined_count
        """
        with self._connect() as conn:
            # Get last N completed runs
            cursor = conn.execute(
                """
                SELECT id, status FROM e2e_runs
                WHERE orchestrator_id = ? AND status != 'running'
                ORDER BY started_at DESC
                LIMIT ?
                """,
                (orchestrator_id, last_n_runs),
            )
            runs = cursor.fetchall()

            if not runs:
                return {
                    "pass_rate": None,
                    "runs_analyzed": 0,
                    "quarantined_count": 0,
                }

            passed = sum(1 for r in runs if r["status"] == "passed")
            pass_rate = passed / len(runs)

            # Count quarantined tests from most recent run
            latest_run_id = runs[0]["id"] if runs else None
            quarantined_count = 0
            if latest_run_id:
                cursor = conn.execute(
                    """
                    SELECT COUNT(*) as cnt FROM e2e_test_results
                    WHERE run_id = ? AND is_quarantined = 1
                    """,
                    (latest_run_id,),
                )
                quarantined_count = cursor.fetchone()["cnt"]

            return {
                "pass_rate": pass_rate,
                "runs_analyzed": len(runs),
                "quarantined_count": quarantined_count,
            }

    # -------------------------------------------------------------------------
    # E2E Issue Tracking Methods
    # -------------------------------------------------------------------------

    def record_run_issue(
        self,
        run_id: int,
        github_issue_number: int,
    ) -> int:
        """Record a GitHub parent issue for an E2E run.

        Args:
            run_id: E2E run ID
            github_issue_number: GitHub issue number for the parent issue

        Returns:
            ID of the created record
        """
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO e2e_run_issues (run_id, github_issue_number, created_at)
                VALUES (?, ?, ?)
                """,
                (run_id, github_issue_number, self._now_iso()),
            )
            return cursor.lastrowid or 0

    def get_run_issue(self, run_id: int) -> Optional[E2ERunIssue]:
        """Get the GitHub issue for an E2E run.

        Args:
            run_id: E2E run ID

        Returns:
            E2ERunIssue or None if no issue exists
        """
        with self._connect() as conn:
            cursor = conn.execute(
                "SELECT * FROM e2e_run_issues WHERE run_id = ?",
                (run_id,),
            )
            row = cursor.fetchone()
            return E2ERunIssue.from_row(row) if row else None

    def close_run_issue(self, run_id: int) -> bool:
        """Mark a run's GitHub issue as closed.

        Args:
            run_id: E2E run ID

        Returns:
            True if updated, False if no issue existed
        """
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE e2e_run_issues SET closed_at = ?
                WHERE run_id = ? AND closed_at IS NULL
                """,
                (self._now_iso(), run_id),
            )
            return cursor.rowcount > 0

    def get_open_run_issues(self) -> list[E2ERunIssue]:
        """Get all open (not closed) E2E run issues.

        Returns:
            List of E2ERunIssue records where closed_at IS NULL
        """
        with self._connect() as conn:
            cursor = conn.execute(
                "SELECT * FROM e2e_run_issues WHERE closed_at IS NULL ORDER BY created_at DESC",
            )
            return [E2ERunIssue.from_row(row) for row in cursor.fetchall()]

    def record_failure_issue(
        self,
        nodeid: str,
        github_issue_number: int,
        parent_issue_number: int,
        first_failing_run_id: int,
        first_failing_sha: str,
        last_passing_sha: Optional[str] = None,
    ) -> int:
        """Record a GitHub sub-issue for a test failure.

        Args:
            nodeid: Test node ID (e.g., tests/e2e/test_foo.py::test_bar)
            github_issue_number: GitHub issue number for the sub-issue
            parent_issue_number: Parent issue number
            first_failing_run_id: Run ID where failure was first detected
            first_failing_sha: Commit SHA where failure was first detected
            last_passing_sha: Last known passing commit SHA

        Returns:
            ID of the created record
        """
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO e2e_failure_issues
                (nodeid, github_issue_number, parent_issue_number,
                 first_failing_run_id, first_failing_sha, last_passing_sha, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    nodeid,
                    github_issue_number,
                    parent_issue_number,
                    first_failing_run_id,
                    first_failing_sha,
                    last_passing_sha,
                    self._now_iso(),
                ),
            )
            return cursor.lastrowid or 0

    def find_open_failure_issue(
        self,
        nodeid: str,
    ) -> Optional[E2EFailureIssue]:
        """Find an open GitHub issue for a test failure.

        Args:
            nodeid: Test node ID

        Returns:
            E2EFailureIssue or None if no open issue exists
        """
        with self._connect() as conn:
            cursor = conn.execute(
                """
                SELECT * FROM e2e_failure_issues
                WHERE nodeid = ? AND resolved_at IS NULL
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (nodeid,),
            )
            row = cursor.fetchone()
            return E2EFailureIssue.from_row(row) if row else None

    def resolve_failure_issue(
        self,
        nodeid: str,
        resolution: str,
    ) -> bool:
        """Mark a failure issue as resolved.

        Args:
            nodeid: Test node ID
            resolution: Resolution type ('passed', 'quarantined', 'manual')

        Returns:
            True if updated, False if no open issue existed
        """
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE e2e_failure_issues
                SET resolved_at = ?, resolution = ?
                WHERE nodeid = ? AND resolved_at IS NULL
                """,
                (self._now_iso(), resolution, nodeid),
            )
            return cursor.rowcount > 0

    def get_failure_issues_for_parent(
        self,
        parent_issue_number: int,
    ) -> list[E2EFailureIssue]:
        """Get all failure sub-issues for a parent issue.

        Args:
            parent_issue_number: Parent GitHub issue number

        Returns:
            List of E2EFailureIssue records
        """
        with self._connect() as conn:
            cursor = conn.execute(
                """
                SELECT * FROM e2e_failure_issues
                WHERE parent_issue_number = ?
                ORDER BY nodeid
                """,
                (parent_issue_number,),
            )
            return [E2EFailureIssue.from_row(row) for row in cursor.fetchall()]

    def get_unresolved_failure_count(
        self,
        parent_issue_number: int,
    ) -> int:
        """Count unresolved failure issues for a parent.

        Args:
            parent_issue_number: Parent GitHub issue number

        Returns:
            Count of unresolved failure issues
        """
        with self._connect() as conn:
            cursor = conn.execute(
                """
                SELECT COUNT(*) as cnt FROM e2e_failure_issues
                WHERE parent_issue_number = ? AND resolved_at IS NULL
                """,
                (parent_issue_number,),
            )
            return cursor.fetchone()["cnt"]

    def get_all_open_failure_issues(self) -> list[E2EFailureIssue]:
        """Get all unresolved failure issues across all runs.

        Returns:
            List of E2EFailureIssue records where resolved_at IS NULL
        """
        with self._connect() as conn:
            cursor = conn.execute(
                """
                SELECT * FROM e2e_failure_issues
                WHERE resolved_at IS NULL
                ORDER BY nodeid
                """,
            )
            return [E2EFailureIssue.from_row(row) for row in cursor.fetchall()]

    # -------------------------------------------------------------------------
    # Flakiness Tracking Methods
    # -------------------------------------------------------------------------

    def record_flake(
        self,
        nodeid: str,
        run_id: int,
        was_flaky: bool,
    ) -> int:
        """Record a flaky test occurrence.

        Args:
            nodeid: Test node ID
            run_id: E2E run ID
            was_flaky: Whether the test was flaky (failed then passed on retry)

        Returns:
            ID of the created record
        """
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO e2e_flake_history (nodeid, run_id, was_flaky, recorded_at)
                VALUES (?, ?, ?, ?)
                """,
                (nodeid, run_id, int(was_flaky), self._now_iso()),
            )
            return cursor.lastrowid or 0

    def get_flake_count(
        self,
        nodeid: str,
        window_runs: int = 10,
    ) -> int:
        """Count consecutive flaky occurrences for a test.

        Args:
            nodeid: Test node ID
            window_runs: Number of recent runs to check

        Returns:
            Count of flaky occurrences in the window
        """
        with self._connect() as conn:
            cursor = conn.execute(
                """
                SELECT COUNT(*) as cnt FROM (
                    SELECT was_flaky FROM e2e_flake_history
                    WHERE nodeid = ?
                    ORDER BY recorded_at DESC
                    LIMIT ?
                )
                WHERE was_flaky = 1
                """,
                (nodeid, window_runs),
            )
            return cursor.fetchone()["cnt"]

    def get_flaky_tests(
        self,
        threshold: int = 3,
        window_runs: int = 10,
    ) -> list[dict]:
        """Get tests that exceed the flakiness threshold.

        Args:
            threshold: Number of flakes to consider a test as problematic
            window_runs: Number of recent runs to check

        Returns:
            List of dicts with nodeid and flake_count
        """
        with self._connect() as conn:
            # Get unique nodeids that have flake history
            cursor = conn.execute(
                "SELECT DISTINCT nodeid FROM e2e_flake_history"
            )
            nodeids = [row["nodeid"] for row in cursor.fetchall()]

        # Check each one (could be optimized with window functions if needed)
        result = []
        for nodeid in nodeids:
            count = self.get_flake_count(nodeid, window_runs)
            if count >= threshold:
                result.append({"nodeid": nodeid, "flake_count": count})

        return sorted(result, key=lambda x: x["flake_count"], reverse=True)


# -------------------------------------------------------------------------
# Utility functions
# -------------------------------------------------------------------------


def load_quarantine_list(quarantine_path: Path) -> set[str]:
    """Load quarantined test nodeids from a file.

    File format: one nodeid per line, lines starting with # are comments.

    Args:
        quarantine_path: Path to quarantine file

    Returns:
        Set of quarantined nodeids
    """
    if not quarantine_path.exists():
        return set()

    quarantined = set()
    with open(quarantine_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                quarantined.add(line)

    return quarantined


def save_quarantine_list(quarantine_path: Path, nodeids: set[str]) -> None:
    """Save the quarantine list to a file.

    Creates the file and parent directories if they don't exist.
    Preserves header comment if present.

    Args:
        quarantine_path: Path to quarantine file
        nodeids: Set of nodeids to quarantine
    """
    # Preserve any header comments if file exists
    header_lines = []
    if quarantine_path.exists():
        with open(quarantine_path) as f:
            for line in f:
                if line.startswith("#"):
                    header_lines.append(line.rstrip())
                else:
                    break

    # Ensure parent directory exists
    quarantine_path.parent.mkdir(parents=True, exist_ok=True)

    with open(quarantine_path, "w") as f:
        # Write header if present
        if header_lines:
            for line in header_lines:
                f.write(line + "\n")
            f.write("\n")
        else:
            # Add a default header
            f.write("# Quarantined E2E tests\n")
            f.write("# Tests listed here are excluded from E2E failure counts\n")
            f.write("\n")

        # Write sorted nodeids
        for nodeid in sorted(nodeids):
            f.write(nodeid + "\n")
