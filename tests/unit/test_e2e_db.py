"""Unit tests for E2E database layer."""

import pytest
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from issue_orchestrator.infra.e2e_db import E2EDB, AlreadyRunning, E2ERun


class TestE2EDB:
    """Test the E2EDB SQLite layer."""

    @pytest.fixture
    def db(self, tmp_path: Path) -> E2EDB:
        """Create a fresh E2EDB instance for each test."""
        db_path = tmp_path / "test_e2e.db"
        return E2EDB(db_path)

    def test_db_creation(self, db: E2EDB):
        """Test that DB is created and usable."""
        # Verify DB is ready by successfully starting and querying a run
        run_id = db.start_run(
            repo_root="/test/repo",
            orchestrator_id="test-orch",
            pytest_args=["tests/e2e"],
        )
        assert run_id >= 1

        # Verify we can query the run back
        runs = db.list_runs(orchestrator_id="test-orch", limit=10)
        assert len(runs) == 1
        assert runs[0].id == run_id

    def test_start_run(self, db: E2EDB):
        """Test starting a new E2E run."""
        run_id = db.start_run(
            repo_root="/test/repo",
            orchestrator_id="test-orch",
            pytest_args=["tests/e2e", "-v"],
            commit_sha="abc123",
            branch="main",
        )

        assert run_id == 1

        # Verify run is in DB
        run = db.latest_run("test-orch")
        assert run is not None
        assert run.status == "running"
        assert run.orchestrator_id == "test-orch"
        assert run.commit_sha == "abc123"
        assert run.branch == "main"

    def test_start_run_prevents_concurrent(self, db: E2EDB):
        """Test that starting a run while another is running raises AlreadyRunning."""
        db.start_run("/test/repo", "test-orch", ["tests/e2e"], None, None)

        with pytest.raises(AlreadyRunning) as exc_info:
            db.start_run("/test/repo", "test-orch", ["tests/e2e"], None, None)

        assert exc_info.value.orchestrator_id == "test-orch"

    def test_different_orchestrators_can_run_concurrently(self, db: E2EDB):
        """Test that different orchestrators can have concurrent runs."""
        run1 = db.start_run("/test/repo", "orch-1", ["tests/e2e"], None, None)
        run2 = db.start_run("/test/repo", "orch-2", ["tests/e2e"], None, None)

        assert run1 != run2

        # Both should be running
        latest1 = db.latest_run("orch-1")
        latest2 = db.latest_run("orch-2")
        assert latest1.status == "running"
        assert latest2.status == "running"

    def test_finish_run(self, db: E2EDB):
        """Test finishing a run."""
        run_id = db.start_run("/test/repo", "test-orch", ["tests/e2e"], None, None)

        db.finish_run(
            run_id=run_id,
            status="passed",
            exit_code=0,
            note="All tests passed",
        )

        run = db.latest_run("test-orch")
        assert run.status == "passed"
        assert run.exit_code == 0
        assert run.note == "All tests passed"

    def test_upsert_test_result(self, db: E2EDB):
        """Test inserting and updating test results."""
        run_id = db.start_run("/test/repo", "test-orch", ["tests/e2e"], None, None)

        # Insert a test result
        db.upsert_test_result(
            run_id=run_id,
            nodeid="tests/e2e/test_login.py::test_login_success",
            outcome="passed",
            duration_seconds=1.5,
            longrepr=None,
        )

        # Verify result is in DB
        details = db.run_details(run_id)
        assert details is not None
        results = details.get("results", [])
        assert len(results) == 1
        assert results[0]["nodeid"] == "tests/e2e/test_login.py::test_login_success"
        assert results[0]["outcome"] == "passed"
        assert results[0]["duration_seconds"] == 1.5

    def test_upsert_test_result_update(self, db: E2EDB):
        """Test that upsert updates existing results."""
        run_id = db.start_run("/test/repo", "test-orch", ["tests/e2e"], None, None)

        # Insert initial result
        db.upsert_test_result(run_id, "test::foo", "passed", 1.0, None)

        # Update with new outcome
        db.upsert_test_result(run_id, "test::foo", "failed", 2.0, "AssertionError")

        # Should only have one result
        details = db.run_details(run_id)
        results = details["results"]
        assert len(results) == 1
        assert results[0]["outcome"] == "failed"
        assert results[0]["duration_seconds"] == 2.0
        assert results[0]["longrepr"] == "AssertionError"

    def test_get_failed_tests(self, db: E2EDB):
        """Test retrieving failed tests (excludes quarantined and retried-passed)."""
        run_id = db.start_run("/test/repo", "test-orch", ["tests/e2e"], None, None)

        # Add mixed results
        db.upsert_test_result(run_id, "test::pass1", "passed", 1.0, None)
        db.upsert_test_result(run_id, "test::fail1", "failed", 2.0, "Error 1")
        db.upsert_test_result(run_id, "test::pass2", "passed", 1.5, None)
        db.upsert_test_result(run_id, "test::fail2", "failed", 3.0, "Error 2")
        # Error outcome is not included in get_failed_tests (only 'failed')
        db.upsert_test_result(run_id, "test::error1", "error", 0.5, "Error 3")

        failed = db.get_failed_tests(run_id)

        # Only 'failed' outcomes (not 'error') are returned
        assert len(failed) == 2
        nodeids = {t.nodeid for t in failed}
        assert nodeids == {"test::fail1", "test::fail2"}

    def test_list_runs(self, db: E2EDB):
        """Test listing runs with limit."""
        # Create several runs
        for i in range(5):
            run_id = db.start_run(
                f"/test/repo{i}", "test-orch", ["tests/e2e"], f"sha{i}", "main"
            )
            db.finish_run(run_id, "passed", exit_code=0)

        # Get with default limit
        runs = db.list_runs("test-orch")
        assert len(runs) == 5

        # Get with limit
        runs = db.list_runs("test-orch", limit=3)
        assert len(runs) == 3

    def test_cancel_running(self, db: E2EDB):
        """Test canceling a running test."""
        run_id = db.start_run("/test/repo", "test-orch", ["tests/e2e"], None, None)

        db.cancel_running("test-orch")

        run = db.latest_run("test-orch")
        assert run.status == "canceled"
        assert run.finished_at is not None

    def test_compute_signal_score(self, db: E2EDB):
        """Test computing signal score from run history."""
        # Create runs with mixed results
        for i, status in enumerate(["passed", "passed", "failed", "passed", "passed"]):
            run_id = db.start_run(f"/repo{i}", "test-orch", ["tests/e2e"], None, None)
            db.finish_run(run_id, status, exit_code=0 if status == "passed" else 1)

        score = db.compute_signal_score("test-orch")

        assert score["runs_analyzed"] == 5
        assert score["pass_rate"] == 0.8  # 4/5

    def test_latest_run_returns_none_for_unknown(self, db: E2EDB):
        """Test that latest_run returns None for unknown orchestrator."""
        run = db.latest_run("unknown-orch")
        assert run is None

    def test_run_details_returns_none_for_unknown(self, db: E2EDB):
        """Test that run_details returns None for unknown run_id."""
        details = db.run_details(9999)
        assert details is None

    def test_db_path_creates_parent_dirs(self, tmp_path: Path):
        """Test that E2EDB creates parent directories if needed."""
        db_path = tmp_path / "subdir" / "nested" / "e2e.db"
        db = E2EDB(db_path)

        assert db_path.exists()

    def test_start_run_with_retry_of(self, db: E2EDB):
        """Test starting a retry run linked to original."""
        run1 = db.start_run("/test/repo", "test-orch", ["tests/e2e"], None, None)
        db.finish_run(run1, "failed", exit_code=1)

        # Start retry
        run2 = db.start_run("/test/repo", "test-orch", ["tests/e2e"], None, None, retry_of=run1)

        details = db.run_details(run2)
        assert details["run"]["retry_of"] == run1

    def test_start_run_with_worker_pid(self, db: E2EDB):
        """Test starting a run with worker_pid."""
        run_id = db.start_run(
            repo_root="/test/repo",
            orchestrator_id="test-orch",
            pytest_args=["tests/e2e"],
            worker_pid=12345,
        )

        run = db.latest_run("test-orch")
        assert run.worker_pid == 12345

    def test_update_worker_pid(self, db: E2EDB):
        """Test updating worker_pid after run creation."""
        run_id = db.start_run("/test/repo", "test-orch", ["tests/e2e"])

        db.update_worker_pid(run_id, 67890)

        run = db.latest_run("test-orch")
        assert run.worker_pid == 67890

    def test_orphan_detection_marks_interrupted(self, db: E2EDB, monkeypatch):
        """Test that orphaned runs (dead PID) are marked as interrupted."""
        # Start a run with a PID
        run1 = db.start_run("/test/repo", "test-orch", ["tests/e2e"], worker_pid=99999)

        # Mock _is_process_alive to return False (process is dead)
        monkeypatch.setattr(db, "_is_process_alive", lambda pid: False)

        # Starting a new run should mark the orphaned run as interrupted
        run2 = db.start_run("/test/repo", "test-orch", ["tests/e2e"], worker_pid=88888)

        assert run2 != run1

        # Check that run1 is now interrupted
        details1 = db.run_details(run1)
        assert details1["run"]["status"] == "interrupted"
        assert "died" in details1["run"]["note"].lower()

        # Check that run2 is running
        details2 = db.run_details(run2)
        assert details2["run"]["status"] == "running"

    def test_orphan_detection_alive_process_raises(self, db: E2EDB, monkeypatch):
        """Test that running run with alive process still raises AlreadyRunning."""
        run1 = db.start_run("/test/repo", "test-orch", ["tests/e2e"], worker_pid=99999)

        # Mock _is_process_alive to return True (process is alive)
        monkeypatch.setattr(db, "_is_process_alive", lambda pid: True)

        with pytest.raises(AlreadyRunning):
            db.start_run("/test/repo", "test-orch", ["tests/e2e"], worker_pid=88888)

    def test_get_passed_nodeids(self, db: E2EDB):
        """Test getting nodeids that passed in a run."""
        run_id = db.start_run("/test/repo", "test-orch", ["tests/e2e"])

        # Add mixed results
        db.upsert_test_result(run_id, "test::pass1", "passed", 1.0, None)
        db.upsert_test_result(run_id, "test::pass2", "passed", 1.5, None)
        db.upsert_test_result(run_id, "test::fail1", "failed", 2.0, "Error")
        db.upsert_test_result(run_id, "test::skip1", "skipped", 0.0, None)

        passed = db.get_passed_nodeids(run_id)

        assert passed == {"test::pass1", "test::pass2"}

    def test_get_interrupted_run(self, db: E2EDB):
        """Test getting the latest interrupted run."""
        # No interrupted runs yet
        assert db.get_interrupted_run("test-orch") is None

        # Create and interrupt a run
        run1 = db.start_run("/test/repo", "test-orch", ["tests/e2e"])
        with db._connect() as conn:
            conn.execute(
                "UPDATE e2e_runs SET status = 'interrupted' WHERE id = ?",
                (run1,),
            )

        interrupted = db.get_interrupted_run("test-orch")
        assert interrupted is not None
        assert interrupted.id == run1
        assert interrupted.status == "interrupted"

    def test_resume_run(self, db: E2EDB):
        """Test resuming an interrupted run."""
        # Create and interrupt a run
        run_id = db.start_run("/test/repo", "test-orch", ["tests/e2e"], worker_pid=11111)
        with db._connect() as conn:
            conn.execute(
                "UPDATE e2e_runs SET status = 'interrupted' WHERE id = ?",
                (run_id,),
            )

        # Resume it
        result = db.resume_run(run_id, worker_pid=22222)

        assert result is True

        run = db.latest_run("test-orch")
        assert run.status == "running"
        assert run.worker_pid == 22222
        assert "Resumed" in run.note

    def test_resume_run_fails_for_non_interrupted(self, db: E2EDB):
        """Test that resume_run fails for runs that aren't interrupted."""
        run_id = db.start_run("/test/repo", "test-orch", ["tests/e2e"])

        # Try to resume a running run
        result = db.resume_run(run_id, worker_pid=99999)

        assert result is False

        # Status should still be running
        run = db.latest_run("test-orch")
        assert run.status == "running"

    def test_resume_run_fails_for_unknown_run(self, db: E2EDB):
        """Test that resume_run fails for unknown run_id."""
        result = db.resume_run(9999, worker_pid=12345)
        assert result is False

    def test_update_progress_total_tests(self, db: E2EDB):
        """Test updating total_tests for a run."""
        run_id = db.start_run("/test/repo", "test-orch", ["tests/e2e"])

        db.update_progress(run_id, total_tests=42)

        run = db.latest_run("test-orch")
        assert run.total_tests == 42

    def test_update_progress_current_test(self, db: E2EDB):
        """Test updating current_test for a run."""
        run_id = db.start_run("/test/repo", "test-orch", ["tests/e2e"])

        db.update_progress(run_id, current_test="tests/e2e/test_foo.py::test_bar")

        run = db.latest_run("test-orch")
        assert run.current_test == "tests/e2e/test_foo.py::test_bar"

    def test_get_progress(self, db: E2EDB):
        """Test getting progress stats for a run."""
        run_id = db.start_run("/test/repo", "test-orch", ["tests/e2e"])

        # Set total tests
        db.update_progress(run_id, total_tests=10, current_test="test::current")

        # Add some results
        db.upsert_test_result(run_id, "test::pass1", "passed", 1.0, None)
        db.upsert_test_result(run_id, "test::pass2", "passed", 1.0, None)
        db.upsert_test_result(run_id, "test::fail1", "failed", 2.0, "Error")
        db.upsert_test_result(run_id, "test::skip1", "skipped", 0.0, None)

        progress = db.get_progress(run_id)

        assert progress["total_tests"] == 10
        assert progress["completed"] == 4
        assert progress["passed"] == 2
        assert progress["failed"] == 1
        assert progress["skipped"] == 1
        assert progress["current_test"] == "test::current"
        assert progress["percent"] == 40  # 4/10 * 100

    def test_get_progress_unknown_run(self, db: E2EDB):
        """Test get_progress returns empty dict for unknown run."""
        progress = db.get_progress(9999)
        assert progress == {}

    def test_get_test_summary(self, db: E2EDB):
        """Test comprehensive test summary including retries and quarantine."""
        run_id = db.start_run("/test/repo", "test-orch", ["tests/e2e"])

        # Add various test results
        db.upsert_test_result(run_id, "test::pass1", "passed", 1.0, None)
        db.upsert_test_result(run_id, "test::pass2", "passed", 1.5, None)
        db.upsert_test_result(run_id, "test::fail1", "failed", 2.0, "Error 1")
        db.upsert_test_result(run_id, "test::fail2", "failed", 2.5, "Error 2")
        db.upsert_test_result(run_id, "test::skip1", "skipped", 0.0, None)

        # Add a test that failed but passed on retry
        db.upsert_test_result(run_id, "test::flaky", "failed", 1.0, "Flaky error")
        db.update_retry_outcome(run_id, "test::flaky", "passed")

        # Add a quarantined test
        db.upsert_test_result(
            run_id, "test::quarantined", "failed", 1.0, "Known issue",
            is_quarantined=True
        )

        summary = db.get_test_summary(run_id)

        # Check counts
        assert summary["counts"]["total"] == 7
        assert summary["counts"]["passed"] == 2
        assert summary["counts"]["failed"] == 2  # fail1 and fail2
        assert summary["counts"]["passed_on_retry"] == 1  # flaky
        assert summary["counts"]["quarantined"] == 1
        assert summary["counts"]["skipped"] == 1

        # Check lists
        assert len(summary["passed"]) == 2
        assert len(summary["failed"]) == 2
        assert len(summary["passed_on_retry"]) == 1
        assert summary["passed_on_retry"][0]["nodeid"] == "test::flaky"
        assert len(summary["quarantined"]) == 1
        assert summary["quarantined"][0]["nodeid"] == "test::quarantined"
        assert len(summary["skipped"]) == 1

    def test_compute_signal_score_with_quarantine(self, db: E2EDB):
        """Test signal score includes quarantine count."""
        # Create runs with mixed results
        for i, status in enumerate(["passed", "passed", "failed", "passed"]):
            run_id = db.start_run(f"/repo{i}", "test-orch", ["tests/e2e"])

            # Add a quarantined test to latest run
            if i == 3:
                db.upsert_test_result(
                    run_id, "test::quarantined1", "failed", 1.0, "Known",
                    is_quarantined=True
                )
                db.upsert_test_result(
                    run_id, "test::quarantined2", "failed", 1.0, "Known",
                    is_quarantined=True
                )

            db.finish_run(run_id, status, exit_code=0 if status == "passed" else 1)

        score = db.compute_signal_score("test-orch")

        assert score["runs_analyzed"] == 4
        assert score["pass_rate"] == 0.75  # 3/4
        assert score["quarantined_count"] == 2  # From the latest run
