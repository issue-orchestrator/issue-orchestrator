"""E2E test results database - SQLite-based persistence.

Stores E2E test run results and per-test outcomes for dashboard visibility.
"""

import json
import logging
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Iterator, Optional

if TYPE_CHECKING:
    from ..ports.timeline_store import TimelineStore

from .e2e_event_nesting import (
    _build_test_windows as _build_test_windows,
    nest_orchestrator_events as nest_orchestrator_events,
)
from .e2e_models import (
    E2EFailureIssue as E2EFailureIssue,
    E2ERunArtifact as E2ERunArtifact,
    E2ERun as E2ERun,
    E2ERunIssue as E2ERunIssue,
    E2ETestResult as E2ETestResult,
)
from .e2e_reports import E2ERunArtifactRecord, JUnitCaseResult
from .e2e_quarantine import (
    load_quarantine_list as load_quarantine_list,
    save_quarantine_list as save_quarantine_list,
)
from .e2e_schema import SCHEMA
from .e2e_stability import (
    TestStability as TestStability,
    _categorize_test as _categorize_test,
    _compute_stability as _compute_stability,
    categorize_test_results as _categorize_test_results,
)
from .sqlite_connection import open_sqlite

logger = logging.getLogger(__name__)


# e2e_db remains the public persistence facade. Supporting modules own the
# extracted schema, row models, stability logic, event nesting, and quarantine IO.
__all__ = [
    "AlreadyRunning",
    "E2EDB",
    "E2EFailureIssue",
    "E2ERunArtifact",
    "E2ERun",
    "E2ERunIssue",
    "E2ETestResult",
    "TestStability",
    "_build_test_windows",
    "_categorize_test",
    "_compute_stability",
    "load_quarantine_list",
    "nest_orchestrator_events",
    "save_quarantine_list",
]


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
        """Create tables if they don't exist, and migrate existing ones."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(SCHEMA)
            # Drop legacy e2e_run_events table (timeline events now live
            # in timeline.sqlite).  Other tables are preserved — they still
            # power run history, stability analysis, and triage features.
            conn.execute("DROP TABLE IF EXISTS e2e_run_events")
            # Migrate: add orchestrator_instance_id if missing (pre-existing DBs)
            columns = {row[1] for row in conn.execute("PRAGMA table_info(e2e_runs)")}
            if "orchestrator_instance_id" not in columns:
                conn.execute(
                    "ALTER TABLE e2e_runs ADD COLUMN orchestrator_instance_id TEXT NOT NULL DEFAULT ''"
                )
            if "command_json" not in columns:
                conn.execute(
                    "ALTER TABLE e2e_runs ADD COLUMN command_json TEXT NOT NULL DEFAULT '[]'"
                )
            if "runner_kind" not in columns:
                conn.execute(
                    "ALTER TABLE e2e_runs ADD COLUMN runner_kind TEXT NOT NULL DEFAULT 'pytest'"
                )
            result_columns = {
                row[1] for row in conn.execute("PRAGMA table_info(e2e_test_results)")
            }
            if "display_name" not in result_columns:
                conn.execute(
                    "ALTER TABLE e2e_test_results ADD COLUMN display_name TEXT"
                )
            if "suite_name" not in result_columns:
                conn.execute(
                    "ALTER TABLE e2e_test_results ADD COLUMN suite_name TEXT"
                )
            if "result_source" not in result_columns:
                conn.execute(
                    "ALTER TABLE e2e_test_results ADD COLUMN result_source TEXT NOT NULL DEFAULT 'runtime'"
                )
            if "stdout_available" not in result_columns:
                conn.execute(
                    "ALTER TABLE e2e_test_results ADD COLUMN stdout_available INTEGER NOT NULL DEFAULT 0"
                )
            if "stderr_available" not in result_columns:
                conn.execute(
                    "ALTER TABLE e2e_test_results ADD COLUMN stderr_available INTEGER NOT NULL DEFAULT 0"
                )
            self._backfill_legacy_run_commands(conn)

    def _backfill_legacy_run_commands(self, conn: sqlite3.Connection) -> None:
        """Populate canonical commands for legacy pytest rows added before command_json."""
        rows = conn.execute(
            """
            SELECT id, pytest_args, command_json, runner_kind
            FROM e2e_runs
            WHERE runner_kind = 'pytest'
            """
        ).fetchall()
        updates: list[tuple[str, int]] = []
        for row in rows:
            raw_command = row["command_json"]
            if isinstance(raw_command, str) and raw_command.strip() not in {"", "[]"}:
                continue
            try:
                pytest_args = json.loads(row["pytest_args"])
            except json.JSONDecodeError:
                logger.warning(
                    "Skipping canonical-command backfill for legacy E2E run %s: invalid pytest_args JSON",
                    row["id"],
                )
                continue
            if not isinstance(pytest_args, list) or not all(
                isinstance(arg, str) for arg in pytest_args
            ):
                logger.warning(
                    "Skipping canonical-command backfill for legacy E2E run %s: pytest_args was not a list[str]",
                    row["id"],
                )
                continue
            if not pytest_args:
                continue
            updates.append((json.dumps(["pytest", *pytest_args]), int(row["id"])))
        if not updates:
            return
        conn.executemany(
            "UPDATE e2e_runs SET command_json = ? WHERE id = ?",
            updates,
        )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """Get a database connection with row factory."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = open_sqlite(self.db_path, timeout=10.0, row_factory=sqlite3.Row)
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
        orchestrator_instance_id: str = "",
        command: list[str] | None = None,
        runner_kind: str = "pytest",
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
            command: Canonical command associated with the run
            runner_kind: Result adapter kind, for example pytest or command

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
                    pytest_args, command_json, runner_kind,
                    commit_sha, branch, retry_of, is_retry_run,
                    worker_pid, orchestrator_instance_id
                ) VALUES (?, ?, ?, 'running', ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    repo_root,
                    orchestrator_id,
                    self._now_iso(),
                    json.dumps(pytest_args),
                    json.dumps(command or []),
                    runner_kind,
                    commit_sha,
                    branch,
                    retry_of,
                    1 if retry_of else 0,
                    worker_pid,
                    orchestrator_instance_id,
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

    def prune_old_runs(
        self,
        retention_count: int,
        timeline_store: "TimelineStore | None" = None,
        e2e_worktree_path: "Path | None" = None,
    ) -> int:
        """Delete runs beyond the retention count, oldest first.

        Removes the run row, its test results, failure issues, run issues,
        flake history, log file on disk, timeline events (if store provided),
        and worktree-local artifacts (run report snapshots, sessions, and
        timeline) if worktree path is provided.

        Returns the number of runs pruned.
        """
        with self._connect() as conn:
            rows = self._runs_to_prune(conn, retention_count)

            if not rows:
                return 0

            pruned = self._delete_pruned_runs(conn, rows, timeline_store, e2e_worktree_path)

            # Clean worktree-local artifacts for pruned runs
            if pruned and e2e_worktree_path is not None:
                cutoff = self._oldest_retained_run_start(retention_count)
                if cutoff:
                    from .e2e_artifact_retention import prune_worktree_artifacts
                    prune_worktree_artifacts(e2e_worktree_path, cutoff)

            if pruned:
                logger.info("Pruned %d old E2E run(s) (retention=%d)", pruned, retention_count)
            return pruned

    def _runs_to_prune(
        self, conn: sqlite3.Connection, retention_count: int
    ) -> list[sqlite3.Row]:
        return conn.execute(
            """
            SELECT id, log_path FROM e2e_runs
            WHERE id NOT IN (
                SELECT id FROM e2e_runs
                ORDER BY started_at DESC
                LIMIT ?
            )
            ORDER BY started_at ASC
            """,
            (retention_count,),
        ).fetchall()

    def _delete_pruned_runs(
        self,
        conn: sqlite3.Connection,
        rows: list[sqlite3.Row],
        timeline_store: "TimelineStore | None",
        e2e_worktree_path: Path | None,
    ) -> int:
        for row in rows:
            run_id = row["id"]
            self._delete_run_rows(conn, run_id)
            self._delete_log_file(row["log_path"])
            self._delete_run_worktree_artifacts(e2e_worktree_path, run_id)
            self._delete_run_timeline(timeline_store, run_id)
        return len(rows)

    def _delete_run_rows(self, conn: sqlite3.Connection, run_id: int) -> None:
        conn.execute("DELETE FROM e2e_run_artifacts WHERE run_id = ?", (run_id,))
        conn.execute("DELETE FROM e2e_test_results WHERE run_id = ?", (run_id,))
        conn.execute("DELETE FROM e2e_failure_issues WHERE first_failing_run_id = ?", (run_id,))
        conn.execute("DELETE FROM e2e_run_issues WHERE run_id = ?", (run_id,))
        conn.execute("DELETE FROM e2e_flake_history WHERE run_id = ?", (run_id,))
        conn.execute("DELETE FROM e2e_runs WHERE id = ?", (run_id,))

    def _delete_log_file(self, log_path: str | None) -> None:
        if not log_path:
            return
        try:
            Path(log_path).unlink(missing_ok=True)
        except OSError:
            pass

    def _delete_run_worktree_artifacts(
        self, e2e_worktree_path: Path | None, run_id: int
    ) -> None:
        if e2e_worktree_path is None:
            return
        from .e2e_artifact_retention import delete_run_report_artifacts
        delete_run_report_artifacts(e2e_worktree_path, run_id)

    def _delete_run_timeline(
        self, timeline_store: "TimelineStore | None", run_id: int
    ) -> None:
        if timeline_store is None:
            return
        try:
            from ..domain.timeline_key import TimelineKey
            store_key = TimelineKey.for_e2e_run(run_id).to_store_key()
            timeline_store.delete(store_key)
        except Exception:
            logger.debug("Could not delete timeline for E2E run %d", run_id)

    def _oldest_retained_run_start(self, retention_count: int) -> str | None:
        """Return the oldest retained run start timestamp."""
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT started_at FROM e2e_runs
                ORDER BY started_at DESC
                LIMIT 1 OFFSET ?
                """,
                (max(0, retention_count - 1),),
            ).fetchone()

        if not row:
            return None
        return row["started_at"]

    def reset_all_history(
        self,
        timeline_store: "TimelineStore | None" = None,
    ) -> dict[str, int]:
        """Delete all E2E run history. Returns counts of deleted items."""
        counts: dict[str, int] = {}
        with self._connect() as conn:
            # Collect run IDs and log paths before deletion
            runs = conn.execute("SELECT id, log_path FROM e2e_runs").fetchall()

            for table in (
                "e2e_run_artifacts",
                "e2e_test_results",
                "e2e_failure_issues",
                "e2e_run_issues",
                "e2e_flake_history",
                "e2e_runs",
            ):
                cursor = conn.execute(f"DELETE FROM {table}")  # noqa: S608 — table names are hardcoded literals
                counts[table] = cursor.rowcount

            # Delete log files
            log_count = 0
            for row in runs:
                if row["log_path"]:
                    try:
                        Path(row["log_path"]).unlink(missing_ok=True)
                        log_count += 1
                    except OSError:
                        pass

                # Delete timeline events
                if timeline_store is not None:
                    try:
                        from ..domain.timeline_key import TimelineKey
                        store_key = TimelineKey.for_e2e_run(row["id"]).to_store_key()
                        timeline_store.delete(store_key)
                    except Exception:
                        pass

            counts["log_files"] = log_count

        logger.info("Reset E2E history: %s", counts)
        return counts

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
        display_name: str | None = None,
        suite_name: str | None = None,
        result_source: str = "runtime",
        stdout_available: bool = False,
        stderr_available: bool = False,
    ) -> None:
        """Insert or update a test result.

        Uses UPSERT to handle retries updating the same nodeid.
        """
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO e2e_test_results (
                    run_id, nodeid, display_name, suite_name, result_source,
                    stdout_available, stderr_available,
                    outcome, duration_seconds, longrepr, retry_outcome,
                    is_quarantined, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id, nodeid) DO UPDATE SET
                    display_name = COALESCE(excluded.display_name, e2e_test_results.display_name),
                    suite_name = COALESCE(excluded.suite_name, e2e_test_results.suite_name),
                    result_source = excluded.result_source,
                    stdout_available = e2e_test_results.stdout_available OR excluded.stdout_available,
                    stderr_available = e2e_test_results.stderr_available OR excluded.stderr_available,
                    outcome = excluded.outcome,
                    duration_seconds = excluded.duration_seconds,
                    longrepr = excluded.longrepr,
                    retry_outcome = excluded.retry_outcome,
                    is_quarantined = e2e_test_results.is_quarantined OR excluded.is_quarantined,
                    updated_at = excluded.updated_at
                """,
                (
                    run_id,
                    nodeid,
                    display_name,
                    suite_name,
                    result_source,
                    1 if stdout_available else 0,
                    1 if stderr_available else 0,
                    outcome,
                    duration_seconds,
                    longrepr,
                    retry_outcome,
                    1 if is_quarantined else 0,
                    self._now_iso(),
                ),
            )

    def upsert_result_case(
        self,
        run_id: int,
        case_id: str,
        outcome: str,
        duration_seconds: float | None = None,
        failure_details: str | None = None,
        display_name: str | None = None,
        suite_name: str | None = None,
        result_source: str = "external_report",
        stdout_available: bool = False,
        stderr_available: bool = False,
        is_quarantined: bool = False,
    ) -> None:
        """Generic wrapper for non-pytest case results."""
        self.upsert_test_result(
            run_id=run_id,
            nodeid=case_id,
            outcome=outcome,
            duration_seconds=duration_seconds,
            longrepr=failure_details,
            display_name=display_name,
            suite_name=suite_name,
            result_source=result_source,
            stdout_available=stdout_available,
            stderr_available=stderr_available,
            is_quarantined=is_quarantined,
        )

    def replace_run_artifacts(
        self,
        run_id: int,
        artifacts: list[E2ERunArtifactRecord],
    ) -> None:
        """Replace the run-scoped artifact set atomically."""
        with self._connect() as conn:
            conn.execute("DELETE FROM e2e_run_artifacts WHERE run_id = ?", (run_id,))
            for artifact in artifacts:
                conn.execute(
                    """
                    INSERT INTO e2e_run_artifacts (run_id, kind, label, path, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        artifact.kind,
                        artifact.label,
                        artifact.path,
                        self._now_iso(),
                    ),
                )

    def list_run_artifacts(self, run_id: int) -> list[E2ERunArtifact]:
        """List artifacts for one run."""
        with self._connect() as conn:
            cursor = conn.execute(
                """
                SELECT * FROM e2e_run_artifacts
                WHERE run_id = ?
                ORDER BY kind, label, path
                """,
                (run_id,),
            )
            return [E2ERunArtifact.from_row(row) for row in cursor.fetchall()]

    def record_junit_cases(
        self,
        run_id: int,
        cases: list[JUnitCaseResult],
        quarantine: set[str] | None = None,
    ) -> None:
        """Persist parsed JUnit cases into e2e_test_results."""
        quarantine = quarantine or set()
        for case in cases:
            self.upsert_result_case(
                run_id=run_id,
                case_id=case.case_id,
                outcome=case.outcome,
                duration_seconds=case.duration_seconds,
                failure_details=case.failure_details,
                display_name=case.display_name,
                suite_name=case.suite_name,
                result_source="junit_xml",
                stdout_available=case.system_out is not None,
                stderr_available=case.system_err is not None,
                is_quarantined=case.case_id in quarantine,
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
            artifacts_cursor = conn.execute(
                """
                SELECT * FROM e2e_run_artifacts
                WHERE run_id = ?
                ORDER BY kind, label, path
                """,
                (run_id,),
            )
            artifacts = [E2ERunArtifact.from_row(r) for r in artifacts_cursor.fetchall()]

            return {
                "run": run.to_dict(),
                "results": [r.to_dict() for r in results],
                "artifacts": [artifact.to_dict() for artifact in artifacts],
            }

    def _fetch_test_history(
        self,
        conn: sqlite3.Connection,
        nodeids: list[str],
        run_id: int,
        history_limit: int,
    ) -> dict[str, list[dict]]:
        """Batch fetch test outcome history for all nodeids.

        Uses SQL window function to limit rows per nodeid at the database level,
        avoiding fetching unnecessary data for large test histories.
        """
        if not nodeids:
            return {}

        from collections import defaultdict

        # S608: ``placeholders`` only ever contains ``?`` characters — it
        # is the dynamic-IN-clause idiom for binding a variable-length
        # list of parameters. Values are still bound via ``(*nodeids,
        # ...)``.
        placeholders = ",".join("?" * len(nodeids))
        cursor = conn.execute(
            f"""
            WITH ranked_history AS (
                SELECT
                    t.nodeid,
                    COALESCE(t.retry_outcome, t.outcome) AS effective_outcome,
                    r.id AS run_id,
                    ROW_NUMBER() OVER (PARTITION BY t.nodeid ORDER BY r.started_at DESC) AS rn
                FROM e2e_test_results t
                JOIN e2e_runs r ON t.run_id = r.id
                WHERE t.nodeid IN ({placeholders})
                    AND r.id != ?
                    AND r.status IN ('passed', 'failed')
            )
            SELECT nodeid, effective_outcome, run_id
            FROM ranked_history
            WHERE rn <= ?
            ORDER BY nodeid
            """,  # noqa: S608
            (*nodeids, run_id, history_limit),
        )
        history: dict[str, list[dict]] = defaultdict(list)
        for row in cursor.fetchall():
            history[row["nodeid"]].append({
                "outcome": row["effective_outcome"],
                "run_id": row["run_id"],
            })
        return dict(history)

    def _fetch_issue_info(
        self,
        conn: sqlite3.Connection,
        nodeids: list[str],
    ) -> dict[str, dict]:
        """Batch fetch failure issue info for all nodeids."""
        if not nodeids:
            return {}

        issues_by_nodeid: dict[str, dict] = {}
        conn.execute(
            "CREATE TEMP TABLE IF NOT EXISTS temp_e2e_issue_nodeids (nodeid TEXT PRIMARY KEY)"
        )
        conn.execute("DELETE FROM temp_e2e_issue_nodeids")
        conn.executemany(
            "INSERT OR IGNORE INTO temp_e2e_issue_nodeids (nodeid) VALUES (?)",
            [(nodeid,) for nodeid in nodeids],
        )
        cursor = conn.execute(
            """
            SELECT f.nodeid, f.github_issue_number, f.resolution, f.resolved_at
            FROM e2e_failure_issues f
            JOIN temp_e2e_issue_nodeids n ON n.nodeid = f.nodeid
            ORDER BY f.nodeid, f.created_at DESC
            """
        )
        # Take the most recent issue per nodeid
        for issue_row in cursor.fetchall():
            nodeid = issue_row["nodeid"]
            if nodeid not in issues_by_nodeid:
                issues_by_nodeid[nodeid] = {
                    "number": issue_row["github_issue_number"],
                    "status": "closed" if issue_row["resolved_at"] else "open",
                    "resolution": issue_row["resolution"],
                }
        return issues_by_nodeid

    def run_details_enhanced(
        self,
        run_id: int,
        history_limit: int = 5,
        flake_threshold_percent: float = 20.0,
    ) -> Optional[dict]:
        """Get enhanced run details with test history, issue info, and categories.

        This is used by the unified run view to display all information needed
        for triaging E2E failures without additional API calls.

        Returns:
            Dict with:
                - run: Run metadata
                - tests_by_category: Tests grouped by state (untriaged, has_issue, flaky, fixed, passed)
                - summary: Counts for each category
        """
        with self._connect() as conn:
            # Get run
            cursor = conn.execute(
                "SELECT * FROM e2e_runs WHERE id = ?", (run_id,)
            )
            row = cursor.fetchone()
            if not row:
                return None

            run = E2ERun.from_row(row)

            # Get all test results for this run
            cursor = conn.execute(
                "SELECT * FROM e2e_test_results WHERE run_id = ? ORDER BY nodeid",
                (run_id,),
            )
            results = [E2ETestResult.from_row(r) for r in cursor.fetchall()]

            # Batch fetch history and issues
            nodeids = [r.nodeid for r in results]
            history_by_nodeid = self._fetch_test_history(conn, nodeids, run_id, history_limit)
            issues_by_nodeid = self._fetch_issue_info(conn, nodeids)

            # Build enhanced test data and categorize
            tests_by_category = _categorize_test_results(
                results, history_by_nodeid, issues_by_nodeid, flake_threshold_percent
            )

            # Build summary counts
            summary = {cat: len(tests) for cat, tests in tests_by_category.items()}
            summary["total"] = len(results)
            artifacts_cursor = conn.execute(
                """
                SELECT * FROM e2e_run_artifacts
                WHERE run_id = ?
                ORDER BY kind, label, path
                """,
                (run_id,),
            )
            artifacts = [E2ERunArtifact.from_row(r).to_dict() for r in artifacts_cursor.fetchall()]

            return {
                "run": run.to_dict(),
                "tests_by_category": tests_by_category,
                "summary": summary,
                "artifacts": artifacts,
            }

    def get_failed_tests(self, run_id: int) -> list[E2ETestResult]:
        """Get just the failed tests from a run (excluding quarantined)."""
        with self._connect() as conn:
            cursor = conn.execute(
                """
                SELECT * FROM e2e_test_results
                WHERE run_id = ?
                    AND outcome IN ('failed', 'error')
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
            elif r.outcome in {"failed", "error"}:
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
    # Flip-Rate Stability Methods
    # -------------------------------------------------------------------------

    def _get_recent_outcomes(
        self,
        nodeid: str,
        window_runs: int = 10,
    ) -> list[str]:
        """Get recent effective outcomes for a test across completed runs.

        Returns outcomes most-recent-first. Uses COALESCE(retry_outcome, outcome)
        to get the effective outcome (retry takes precedence). Only includes
        completed runs with pass/fail outcomes.

        Args:
            nodeid: Test node ID
            window_runs: Number of recent runs to include
        """
        with self._connect() as conn:
            cursor = conn.execute(
                """
                SELECT COALESCE(t.retry_outcome, t.outcome) AS effective_outcome
                FROM e2e_test_results t
                JOIN e2e_runs r ON t.run_id = r.id
                WHERE t.nodeid = ?
                    AND r.status IN ('passed', 'failed')
                    AND COALESCE(t.retry_outcome, t.outcome) IN ('passed', 'failed')
                ORDER BY r.started_at DESC
                LIMIT ?
                """,
                (nodeid, window_runs),
            )
            return [row["effective_outcome"] for row in cursor.fetchall()]

    def get_test_stability(
        self,
        nodeid: str,
        window_runs: int = 10,
        flake_threshold_percent: float = 20.0,
    ) -> TestStability:
        """Get flip-rate stability analysis for a single test.

        Args:
            nodeid: Test node ID
            window_runs: Number of recent runs to analyze
            flake_threshold_percent: Flip rate percentage (0-100) to flag as flaky
        """
        outcomes = self._get_recent_outcomes(nodeid, window_runs)
        return _compute_stability(nodeid, outcomes, flake_threshold_percent)

    def get_all_test_stability(
        self,
        window_runs: int = 10,
        flake_threshold_percent: float = 20.0,
    ) -> list[TestStability]:
        """Get flip-rate stability for all tests with recent history.

        Single SQL call to fetch all outcomes, then compute stability per test.

        Args:
            window_runs: Number of recent runs to analyze per test
            flake_threshold_percent: Flip rate percentage (0-100) to flag as flaky
        """
        with self._connect() as conn:
            # Bulk query: get recent outcomes for all tests, ordered by test then recency
            cursor = conn.execute(
                """
                SELECT t.nodeid,
                       COALESCE(t.retry_outcome, t.outcome) AS effective_outcome,
                       r.started_at
                FROM e2e_test_results t
                JOIN e2e_runs r ON t.run_id = r.id
                WHERE r.status IN ('passed', 'failed')
                    AND COALESCE(t.retry_outcome, t.outcome) IN ('passed', 'failed')
                ORDER BY t.nodeid, r.started_at DESC
                """,
            )

            # Group outcomes by nodeid, limiting to window_runs per test
            from collections import defaultdict
            outcomes_by_nodeid: dict[str, list[str]] = defaultdict(list)
            for row in cursor.fetchall():
                nodeid_key = row["nodeid"]
                if len(outcomes_by_nodeid[nodeid_key]) < window_runs:
                    outcomes_by_nodeid[nodeid_key].append(row["effective_outcome"])

        results = []
        for nodeid_key, outcomes in outcomes_by_nodeid.items():
            stability = _compute_stability(nodeid_key, outcomes, flake_threshold_percent)
            results.append(stability)

        # Sort by flip_rate descending for most-flaky-first ordering
        results.sort(key=lambda s: s.flip_rate, reverse=True)
        return results


    # -------------------------------------------------------------------------
    # Run events (timeline)
    # -------------------------------------------------------------------------
