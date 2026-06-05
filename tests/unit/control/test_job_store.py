"""Unit tests for JobStore - SQLite-based job persistence.

Tests cover:
1. Schema initialization and migrations
2. Job CRUD operations (save, mark_started, mark_succeeded, mark_failed)
3. Query methods (get_pending_jobs, get_running_jobs, etc.)
4. Worktree identity functions
5. Worktree validation and orphan detection
6. Cleanup of old jobs
7. Thread safety
"""

import json
import shutil
import sqlite3
import threading
import time
from pathlib import Path

import pytest

from issue_orchestrator.control.job_store import (
    JobStore,
    JobRecord,
    SCHEMA_VERSION,
    WORKTREE_ID_MARKER,
    generate_worktree_id,
    get_worktree_id,
    set_worktree_id,
    ensure_worktree_id,
    get_default_db_path,
)
from issue_orchestrator.domain.models import PublishJob, PublishJobStatus
from tests.unit.session_run_helpers import make_session_run_assets


def make_publish_job(
    worktree: Path,
    *,
    job_id: str,
    issue_number: int,
    session_key: str,
    branch_name: str,
    **kwargs: object,
) -> PublishJob:
    """Create a PublishJob with explicit typed run assets."""
    return PublishJob(
        job_id=job_id,
        issue_number=issue_number,
        session_key=session_key,
        run_assets=make_session_run_assets(
            worktree,
            session_name=f"issue-{issue_number}",
        ),
        worktree_path=str(worktree),
        branch_name=branch_name,
        **kwargs,
    )


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def tmp_db_path(tmp_path: Path) -> Path:
    """Create a temporary database path."""
    return tmp_path / "test_jobs.db"


@pytest.fixture
def job_store(tmp_db_path: Path) -> JobStore:
    """Create an initialized JobStore."""
    store = JobStore(tmp_db_path)
    store.initialize()
    return store


@pytest.fixture
def sample_worktree(tmp_path: Path) -> Path:
    """Create a sample worktree directory."""
    worktree = tmp_path / "worktrees" / "issue-123"
    worktree.mkdir(parents=True)
    return worktree


@pytest.fixture
def sample_job(sample_worktree: Path) -> PublishJob:
    """Create a sample PublishJob for testing."""
    return make_publish_job(
        sample_worktree,
        job_id="job-123-abc",
        issue_number=123,
        session_key="code:123",
        status=PublishJobStatus.QUEUED,
        branch_name="issue-123-fix-bug",
        completion_path="completion.json",
        issue_title="Fix important bug",
        outcome="completed",
        requested_actions=("git_push", "create_pr"),
        agent_label="agent:developer",
    )


# ============================================================================
# Worktree Identity Functions
# ============================================================================


class TestGenerateWorktreeId:
    """Tests for generate_worktree_id function."""

    def test_generates_unique_ids(self):
        """Verify each call generates a unique ID."""
        ids = [generate_worktree_id() for _ in range(100)]
        assert len(set(ids)) == 100

    def test_id_has_expected_format(self):
        """Verify ID format: wt-{12 hex chars}."""
        wt_id = generate_worktree_id()
        assert wt_id.startswith("wt-")
        assert len(wt_id) == 15  # "wt-" + 12 hex chars


class TestGetWorktreeId:
    """Tests for get_worktree_id function."""

    def test_returns_none_for_missing_path(self, tmp_path: Path):
        """Verify returns None if path doesn't exist."""
        result = get_worktree_id(tmp_path / "nonexistent")
        assert result is None

    def test_returns_none_for_missing_marker(self, sample_worktree: Path):
        """Verify returns None if marker file doesn't exist."""
        result = get_worktree_id(sample_worktree)
        assert result is None

    def test_returns_id_from_marker(self, sample_worktree: Path):
        """Verify returns ID from marker file."""
        marker_path = sample_worktree / WORKTREE_ID_MARKER
        marker_path.parent.mkdir(parents=True, exist_ok=True)
        marker_path.write_text("wt-abc123def456")

        result = get_worktree_id(sample_worktree)
        assert result == "wt-abc123def456"

    def test_strips_whitespace(self, sample_worktree: Path):
        """Verify whitespace is stripped from ID."""
        marker_path = sample_worktree / WORKTREE_ID_MARKER
        marker_path.parent.mkdir(parents=True, exist_ok=True)
        marker_path.write_text("  wt-abc123def456  \n")

        result = get_worktree_id(sample_worktree)
        assert result == "wt-abc123def456"


class TestSetWorktreeId:
    """Tests for set_worktree_id function."""

    def test_writes_provided_id(self, sample_worktree: Path):
        """Verify writes the provided ID."""
        result = set_worktree_id(sample_worktree, "wt-custom-id")

        assert result == "wt-custom-id"
        marker_path = sample_worktree / WORKTREE_ID_MARKER
        assert marker_path.read_text() == "wt-custom-id"

    def test_generates_id_if_none(self, sample_worktree: Path):
        """Verify generates ID if not provided."""
        result = set_worktree_id(sample_worktree)

        assert result.startswith("wt-")
        marker_path = sample_worktree / WORKTREE_ID_MARKER
        assert marker_path.read_text() == result

    def test_creates_parent_directories(self, sample_worktree: Path):
        """Verify creates .issue-orchestrator directory if needed."""
        result = set_worktree_id(sample_worktree, "wt-test-id")

        marker_path = sample_worktree / WORKTREE_ID_MARKER
        assert marker_path.exists()
        assert marker_path.read_text() == "wt-test-id"


class TestEnsureWorktreeId:
    """Tests for ensure_worktree_id function."""

    def test_returns_existing_id(self, sample_worktree: Path):
        """Verify returns existing ID without modification."""
        marker_path = sample_worktree / WORKTREE_ID_MARKER
        marker_path.parent.mkdir(parents=True, exist_ok=True)
        marker_path.write_text("wt-existing-id")

        result = ensure_worktree_id(sample_worktree)
        assert result == "wt-existing-id"

    def test_creates_id_if_missing(self, sample_worktree: Path):
        """Verify creates new ID if none exists."""
        result = ensure_worktree_id(sample_worktree)

        assert result.startswith("wt-")
        # Verify it was persisted
        assert get_worktree_id(sample_worktree) == result

    def test_idempotent(self, sample_worktree: Path):
        """Verify multiple calls return the same ID."""
        id1 = ensure_worktree_id(sample_worktree)
        id2 = ensure_worktree_id(sample_worktree)
        id3 = ensure_worktree_id(sample_worktree)

        assert id1 == id2 == id3


# ============================================================================
# JobStore Initialization
# ============================================================================


class TestJobStoreInitialize:
    """Tests for JobStore.initialize()."""

    def test_creates_database_file(self, tmp_db_path: Path):
        """Verify database file is created."""
        store = JobStore(tmp_db_path)
        store.initialize()

        assert tmp_db_path.exists()

    def test_creates_parent_directories(self, tmp_path: Path):
        """Verify parent directories are created."""
        db_path = tmp_path / "nested" / "path" / "jobs.db"
        store = JobStore(db_path)
        store.initialize()

        assert db_path.exists()

    def test_creates_jobs_table(self, tmp_db_path: Path):
        """Verify publish_jobs table is created."""
        store = JobStore(tmp_db_path)
        store.initialize()

        conn = sqlite3.connect(str(tmp_db_path))
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='publish_jobs'"
        )
        assert cursor.fetchone() is not None
        conn.close()

    def test_creates_indexes(self, tmp_db_path: Path):
        """Verify indexes are created."""
        store = JobStore(tmp_db_path)
        store.initialize()

        conn = sqlite3.connect(str(tmp_db_path))
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        )
        index_names = {row[0] for row in cursor.fetchall()}
        conn.close()

        assert "idx_jobs_status" in index_names
        assert "idx_jobs_issue" in index_names
        assert "idx_jobs_worktree" in index_names
        assert "idx_jobs_session_key" in index_names

    def test_sets_schema_version(self, tmp_db_path: Path):
        """Verify schema version is set."""
        store = JobStore(tmp_db_path)
        store.initialize()

        conn = sqlite3.connect(str(tmp_db_path))
        cursor = conn.execute("SELECT version FROM schema_version")
        version = cursor.fetchone()[0]
        conn.close()

        assert version == SCHEMA_VERSION

    def test_idempotent(self, tmp_db_path: Path):
        """Verify initialize can be called multiple times."""
        store = JobStore(tmp_db_path)
        store.initialize()
        store.initialize()  # Should not raise

        # Verify still works
        conn = sqlite3.connect(str(tmp_db_path))
        cursor = conn.execute("SELECT COUNT(*) FROM publish_jobs")
        assert cursor.fetchone()[0] == 0
        conn.close()


# ============================================================================
# JobStore.save_job
# ============================================================================


class TestJobStoreSaveJob:
    """Tests for JobStore.save_job()."""

    def test_saves_job(self, job_store: JobStore, sample_job: PublishJob):
        """Verify job is saved to database."""
        job_store.save_job(sample_job)

        record = job_store.get_job(sample_job.job_id)
        assert record is not None
        assert record.job_id == sample_job.job_id
        assert record.issue_number == sample_job.issue_number
        assert record.session_key == sample_job.session_key

    def test_stores_worktree_id(
        self, job_store: JobStore, sample_job: PublishJob, sample_worktree: Path
    ):
        """Verify worktree ID is stored."""
        # Ensure worktree has an ID
        wt_id = ensure_worktree_id(sample_worktree)

        job_store.save_job(sample_job)

        record = job_store.get_job(sample_job.job_id)
        assert record.worktree_id == wt_id

    def test_stores_metadata(self, job_store: JobStore, sample_job: PublishJob):
        """Verify metadata is stored as JSON."""
        job_store.save_job(sample_job)

        record = job_store.get_job(sample_job.job_id)
        metadata = json.loads(record.metadata_json)

        assert metadata["issue_title"] == sample_job.issue_title
        assert metadata["outcome"] == sample_job.outcome
        assert metadata["agent_label"] == sample_job.agent_label
        assert set(metadata["requested_actions"]) == set(sample_job.requested_actions)

    def test_initial_status_is_queued(
        self, job_store: JobStore, sample_job: PublishJob
    ):
        """Verify initial status is 'queued'."""
        job_store.save_job(sample_job)

        record = job_store.get_job(sample_job.job_id)
        assert record.status == "queued"

    def test_sets_created_at(self, job_store: JobStore, sample_job: PublishJob):
        """Verify created_at timestamp is set."""
        before = time.time()
        job_store.save_job(sample_job)
        after = time.time()

        record = job_store.get_job(sample_job.job_id)
        assert before <= record.created_at <= after


# ============================================================================
# JobStore status transitions
# ============================================================================


class TestJobStoreMarkStarted:
    """Tests for JobStore.mark_started()."""

    def test_marks_queued_as_running(
        self, job_store: JobStore, sample_job: PublishJob
    ):
        """Verify queued job can be marked as running."""
        job_store.save_job(sample_job)

        result = job_store.mark_started(sample_job.job_id)

        assert result is True
        record = job_store.get_job(sample_job.job_id)
        assert record.status == "running"

    def test_sets_started_at(self, job_store: JobStore, sample_job: PublishJob):
        """Verify started_at timestamp is set."""
        job_store.save_job(sample_job)
        before = time.time()

        job_store.mark_started(sample_job.job_id)
        after = time.time()

        record = job_store.get_job(sample_job.job_id)
        assert record.started_at is not None
        assert before <= record.started_at <= after

    def test_returns_false_for_nonexistent(self, job_store: JobStore):
        """Verify returns False for nonexistent job."""
        result = job_store.mark_started("nonexistent-job-id")
        assert result is False

    def test_returns_false_for_running_job(
        self, job_store: JobStore, sample_job: PublishJob
    ):
        """Verify returns False if job is already running."""
        job_store.save_job(sample_job)
        job_store.mark_started(sample_job.job_id)

        result = job_store.mark_started(sample_job.job_id)
        assert result is False


class TestJobStoreMarkSucceeded:
    """Tests for JobStore.mark_succeeded()."""

    def test_marks_running_as_succeeded(
        self, job_store: JobStore, sample_job: PublishJob
    ):
        """Verify running job can be marked as succeeded."""
        job_store.save_job(sample_job)
        job_store.mark_started(sample_job.job_id)

        result = job_store.mark_succeeded(
            sample_job.job_id,
            pr_url="https://github.com/owner/repo/pull/42",
            pr_number=42,
        )

        assert result is True
        record = job_store.get_job(sample_job.job_id)
        assert record.status == "succeeded"
        assert record.pr_url == "https://github.com/owner/repo/pull/42"
        assert record.pr_number == 42

    def test_sets_finished_at(self, job_store: JobStore, sample_job: PublishJob):
        """Verify finished_at timestamp is set."""
        job_store.save_job(sample_job)
        job_store.mark_started(sample_job.job_id)
        before = time.time()

        job_store.mark_succeeded(sample_job.job_id)
        after = time.time()

        record = job_store.get_job(sample_job.job_id)
        assert record.finished_at is not None
        assert before <= record.finished_at <= after

    def test_can_succeed_from_queued(
        self, job_store: JobStore, sample_job: PublishJob
    ):
        """Verify queued job can be directly marked as succeeded."""
        job_store.save_job(sample_job)

        result = job_store.mark_succeeded(sample_job.job_id)

        assert result is True
        record = job_store.get_job(sample_job.job_id)
        assert record.status == "succeeded"


class TestJobStoreMarkFailed:
    """Tests for JobStore.mark_failed()."""

    def test_marks_running_as_failed(
        self, job_store: JobStore, sample_job: PublishJob
    ):
        """Verify running job can be marked as failed."""
        job_store.save_job(sample_job)
        job_store.mark_started(sample_job.job_id)

        result = job_store.mark_failed(
            sample_job.job_id,
            error_message="Git push failed",
            errors=["Network error", "Retry limit exceeded"],
        )

        assert result is True
        record = job_store.get_job(sample_job.job_id)
        assert record.status == "failed"
        assert record.error_message == "Git push failed"
        assert json.loads(record.errors_json) == ["Network error", "Retry limit exceeded"]

    def test_sets_finished_at(self, job_store: JobStore, sample_job: PublishJob):
        """Verify finished_at timestamp is set."""
        job_store.save_job(sample_job)
        before = time.time()

        job_store.mark_failed(sample_job.job_id, "Error")
        after = time.time()

        record = job_store.get_job(sample_job.job_id)
        assert record.finished_at is not None
        assert before <= record.finished_at <= after


class TestJobStoreMarkWorktreeCleaned:
    """Tests for JobStore.mark_worktree_cleaned()."""

    def test_marks_jobs_for_worktree(
        self, job_store: JobStore, sample_job: PublishJob, sample_worktree: Path
    ):
        """Verify all jobs for a worktree are marked as worktree_gone."""
        job_store.save_job(sample_job)

        count = job_store.mark_worktree_cleaned(str(sample_worktree))

        assert count == 1
        record = job_store.get_job(sample_job.job_id)
        assert record.status == "worktree_gone"

    def test_only_marks_non_terminal_jobs(
        self, job_store: JobStore, sample_job: PublishJob, sample_worktree: Path
    ):
        """Verify only non-terminal jobs are marked."""
        # Save and succeed a job
        job_store.save_job(sample_job)
        job_store.mark_succeeded(sample_job.job_id)

        count = job_store.mark_worktree_cleaned(str(sample_worktree))

        assert count == 0
        record = job_store.get_job(sample_job.job_id)
        assert record.status == "succeeded"

    def test_returns_zero_for_unknown_worktree(self, job_store: JobStore):
        """Verify returns 0 for unknown worktree."""
        count = job_store.mark_worktree_cleaned("/nonexistent/path")
        assert count == 0


# ============================================================================
# JobStore query methods
# ============================================================================


class TestJobStoreQueries:
    """Tests for JobStore query methods."""

    def test_get_pending_jobs(
        self, job_store: JobStore, sample_job: PublishJob, sample_worktree: Path
    ):
        """Verify get_pending_jobs returns queued jobs."""
        job_store.save_job(sample_job)

        pending = job_store.get_pending_jobs()

        assert len(pending) == 1
        assert pending[0].job_id == sample_job.job_id

    def test_get_running_jobs(
        self, job_store: JobStore, sample_job: PublishJob
    ):
        """Verify get_running_jobs returns running jobs."""
        job_store.save_job(sample_job)
        job_store.mark_started(sample_job.job_id)

        running = job_store.get_running_jobs()

        assert len(running) == 1
        assert running[0].status == "running"

    def test_get_active_jobs(
        self, job_store: JobStore, sample_worktree: Path
    ):
        """Verify get_active_jobs returns queued and running jobs."""
        # Create two jobs
        job1 = make_publish_job(
            sample_worktree,
            job_id="job-1",
            issue_number=1,
            session_key="code:1",
            branch_name="branch-1",
        )
        job2 = make_publish_job(
            sample_worktree,
            job_id="job-2",
            issue_number=2,
            session_key="code:2",
            branch_name="branch-2",
        )

        job_store.save_job(job1)
        job_store.save_job(job2)
        job_store.mark_started(job1.job_id)

        active = job_store.get_active_jobs()

        assert len(active) == 2
        assert {j.job_id for j in active} == {"job-1", "job-2"}

    def test_get_jobs_for_issue(
        self, job_store: JobStore, sample_job: PublishJob
    ):
        """Verify get_jobs_for_issue returns jobs for a specific issue."""
        job_store.save_job(sample_job)

        jobs = job_store.get_jobs_for_issue(sample_job.issue_number)

        assert len(jobs) == 1
        assert jobs[0].issue_number == sample_job.issue_number

    def test_get_recent_jobs(
        self, job_store: JobStore, sample_worktree: Path
    ):
        """Verify get_recent_jobs returns jobs in order."""
        for i in range(5):
            job = make_publish_job(
                sample_worktree,
                job_id=f"job-{i}",
                issue_number=i,
                session_key=f"code:{i}",
                branch_name=f"branch-{i}",
            )
            job_store.save_job(job)

        recent = job_store.get_recent_jobs(limit=3)

        assert len(recent) == 3
        # Most recent first (DESC order)
        assert recent[0].job_id == "job-4"

    def test_get_job_returns_none_for_nonexistent(self, job_store: JobStore):
        """Verify get_job returns None for nonexistent job."""
        result = job_store.get_job("nonexistent")
        assert result is None


# ============================================================================
# JobStore.validate_worktrees
# ============================================================================


class TestJobStoreValidateWorktrees:
    """Tests for JobStore.validate_worktrees()."""

    def test_marks_missing_worktree_as_gone(
        self, job_store: JobStore, sample_worktree: Path
    ):
        """Verify jobs with missing worktrees are marked as worktree_gone."""
        # Create a worktree, save a job, then delete the worktree
        worktree_path = sample_worktree / "to_be_deleted"
        worktree_path.mkdir(parents=True)

        job = make_publish_job(
            worktree_path,
            job_id="job-orphan",
            issue_number=123,
            session_key="code:123",
            branch_name="branch",
        )
        job_store.save_job(job)

        # Now delete the worktree to simulate orphan
        shutil.rmtree(worktree_path)

        count = job_store.validate_worktrees()

        assert count == 1
        record = job_store.get_job(job.job_id)
        assert record.status == "worktree_gone"
        assert "missing" in record.error_message.lower()

    def test_marks_identity_mismatch_as_gone(
        self, job_store: JobStore, sample_worktree: Path
    ):
        """Verify jobs with identity mismatch are marked as worktree_gone."""
        # Create job with a worktree
        job = make_publish_job(
            sample_worktree,
            job_id="job-mismatch",
            issue_number=123,
            session_key="code:123",
            branch_name="branch",
        )
        job_store.save_job(job)

        # Change the worktree identity (simulating worktree recreation)
        set_worktree_id(sample_worktree, "wt-different-id")

        count = job_store.validate_worktrees()

        assert count == 1
        record = job_store.get_job(job.job_id)
        assert record.status == "worktree_gone"
        assert "mismatch" in record.error_message.lower()

    def test_preserves_valid_worktrees(
        self, job_store: JobStore, sample_job: PublishJob
    ):
        """Verify jobs with valid worktrees are preserved."""
        job_store.save_job(sample_job)

        count = job_store.validate_worktrees()

        assert count == 0
        record = job_store.get_job(sample_job.job_id)
        assert record.status == "queued"

    def test_skips_terminal_jobs(
        self, job_store: JobStore, sample_job: PublishJob, sample_worktree: Path
    ):
        """Verify terminal jobs are not validated."""
        job_store.save_job(sample_job)
        job_store.mark_succeeded(sample_job.job_id)

        # Delete the worktree
        shutil.rmtree(sample_worktree)

        count = job_store.validate_worktrees()

        # Should not mark as gone since already succeeded
        assert count == 0


# ============================================================================
# JobStore.cleanup_old_jobs
# ============================================================================


class TestJobStoreCleanupOldJobs:
    """Tests for JobStore.cleanup_old_jobs()."""

    def test_deletes_old_terminal_jobs(
        self, job_store: JobStore, sample_worktree: Path, tmp_db_path: Path
    ):
        """Verify old terminal jobs are deleted."""
        job = make_publish_job(
            sample_worktree,
            job_id="old-job",
            issue_number=123,
            session_key="code:123",
            branch_name="branch",
        )
        job_store.save_job(job)
        job_store.mark_succeeded(job.job_id)

        # Manually set finished_at to 40 days ago
        conn = sqlite3.connect(str(tmp_db_path))
        old_time = time.time() - (40 * 86400)
        conn.execute(
            "UPDATE publish_jobs SET finished_at = ? WHERE job_id = ?",
            (old_time, job.job_id),
        )
        conn.commit()
        conn.close()

        count = job_store.cleanup_old_jobs(max_age_days=30)

        assert count == 1
        assert job_store.get_job(job.job_id) is None

    def test_preserves_recent_jobs(
        self, job_store: JobStore, sample_job: PublishJob
    ):
        """Verify recent jobs are preserved."""
        job_store.save_job(sample_job)
        job_store.mark_succeeded(sample_job.job_id)

        count = job_store.cleanup_old_jobs(max_age_days=30)

        assert count == 0
        assert job_store.get_job(sample_job.job_id) is not None

    def test_preserves_active_jobs(
        self, job_store: JobStore, sample_job: PublishJob
    ):
        """Verify active (non-terminal) jobs are preserved regardless of age."""
        job_store.save_job(sample_job)  # Status is queued

        count = job_store.cleanup_old_jobs(max_age_days=0)  # Delete all "old" jobs

        # Should not delete because job is not terminal
        assert count == 0
        assert job_store.get_job(sample_job.job_id) is not None


# ============================================================================
# JobRecord properties
# ============================================================================


class TestJobRecordProperties:
    """Tests for JobRecord dataclass properties."""

    def test_duration_seconds(self):
        """Verify duration_seconds calculation."""
        record = JobRecord(
            job_id="job-1",
            issue_number=1,
            session_key="code:1",
            worktree_path="/path",
            worktree_id="wt-123",
            branch_name="branch",
            status="succeeded",
            created_at=1000.0,
            started_at=1010.0,
            finished_at=1030.0,
        )

        assert record.duration_seconds == 20.0

    def test_duration_seconds_none_if_not_finished(self):
        """Verify duration_seconds is None if not finished."""
        record = JobRecord(
            job_id="job-1",
            issue_number=1,
            session_key="code:1",
            worktree_path="/path",
            worktree_id="wt-123",
            branch_name="branch",
            status="running",
            created_at=1000.0,
            started_at=1010.0,
        )

        assert record.duration_seconds is None

    def test_is_terminal(self):
        """Verify is_terminal property."""
        terminal_statuses = ["succeeded", "failed", "worktree_gone"]
        non_terminal_statuses = ["queued", "running"]

        for status in terminal_statuses:
            record = JobRecord(
                job_id="job-1",
                issue_number=1,
                session_key="code:1",
                worktree_path="/path",
                worktree_id="wt-123",
                branch_name="branch",
                status=status,
                created_at=1000.0,
            )
            assert record.is_terminal is True, f"{status} should be terminal"

        for status in non_terminal_statuses:
            record = JobRecord(
                job_id="job-1",
                issue_number=1,
                session_key="code:1",
                worktree_path="/path",
                worktree_id="wt-123",
                branch_name="branch",
                status=status,
                created_at=1000.0,
            )
            assert record.is_terminal is False, f"{status} should not be terminal"

    def test_is_resumable(self):
        """Verify is_resumable property."""
        resumable = ["queued", "running"]
        not_resumable = ["succeeded", "failed", "worktree_gone"]

        for status in resumable:
            record = JobRecord(
                job_id="job-1",
                issue_number=1,
                session_key="code:1",
                worktree_path="/path",
                worktree_id="wt-123",
                branch_name="branch",
                status=status,
                created_at=1000.0,
            )
            assert record.is_resumable is True, f"{status} should be resumable"

        for status in not_resumable:
            record = JobRecord(
                job_id="job-1",
                issue_number=1,
                session_key="code:1",
                worktree_path="/path",
                worktree_id="wt-123",
                branch_name="branch",
                status=status,
                created_at=1000.0,
            )
            assert record.is_resumable is False, f"{status} should not be resumable"


# ============================================================================
# Thread safety
# ============================================================================


class TestJobStoreThreadSafety:
    """Tests for JobStore thread safety."""

    def test_concurrent_saves(self, tmp_db_path: Path, sample_worktree: Path):
        """Verify concurrent saves don't corrupt data."""
        store = JobStore(tmp_db_path)
        store.initialize()

        num_threads = 10
        jobs_per_thread = 20
        errors = []

        def save_jobs(thread_id: int):
            try:
                for i in range(jobs_per_thread):
                    issue_number = thread_id * 100 + i
                    job = make_publish_job(
                        sample_worktree,
                        job_id=f"job-{thread_id}-{i}",
                        issue_number=issue_number,
                        session_key=f"code:{thread_id}-{i}",
                        branch_name=f"branch-{thread_id}-{i}",
                    )
                    store.save_job(job)
            except Exception as e:
                errors.append(e)

        threads = [
            threading.Thread(target=save_jobs, args=(i,))
            for i in range(num_threads)
        ]

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0, f"Errors occurred: {errors}"

        # Verify all jobs were saved
        recent = store.get_recent_jobs(limit=1000)
        assert len(recent) == num_threads * jobs_per_thread

    def test_concurrent_updates(self, job_store: JobStore, sample_worktree: Path):
        """Verify concurrent status updates are handled correctly."""
        # Create a job
        job = make_publish_job(
            sample_worktree,
            job_id="concurrent-job",
            issue_number=123,
            session_key="code:123",
            branch_name="branch",
        )
        job_store.save_job(job)

        results = []

        def try_start():
            result = job_store.mark_started(job.job_id)
            results.append(result)

        threads = [threading.Thread(target=try_start) for _ in range(10)]

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Only one should succeed
        assert sum(results) == 1


# ============================================================================
# get_default_db_path
# ============================================================================


class TestGetDefaultDbPath:
    """Tests for get_default_db_path function."""

    def test_returns_expected_path(self, tmp_path: Path):
        """Verify returns expected path structure."""
        result = get_default_db_path(tmp_path)

        expected = tmp_path / ".issue-orchestrator" / "state" / "publish_jobs.db"
        assert result == expected


# ============================================================================
# Schema migration
# ============================================================================


class TestSchemaMigration:
    """Tests for schema migration (v1 -> v2)."""

    def test_migrates_v1_to_v2(self, tmp_db_path: Path):
        """Verify v1 schema is migrated to v2 (adds worktree_id column)."""
        # Create v1 schema manually (without worktree_id column)
        tmp_db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(tmp_db_path))
        conn.execute("""
            CREATE TABLE publish_jobs (
                job_id TEXT PRIMARY KEY,
                issue_number INTEGER NOT NULL,
                session_key TEXT NOT NULL,
                worktree_path TEXT NOT NULL,
                branch_name TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'queued',
                created_at REAL NOT NULL,
                started_at REAL,
                finished_at REAL,
                pr_url TEXT,
                pr_number INTEGER,
                error_message TEXT,
                errors_json TEXT,
                metadata_json TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE schema_version (version INTEGER PRIMARY KEY)
        """)
        conn.execute("INSERT INTO schema_version (version) VALUES (1)")
        conn.commit()
        conn.close()

        # Initialize should migrate
        store = JobStore(tmp_db_path)
        store.initialize()

        # Verify worktree_id column exists
        conn = sqlite3.connect(str(tmp_db_path))
        cursor = conn.execute("PRAGMA table_info(publish_jobs)")
        columns = {row[1] for row in cursor.fetchall()}
        conn.close()

        assert "worktree_id" in columns
