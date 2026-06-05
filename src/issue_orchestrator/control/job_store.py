"""SQLite-based job persistence for publish jobs.

This module provides durable storage for publish jobs, enabling:
1. Crash recovery - Resume in-flight jobs after orchestrator restart
2. Audit trail - Historical record of all publish attempts
3. Orphan detection - Identify jobs whose worktrees no longer exist

Key Design Principle:
    Jobs are tied to worktree lifecycle. A job without its worktree cannot
    be resumed or retried - it becomes a historical record only.

    On startup, we validate all non-terminal jobs and mark those with
    missing worktrees as WORKTREE_GONE.

    When a worktree is cleaned up, any associated jobs should be marked
    as WORKTREE_GONE via mark_worktree_cleaned().

Worktree Identity:
    Paths alone are insufficient for identity - a path can be reused for a
    different worktree after deletion. Each worktree gets a unique ID stored
    in a marker file (.issue-orchestrator/worktree-id). Jobs store this ID
    and validation checks both path existence AND identity match.

Job States:
    QUEUED          - Job submitted, waiting for worker
    RUNNING         - Worker is executing the job
    SUCCEEDED       - Job completed successfully
    FAILED          - Job failed (worktree still exists, could potentially retry)
    WORKTREE_GONE   - Worktree was cleaned up, job is now historical only
"""

import json
import logging
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional

from ..domain.models import PublishJob
from ..infra.sqlite_connection import open_sqlite

logger = logging.getLogger(__name__)

# Schema version for migrations
# Version 2: Added worktree_id for robust path reuse handling
SCHEMA_VERSION = 2

# Marker file name for worktree identity
WORKTREE_ID_MARKER = ".issue-orchestrator/worktree-id"


def generate_worktree_id() -> str:
    """Generate a unique worktree identity."""
    return f"wt-{uuid.uuid4().hex[:12]}"


def get_worktree_id(worktree_path: Path) -> str | None:
    """Read the worktree identity from its marker file.

    Args:
        worktree_path: Path to the worktree directory

    Returns:
        The worktree ID if found, None if missing or unreadable
    """
    marker_path = worktree_path / WORKTREE_ID_MARKER
    try:
        if marker_path.exists():
            return marker_path.read_text().strip()
    except Exception:
        pass
    return None


def set_worktree_id(worktree_path: Path, worktree_id: str | None = None) -> str:
    """Write or create a worktree identity marker.

    Args:
        worktree_path: Path to the worktree directory
        worktree_id: ID to write (generates one if None)

    Returns:
        The worktree ID that was written
    """
    if worktree_id is None:
        worktree_id = generate_worktree_id()

    marker_path = worktree_path / WORKTREE_ID_MARKER
    marker_path.parent.mkdir(parents=True, exist_ok=True)
    marker_path.write_text(worktree_id)
    return worktree_id


def ensure_worktree_id(worktree_path: Path) -> str:
    """Get existing worktree ID or create one if missing.

    This is idempotent - safe to call multiple times.

    Args:
        worktree_path: Path to the worktree directory

    Returns:
        The worktree ID (existing or newly created)
    """
    existing = get_worktree_id(worktree_path)
    if existing:
        return existing
    return set_worktree_id(worktree_path)


@dataclass(frozen=True)
class JobRecord:
    """Persistent job record from SQLite.

    This is the stored representation of a job. It contains all the data
    needed to reconstruct a PublishJob or provide historical information.
    """

    job_id: str
    issue_number: int
    session_key: str
    worktree_path: str
    worktree_id: str  # Unique identity for the worktree (survives path reuse)
    branch_name: str
    status: str  # One of: queued, running, succeeded, failed, worktree_gone

    # Timestamps (Unix epoch)
    created_at: float
    started_at: Optional[float] = None
    finished_at: Optional[float] = None

    # Result data (populated on completion)
    pr_url: Optional[str] = None
    pr_number: Optional[int] = None
    error_message: Optional[str] = None
    errors_json: Optional[str] = None  # JSON array of error strings

    # Metadata (stored as JSON)
    metadata_json: Optional[str] = None

    @property
    def duration_seconds(self) -> Optional[float]:
        """Calculate job duration if started and finished."""
        if self.started_at and self.finished_at:
            return self.finished_at - self.started_at
        return None

    @property
    def is_terminal(self) -> bool:
        """Check if job is in a terminal state."""
        return self.status in ("succeeded", "failed", "worktree_gone")

    @property
    def is_resumable(self) -> bool:
        """Check if job could potentially be resumed (non-terminal, not worktree_gone)."""
        return self.status in ("queued", "running")


class JobStore:
    """SQLite-based persistent storage for publish jobs.

    Thread-safe: Uses connection pooling with thread-local connections.

    Usage:
        store = JobStore(db_path)
        store.initialize()

        # Save a job
        store.save_job(job)

        # Update status
        store.mark_started(job_id)
        store.mark_succeeded(job_id, pr_url, pr_number)
        store.mark_failed(job_id, error_message)

        # Query jobs
        pending = store.get_pending_jobs()
        running = store.get_running_jobs()

        # Cleanup
        store.mark_worktree_cleaned(worktree_path)
    """

    def __init__(self, db_path: Path):
        """Initialize the job store.

        Args:
            db_path: Path to SQLite database file
        """
        self._db_path = db_path
        self._local = threading.local()
        self._write_lock = threading.Lock()

    def _get_connection(self) -> sqlite3.Connection:
        """Get thread-local database connection."""
        if not hasattr(self._local, "conn") or self._local.conn is None:
            self._local.conn = open_sqlite(
                self._db_path,
                check_same_thread=False,
                isolation_level=None,  # Autocommit mode
                row_factory=sqlite3.Row,
            )
        return self._local.conn

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        """Context manager for write transactions with locking."""
        with self._write_lock:
            conn = self._get_connection()
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def initialize(self) -> None:
        """Initialize the database schema.

        Creates tables if they don't exist and runs any necessary migrations.
        """
        self._db_path.parent.mkdir(parents=True, exist_ok=True)

        with self._transaction() as conn:
            # Create jobs table
            conn.execute("""
                CREATE TABLE IF NOT EXISTS publish_jobs (
                    job_id TEXT PRIMARY KEY,
                    issue_number INTEGER NOT NULL,
                    session_key TEXT NOT NULL,
                    worktree_path TEXT NOT NULL,
                    worktree_id TEXT NOT NULL DEFAULT '',
                    branch_name TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'queued',

                    created_at REAL NOT NULL,
                    started_at REAL,
                    finished_at REAL,

                    pr_url TEXT,
                    pr_number INTEGER,
                    error_message TEXT,
                    errors_json TEXT,

                    metadata_json TEXT,

                    -- Constraints
                    CHECK (status IN ('queued', 'running', 'succeeded', 'failed', 'worktree_gone'))
                )
            """)

            # Migration: Add worktree_id column if not exists (v1 -> v2)
            try:
                conn.execute("SELECT worktree_id FROM publish_jobs LIMIT 1")
            except sqlite3.OperationalError:
                conn.execute("ALTER TABLE publish_jobs ADD COLUMN worktree_id TEXT NOT NULL DEFAULT ''")

            # Create indexes for common queries
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_jobs_status
                ON publish_jobs(status)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_jobs_issue
                ON publish_jobs(issue_number)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_jobs_worktree
                ON publish_jobs(worktree_path)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_jobs_session_key
                ON publish_jobs(session_key)
            """)

            # Create schema version table
            conn.execute("""
                CREATE TABLE IF NOT EXISTS schema_version (
                    version INTEGER PRIMARY KEY
                )
            """)

            # Set schema version if not exists
            cursor = conn.execute("SELECT version FROM schema_version")
            if cursor.fetchone() is None:
                conn.execute(
                    "INSERT INTO schema_version (version) VALUES (?)",
                    (SCHEMA_VERSION,),
                )

        logger.info("[JOB_STORE] Initialized database at %s", self._db_path)

    def save_job(self, job: PublishJob) -> None:
        """Save a new job to the database.

        Args:
            job: The PublishJob to persist
        """
        metadata = {
            "issue_title": job.issue_title,
            "outcome": job.outcome,
            "requested_actions": list(job.requested_actions),
            "completion_path": job.completion_path,
            "agent_label": job.agent_label,
            "retry_publish": job.retry_publish,
            "run_assets": job.run_assets.to_dict(),
        }

        # Get or create worktree identity
        worktree_id = ensure_worktree_id(Path(job.worktree_path))

        with self._transaction() as conn:
            conn.execute(
                """
                INSERT INTO publish_jobs (
                    job_id, issue_number, session_key, worktree_path, worktree_id, branch_name,
                    status, created_at, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job.job_id,
                    job.issue_number,
                    job.session_key,
                    job.worktree_path,
                    worktree_id,
                    job.branch_name,
                    job.status.value,
                    time.time(),
                    json.dumps(metadata),
                ),
            )

        logger.debug(
            "[JOB_STORE] Saved job: job_id=%s issue=%d",
            job.job_id,
            job.issue_number,
        )

    def mark_started(self, job_id: str) -> bool:
        """Mark a job as started (running).

        Args:
            job_id: The job ID to update

        Returns:
            True if job was updated, False if not found or already terminal
        """
        with self._transaction() as conn:
            cursor = conn.execute(
                """
                UPDATE publish_jobs
                SET status = 'running', started_at = ?
                WHERE job_id = ? AND status = 'queued'
                """,
                (time.time(), job_id),
            )
            return cursor.rowcount > 0

    def mark_succeeded(
        self,
        job_id: str,
        pr_url: Optional[str] = None,
        pr_number: Optional[int] = None,
    ) -> bool:
        """Mark a job as succeeded.

        Args:
            job_id: The job ID to update
            pr_url: URL of created PR (if any)
            pr_number: Number of created PR (if any)

        Returns:
            True if job was updated, False if not found or already terminal
        """
        with self._transaction() as conn:
            cursor = conn.execute(
                """
                UPDATE publish_jobs
                SET status = 'succeeded', finished_at = ?, pr_url = ?, pr_number = ?
                WHERE job_id = ? AND status IN ('queued', 'running')
                """,
                (time.time(), pr_url, pr_number, job_id),
            )
            return cursor.rowcount > 0

    def mark_failed(
        self,
        job_id: str,
        error_message: str,
        errors: Optional[list[str]] = None,
    ) -> bool:
        """Mark a job as failed.

        Args:
            job_id: The job ID to update
            error_message: Primary error message
            errors: Optional list of additional error details

        Returns:
            True if job was updated, False if not found or already terminal
        """
        errors_json = json.dumps(errors) if errors else None

        with self._transaction() as conn:
            cursor = conn.execute(
                """
                UPDATE publish_jobs
                SET status = 'failed', finished_at = ?, error_message = ?, errors_json = ?
                WHERE job_id = ? AND status IN ('queued', 'running')
                """,
                (time.time(), error_message, errors_json, job_id),
            )
            return cursor.rowcount > 0

    def mark_worktree_cleaned(self, worktree_path: str) -> int:
        """Mark all jobs for a worktree as WORKTREE_GONE.

        Called when a worktree is cleaned up. Any non-terminal jobs
        become historical records.

        Args:
            worktree_path: Path to the cleaned worktree

        Returns:
            Number of jobs marked as worktree_gone
        """
        with self._transaction() as conn:
            cursor = conn.execute(
                """
                UPDATE publish_jobs
                SET status = 'worktree_gone', finished_at = ?
                WHERE worktree_path = ? AND status IN ('queued', 'running', 'failed')
                """,
                (time.time(), worktree_path),
            )
            count = cursor.rowcount

        if count > 0:
            logger.info(
                "[JOB_STORE] Marked %d jobs as worktree_gone for: %s",
                count,
                worktree_path,
            )
        return count

    def get_job(self, job_id: str) -> Optional[JobRecord]:
        """Get a job by ID.

        Args:
            job_id: The job ID to look up

        Returns:
            JobRecord if found, None otherwise
        """
        conn = self._get_connection()
        cursor = conn.execute(
            "SELECT * FROM publish_jobs WHERE job_id = ?",
            (job_id,),
        )
        row = cursor.fetchone()
        return self._row_to_record(row) if row else None

    def get_pending_jobs(self) -> list[JobRecord]:
        """Get all jobs in QUEUED state."""
        return self._get_jobs_by_status("queued")

    def get_running_jobs(self) -> list[JobRecord]:
        """Get all jobs in RUNNING state."""
        return self._get_jobs_by_status("running")

    def get_active_jobs(self) -> list[JobRecord]:
        """Get all non-terminal jobs (QUEUED or RUNNING)."""
        conn = self._get_connection()
        cursor = conn.execute(
            """
            SELECT * FROM publish_jobs
            WHERE status IN ('queued', 'running')
            ORDER BY created_at ASC
            """
        )
        return [self._row_to_record(row) for row in cursor.fetchall()]

    def get_jobs_for_issue(self, issue_number: int) -> list[JobRecord]:
        """Get all jobs for a specific issue."""
        conn = self._get_connection()
        cursor = conn.execute(
            """
            SELECT * FROM publish_jobs
            WHERE issue_number = ?
            ORDER BY created_at DESC
            """,
            (issue_number,),
        )
        return [self._row_to_record(row) for row in cursor.fetchall()]

    def get_recent_jobs(self, limit: int = 100) -> list[JobRecord]:
        """Get most recent jobs across all issues."""
        conn = self._get_connection()
        cursor = conn.execute(
            """
            SELECT * FROM publish_jobs
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (limit,),
        )
        return [self._row_to_record(row) for row in cursor.fetchall()]

    def validate_worktrees(self) -> int:
        """Validate all active jobs have existing worktrees with matching identity.

        Called on startup to detect orphaned jobs whose worktrees
        were cleaned up while the orchestrator was down, or whose
        worktrees were deleted and recreated (path reuse).

        Checks both:
        1. Worktree path exists
        2. Worktree identity matches (protects against path reuse)

        Returns:
            Number of jobs marked as worktree_gone
        """
        active_jobs = self.get_active_jobs()
        orphaned_count = 0

        for job in active_jobs:
            worktree = Path(job.worktree_path)
            reason = None

            if not worktree.exists():
                reason = "Worktree missing on startup"
            elif job.worktree_id:
                # Check identity match (only if we recorded an ID)
                current_id = get_worktree_id(worktree)
                if current_id is None:
                    reason = "Worktree missing identity marker"
                elif current_id != job.worktree_id:
                    reason = f"Worktree identity mismatch: expected {job.worktree_id}, found {current_id}"

            if reason:
                logger.warning(
                    "[JOB_STORE] Orphaned job detected: job_id=%s worktree=%s reason=%s",
                    job.job_id,
                    job.worktree_path,
                    reason,
                )
                with self._transaction() as conn:
                    conn.execute(
                        """
                        UPDATE publish_jobs
                        SET status = 'worktree_gone', finished_at = ?,
                            error_message = ?
                        WHERE job_id = ?
                        """,
                        (time.time(), reason, job.job_id),
                    )
                orphaned_count += 1

        if orphaned_count > 0:
            logger.info(
                "[JOB_STORE] Validated worktrees: %d orphaned jobs marked",
                orphaned_count,
            )
        return orphaned_count

    def cleanup_old_jobs(self, max_age_days: int = 30) -> int:
        """Delete old terminal jobs to prevent unbounded growth.

        Args:
            max_age_days: Delete terminal jobs older than this many days

        Returns:
            Number of jobs deleted
        """
        cutoff = time.time() - (max_age_days * 86400)

        with self._transaction() as conn:
            cursor = conn.execute(
                """
                DELETE FROM publish_jobs
                WHERE status IN ('succeeded', 'failed', 'worktree_gone')
                AND finished_at < ?
                """,
                (cutoff,),
            )
            count = cursor.rowcount

        if count > 0:
            logger.info(
                "[JOB_STORE] Cleaned up %d old jobs (older than %d days)",
                count,
                max_age_days,
            )
        return count

    def _get_jobs_by_status(self, status: str) -> list[JobRecord]:
        """Get jobs by status."""
        conn = self._get_connection()
        cursor = conn.execute(
            """
            SELECT * FROM publish_jobs
            WHERE status = ?
            ORDER BY created_at ASC
            """,
            (status,),
        )
        return [self._row_to_record(row) for row in cursor.fetchall()]

    def _row_to_record(self, row: sqlite3.Row) -> JobRecord:
        """Convert a database row to a JobRecord."""
        return JobRecord(
            job_id=row["job_id"],
            issue_number=row["issue_number"],
            session_key=row["session_key"],
            worktree_path=row["worktree_path"],
            worktree_id=row["worktree_id"] or "",  # Handle NULL for migrated rows
            branch_name=row["branch_name"],
            status=row["status"],
            created_at=row["created_at"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            pr_url=row["pr_url"],
            pr_number=row["pr_number"],
            error_message=row["error_message"],
            errors_json=row["errors_json"],
            metadata_json=row["metadata_json"],
        )

    def close(self) -> None:
        """Close the database connection for the current thread."""
        if hasattr(self._local, "conn") and self._local.conn is not None:
            self._local.conn.close()
            self._local.conn = None


def get_default_db_path(repo_root: Path) -> Path:
    """Get the default database path for job storage.

    Args:
        repo_root: Repository root directory

    Returns:
        Path to the SQLite database file
    """
    return repo_root / ".issue-orchestrator" / "state" / "publish_jobs.db"
