"""Unit tests for E2E database layer."""

import json
import pytest
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from issue_orchestrator.infra.e2e_db import (
    E2EDB,
    AlreadyRunning,
    E2ERun,
    TestStability,
    _categorize_test,
    _compute_stability,
    load_quarantine_list,
    save_quarantine_list,
)
from issue_orchestrator.infra.e2e_reports import E2ERunArtifactRecord, JUnitCaseResult


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
            command=["pytest", "tests/e2e", "-v"],
            runner_kind="pytest",
        )

        assert run_id == 1

        # Verify run is in DB
        run = db.latest_run("test-orch")
        assert run is not None
        assert run.status == "running"
        assert run.orchestrator_id == "test-orch"
        assert run.commit_sha == "abc123"
        assert run.branch == "main"
        assert run.command == ["pytest", "tests/e2e", "-v"]
        assert run.runner_kind == "pytest"

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
            display_name="test_login_success",
            suite_name="tests.e2e.test_login",
            result_source="junit_xml",
        )

        # Verify result is in DB
        details = db.run_details(run_id)
        assert details is not None
        results = details.get("results", [])
        assert len(results) == 1
        assert results[0]["nodeid"] == "tests/e2e/test_login.py::test_login_success"
        assert results[0]["case_id"] == "tests/e2e/test_login.py::test_login_success"
        assert results[0]["label"] == "test_login_success"
        assert results[0]["display_name"] == "test_login_success"
        assert results[0]["suite_name"] == "tests.e2e.test_login"
        assert results[0]["result_source"] == "junit_xml"
        assert results[0]["outcome"] == "passed"
        assert results[0]["duration_seconds"] == 1.5

    def test_replace_run_artifacts(self, db: E2EDB):
        """Run-scoped artifacts should round-trip through the DB facade."""
        run_id = db.start_run("/test/repo", "test-orch", ["tests/e2e"], None, None)

        db.replace_run_artifacts(
            run_id,
            [
                E2ERunArtifactRecord(
                    kind="raw_log",
                    label="Raw Output",
                    path="/tmp/run.log",
                ),
                E2ERunArtifactRecord(
                    kind="junit_xml",
                    label="JUnit XML",
                    path="/tmp/junit.xml",
                ),
            ],
        )

        details = db.run_details(run_id)
        artifacts = details["artifacts"]
        assert [artifact["kind"] for artifact in artifacts] == ["junit_xml", "raw_log"]
        assert artifacts[0]["path"] == "/tmp/junit.xml"

    def test_record_junit_cases(self, db: E2EDB):
        """Parsed JUnit cases should populate generic result metadata."""
        run_id = db.start_run("/test/repo", "test-orch", ["tests/e2e"], None, None)

        db.record_junit_cases(
            run_id,
            [
                JUnitCaseResult(
                    case_id="suite::test_passes",
                    display_name="test_passes",
                    suite_name="suite",
                    outcome="passed",
                    duration_seconds=1.2,
                ),
                JUnitCaseResult(
                    case_id="suite::test_fails",
                    display_name="test_fails",
                    suite_name="suite",
                    outcome="failed",
                    duration_seconds=2.4,
                    failure_details="AssertionError\nexpected 1",
                ),
            ],
        )

        details = db.run_details(run_id)
        results = details["results"]
        by_case_id = {result["case_id"]: result for result in results}
        assert by_case_id["suite::test_passes"]["display_name"] == "test_passes"
        assert by_case_id["suite::test_passes"]["result_source"] == "junit_xml"
        assert by_case_id["suite::test_fails"]["failure_summary"] == "AssertionError"

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

    def test_get_run(self, db: E2EDB):
        """Test getting a run by ID."""
        run_id = db.start_run("/test/repo", "test-orch", ["tests/e2e"], "abc123", "main")

        run = db.get_run(run_id)
        assert run is not None
        assert run.id == run_id
        assert run.orchestrator_id == "test-orch"
        assert run.commit_sha == "abc123"
        assert run.branch == "main"

    def test_get_run_returns_none_for_unknown(self, db: E2EDB):
        """Test that get_run returns None for unknown run_id."""
        run = db.get_run(9999)
        assert run is None

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
        # noqa: SLF001 - Direct DB access needed to simulate interrupted state
        with db._connect() as conn:  # noqa: SLF001
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
        with db._connect() as conn:  # noqa: SLF001
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

    def test_get_all_open_failure_issues_empty(self, db: E2EDB):
        """Test getting open failure issues when none exist."""
        issues = db.get_all_open_failure_issues()
        assert issues == []

    def test_get_all_open_failure_issues(self, db: E2EDB):
        """Test getting all unresolved failure issues."""
        run_id = db.start_run("/repo", "test-orch", ["tests/e2e"], commit_sha="abc123")
        db.finish_run(run_id, "failed")

        # Record some failure issues
        db.record_failure_issue(
            nodeid="test::failure1",
            github_issue_number=101,
            parent_issue_number=100,
            first_failing_run_id=run_id,
            first_failing_sha="abc123",
        )
        db.record_failure_issue(
            nodeid="test::failure2",
            github_issue_number=102,
            parent_issue_number=100,
            first_failing_run_id=run_id,
            first_failing_sha="abc123",
        )
        db.record_failure_issue(
            nodeid="test::failure3",
            github_issue_number=103,
            parent_issue_number=100,
            first_failing_run_id=run_id,
            first_failing_sha="abc123",
        )

        # Resolve one issue
        db.resolve_failure_issue("test::failure2", "passed")

        # Get open issues
        issues = db.get_all_open_failure_issues()

        assert len(issues) == 2
        nodeids = {i.nodeid for i in issues}
        assert nodeids == {"test::failure1", "test::failure3"}

    def test_resolve_failure_issue(self, db: E2EDB):
        """Test resolving a failure issue."""
        run_id = db.start_run("/repo", "test-orch", ["tests/e2e"], commit_sha="abc123")
        db.finish_run(run_id, "failed")

        db.record_failure_issue(
            nodeid="test::failure",
            github_issue_number=101,
            parent_issue_number=100,
            first_failing_run_id=run_id,
            first_failing_sha="abc123",
        )

        # Issue should be open initially
        issue = db.find_open_failure_issue("test::failure")
        assert issue is not None
        assert issue.resolution is None

        # Resolve it
        result = db.resolve_failure_issue("test::failure", "passed")
        assert result is True

        # Should no longer be open
        issue = db.find_open_failure_issue("test::failure")
        assert issue is None

        # Resolving again should return False (no open issue)
        result = db.resolve_failure_issue("test::failure", "passed")
        assert result is False

    def test_get_unresolved_failure_count(self, db: E2EDB):
        """Test counting unresolved failures for a parent issue."""
        run_id = db.start_run("/repo", "test-orch", ["tests/e2e"], commit_sha="abc123")
        db.finish_run(run_id, "failed")

        # Record failure issues under parent #100
        db.record_failure_issue(
            nodeid="test::failure1",
            github_issue_number=101,
            parent_issue_number=100,
            first_failing_run_id=run_id,
            first_failing_sha="abc123",
        )
        db.record_failure_issue(
            nodeid="test::failure2",
            github_issue_number=102,
            parent_issue_number=100,
            first_failing_run_id=run_id,
            first_failing_sha="abc123",
        )

        # Should have 2 unresolved
        assert db.get_unresolved_failure_count(100) == 2

        # Resolve one
        db.resolve_failure_issue("test::failure1", "passed")
        assert db.get_unresolved_failure_count(100) == 1

        # Resolve the other
        db.resolve_failure_issue("test::failure2", "passed")
        assert db.get_unresolved_failure_count(100) == 0


class TestFlipRateStability:
    """Test flip-rate stability detection via _compute_stability and DB methods."""

    # --- Pure function tests for _compute_stability ---

    def test_empty_outcomes(self):
        """Empty outcomes -> healthy, zero flip rate."""
        result = _compute_stability("test::foo", [], threshold_percent=20.0)
        assert result.category == "healthy"
        assert result.flip_rate == 0.0
        assert result.flip_count == 0
        assert result.run_count == 0
        assert result.is_likely_flaky is False

    def test_all_pass(self):
        """All passes -> healthy, zero flip rate."""
        outcomes = ["passed"] * 5
        result = _compute_stability("test::foo", outcomes, threshold_percent=20.0)
        assert result.category == "healthy"
        assert result.flip_rate == 0.0
        assert result.flip_count == 0
        assert result.is_likely_flaky is False

    def test_all_fail(self):
        """All failures -> consistently_failing, zero flip rate."""
        outcomes = ["failed"] * 5
        result = _compute_stability("test::foo", outcomes, threshold_percent=20.0)
        assert result.category == "consistently_failing"
        assert result.flip_rate == 0.0
        assert result.flip_count == 0
        assert result.is_likely_flaky is False

    def test_alternating_is_flaky(self):
        """Alternating pass/fail -> flaky with 100% flip rate."""
        outcomes = ["passed", "failed", "passed", "failed", "passed"]
        result = _compute_stability("test::foo", outcomes, threshold_percent=20.0)
        assert result.category == "flaky"
        assert result.flip_rate == 1.0
        assert result.flip_count == 4
        assert result.is_likely_flaky is True
        assert result.flip_rate_percent == 100.0

    def test_boundary_threshold(self):
        """Exactly at threshold boundary should be flaky."""
        # 2 flips out of 9 transitions = 22.2% > 20% threshold
        outcomes = ["passed", "failed", "failed", "failed", "passed", "passed", "passed", "passed", "passed", "passed"]
        result = _compute_stability("test::foo", outcomes, threshold_percent=20.0)
        assert result.is_likely_flaky is True
        assert result.flip_count == 2

    def test_below_threshold_not_flaky(self):
        """Below threshold should not be flaky."""
        # 1 flip out of 9 transitions = 11.1% < 20% threshold
        outcomes = ["passed", "failed", "failed", "failed", "failed", "failed", "failed", "failed", "failed", "failed"]
        result = _compute_stability("test::foo", outcomes, threshold_percent=20.0)
        assert result.is_likely_flaky is False
        assert result.flip_count == 1

    def test_recovered(self):
        """Recent pass after failures -> recovered."""
        # 2 flips / 4 transitions = 50% flip rate; threshold 60% -> not flaky
        outcomes = ["passed", "failed", "failed", "passed", "passed"]
        result = _compute_stability("test::foo", outcomes, threshold_percent=60.0)
        assert result.category == "recovered"

    def test_new_failure_short_history(self):
        """< 3 runs with recent failure -> new_failure."""
        # 2 runs, 0 flips (both failed), not flaky
        outcomes = ["failed", "failed"]
        result = _compute_stability("test::foo", outcomes, threshold_percent=20.0)
        assert result.category == "new_failure"

    def test_single_failure(self):
        """Single failure run -> new_failure."""
        outcomes = ["failed"]
        result = _compute_stability("test::foo", outcomes, threshold_percent=20.0)
        assert result.category == "new_failure"

    def test_single_pass(self):
        """Single pass run -> healthy."""
        outcomes = ["passed"]
        result = _compute_stability("test::foo", outcomes, threshold_percent=20.0)
        assert result.category == "healthy"

    # --- Pure function tests for _categorize_test ---

    def test_categorize_empty(self):
        assert _categorize_test([], False) == "healthy"

    def test_categorize_flaky(self):
        assert _categorize_test(["passed", "failed"], True) == "flaky"

    def test_categorize_consistently_failing(self):
        assert _categorize_test(["failed", "failed", "failed"], False) == "consistently_failing"

    def test_categorize_new_failure(self):
        assert _categorize_test(["failed", "passed"], False) == "new_failure"

    def test_categorize_recovered(self):
        assert _categorize_test(["passed", "failed", "passed"], False) == "recovered"

    def test_categorize_healthy(self):
        assert _categorize_test(["passed", "passed", "passed"], False) == "healthy"

    # --- TestStability.to_dict ---

    def test_to_dict(self):
        s = TestStability(
            nodeid="test::foo",
            flip_rate=0.5,
            flip_count=3,
            run_count=7,
            category="flaky",
            is_likely_flaky=True,
            recent_outcomes=["passed", "failed", "passed"],
        )
        d = s.to_dict()
        assert d["nodeid"] == "test::foo"
        assert d["flip_rate"] == 0.5
        assert d["flip_rate_percent"] == 50.0
        assert d["flip_count"] == 3
        assert d["category"] == "flaky"
        assert d["is_likely_flaky"] is True

    # --- DB integration tests ---

    @pytest.fixture
    def db(self, tmp_path: Path) -> E2EDB:
        return E2EDB(tmp_path / "test_e2e.db")

    def _create_run_with_result(
        self, db: E2EDB, nodeid: str, outcome: str, run_num: int
    ) -> int:
        """Helper to create a completed run with a single test result."""
        run_id = db.start_run(
            repo_root=f"/repo{run_num}",
            orchestrator_id="test-orch",
            pytest_args=["tests/e2e"],
        )
        db.upsert_test_result(run_id, nodeid, outcome)
        db.finish_run(run_id, "passed" if outcome == "passed" else "failed")
        return run_id

    def test_get_test_stability_from_db(self, db: E2EDB):
        """Integration test: get_test_stability with actual DB data."""
        # Create alternating pass/fail runs
        for i, outcome in enumerate(["passed", "failed", "passed", "failed"]):
            self._create_run_with_result(db, "test::flaky", outcome, i)

        stability = db.get_test_stability("test::flaky", window_runs=10, flake_threshold_percent=20.0)
        assert stability.nodeid == "test::flaky"
        assert stability.flip_count == 3
        assert stability.run_count == 4
        assert stability.is_likely_flaky is True
        assert stability.category == "flaky"

    def test_get_test_stability_no_history(self, db: E2EDB):
        """Test with no history returns healthy."""
        stability = db.get_test_stability("test::unknown", window_runs=10, flake_threshold_percent=20.0)
        assert stability.category == "healthy"
        assert stability.run_count == 0

    def test_get_all_test_stability(self, db: E2EDB):
        """Bulk query: get_all_test_stability returns all tests."""
        # Create data for two tests
        for i in range(4):
            run_id = db.start_run(f"/repo{i}", "test-orch", ["tests/e2e"])
            # Test A: alternating (flaky)
            db.upsert_test_result(run_id, "test::flaky_a", "passed" if i % 2 == 0 else "failed")
            # Test B: always passing (healthy)
            db.upsert_test_result(run_id, "test::stable_b", "passed")
            db.finish_run(run_id, "passed" if i % 2 == 0 else "failed")

        results = db.get_all_test_stability(window_runs=10, flake_threshold_percent=20.0)
        assert len(results) == 2

        by_nodeid = {r.nodeid: r for r in results}
        assert by_nodeid["test::flaky_a"].is_likely_flaky is True
        assert by_nodeid["test::flaky_a"].category == "flaky"
        assert by_nodeid["test::stable_b"].is_likely_flaky is False
        assert by_nodeid["test::stable_b"].category == "healthy"

    def test_get_all_test_stability_sorted_by_flip_rate(self, db: E2EDB):
        """Results should be sorted by flip_rate descending."""
        # Create alternating test and stable test
        for i in range(6):
            run_id = db.start_run(f"/repo{i}", "test-orch", ["tests/e2e"])
            db.upsert_test_result(run_id, "test::wild", "passed" if i % 2 == 0 else "failed")
            db.upsert_test_result(run_id, "test::calm", "passed")
            db.finish_run(run_id, "passed")

        results = db.get_all_test_stability(window_runs=10, flake_threshold_percent=20.0)
        assert results[0].nodeid == "test::wild"
        assert results[0].flip_rate > results[1].flip_rate

    def test_stability_uses_retry_outcome(self, db: E2EDB):
        """Retry outcome should take precedence over initial outcome."""
        # Create a run where test initially failed but passed on retry
        run_id = db.start_run("/repo0", "test-orch", ["tests/e2e"])
        db.upsert_test_result(run_id, "test::retried", "failed")
        db.update_retry_outcome(run_id, "test::retried", "passed")
        db.finish_run(run_id, "passed")

        # Second run where it passes outright
        run_id2 = db.start_run("/repo1", "test-orch", ["tests/e2e"])
        db.upsert_test_result(run_id2, "test::retried", "passed")
        db.finish_run(run_id2, "passed")

        stability = db.get_test_stability("test::retried", window_runs=10, flake_threshold_percent=20.0)
        # Both effective outcomes are "passed", so no flips
        assert stability.flip_count == 0
        assert stability.category == "healthy"

    def test_run_details_carries_result_category_separately_from_stability(self, db: E2EDB):
        """UI grouping must not reuse the stability category field."""
        run_id = db.start_run("/test/repo", "test-orch", ["tests/e2e"])
        db.upsert_result_case(
            run_id=run_id,
            case_id="tests/e2e/test_checkout.py::test_checkout",
            outcome="failed",
            duration_seconds=1.2,
            failure_details="AssertionError: checkout failed",
            display_name="test_checkout",
            suite_name="tests/e2e/test_checkout.py",
            result_source="junit_xml",
        )
        db.finish_run(run_id, "failed")

        details = db.run_details_enhanced(run_id)
        assert details is not None
        test_case = details["tests_by_category"]["untriaged"][0]
        assert test_case["category"] == "new_failure"
        assert test_case["result_category"] == "untriaged"

    def test_stability_window_limits_runs(self, db: E2EDB):
        """Window parameter should limit the number of runs considered."""
        # Create 10 runs: first 8 pass, last 2 fail
        for i in range(10):
            outcome = "failed" if i >= 8 else "passed"
            self._create_run_with_result(db, "test::window", outcome, i)

        # Window of 3 should only see the most recent 3 runs
        stability = db.get_test_stability("test::window", window_runs=3, flake_threshold_percent=20.0)
        assert stability.run_count == 3


class TestQuarantineListFunctions:
    """Test quarantine list utility functions."""

    def test_load_quarantine_list_empty_file(self, tmp_path):
        """Load from empty file returns empty set."""
        quarantine_file = tmp_path / "quarantine.txt"
        quarantine_file.write_text("")

        result = load_quarantine_list(quarantine_file)
        assert result == set()

    def test_load_quarantine_list_with_tests(self, tmp_path):
        """Load from file with tests."""
        quarantine_file = tmp_path / "quarantine.txt"
        quarantine_file.write_text("# Comment\ntest::foo\ntest::bar\n\n# Another comment\ntest::baz\n")

        result = load_quarantine_list(quarantine_file)
        assert result == {"test::foo", "test::bar", "test::baz"}

    def test_load_quarantine_list_missing_file(self, tmp_path):
        """Load from non-existent file returns empty set."""
        quarantine_file = tmp_path / "nonexistent.txt"

        result = load_quarantine_list(quarantine_file)
        assert result == set()

    def test_save_quarantine_list_creates_file(self, tmp_path):
        """Save creates file with default header."""
        quarantine_file = tmp_path / "quarantine.txt"
        nodeids = {"test::foo", "test::bar"}

        save_quarantine_list(quarantine_file, nodeids)

        content = quarantine_file.read_text()
        assert "# Quarantined E2E tests" in content
        assert "test::bar" in content
        assert "test::foo" in content

    def test_save_quarantine_list_preserves_header(self, tmp_path):
        """Save preserves existing header comments."""
        quarantine_file = tmp_path / "quarantine.txt"
        quarantine_file.write_text("# Custom header\n# Another line\nold::test\n")

        save_quarantine_list(quarantine_file, {"test::new"})

        content = quarantine_file.read_text()
        assert "# Custom header" in content
        assert "# Another line" in content
        assert "test::new" in content
        assert "old::test" not in content

    def test_save_and_load_roundtrip(self, tmp_path):
        """Save then load returns same set."""
        quarantine_file = tmp_path / "quarantine.txt"
        original = {"test::alpha", "test::beta", "test::gamma"}

        save_quarantine_list(quarantine_file, original)
        loaded = load_quarantine_list(quarantine_file)

        assert loaded == original


class TestOrchestratorInstanceId:
    """Tests for orchestrator_instance_id in e2e_runs."""

    @pytest.fixture
    def db(self, tmp_path: Path) -> E2EDB:
        return E2EDB(tmp_path / "test_e2e.db")

    def test_start_run_stores_instance_id(self, db: E2EDB):
        """start_run persists the orchestrator_instance_id."""
        run_id = db.start_run(
            repo_root="/test/repo",
            orchestrator_id="test-orch",
            pytest_args=["tests/e2e"],
            orchestrator_instance_id="uuid-abc-123",
        )
        run = db.get_run(run_id)
        assert run is not None
        assert run.orchestrator_instance_id == "uuid-abc-123"

    def test_start_run_defaults_instance_id_to_empty(self, db: E2EDB):
        """Without orchestrator_instance_id, field defaults to empty string."""
        run_id = db.start_run(
            repo_root="/test/repo",
            orchestrator_id="test-orch",
            pytest_args=["tests/e2e"],
        )
        run = db.get_run(run_id)
        assert run is not None
        assert run.orchestrator_instance_id == ""

    def test_to_dict_includes_instance_id(self, db: E2EDB):
        """to_dict exposes orchestrator_instance_id."""
        run_id = db.start_run(
            repo_root="/test/repo",
            orchestrator_id="test-orch",
            pytest_args=["tests/e2e"],
            orchestrator_instance_id="uuid-xyz",
        )
        run = db.get_run(run_id)
        assert run is not None
        d = run.to_dict()
        assert d["orchestrator_instance_id"] == "uuid-xyz"

    def test_schema_migration_adds_column(self, tmp_path: Path):
        """Opening a pre-existing DB without the column auto-migrates."""
        import sqlite3

        db_path = tmp_path / "legacy.db"
        # Create a minimal legacy schema without orchestrator_instance_id
        conn = sqlite3.connect(str(db_path))
        conn.execute("""
            CREATE TABLE e2e_runs (
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
            )
        """)
        conn.commit()
        conn.close()

        # Opening with E2EDB should auto-migrate
        db = E2EDB(db_path)
        run_id = db.start_run(
            repo_root="/test",
            orchestrator_id="orch",
            pytest_args=[],
            orchestrator_instance_id="migrated-uuid",
        )
        run = db.get_run(run_id)
        assert run is not None
        assert run.orchestrator_instance_id == "migrated-uuid"

    def test_schema_migration_backfills_legacy_pytest_command(self, tmp_path: Path):
        """Legacy pytest rows gain a canonical command during schema migration."""
        import sqlite3

        db_path = tmp_path / "legacy-command.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            """
            CREATE TABLE e2e_runs (
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
            )
            """
        )
        conn.execute(
            """
            INSERT INTO e2e_runs (
                repo_root,
                orchestrator_id,
                started_at,
                status,
                pytest_args
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                "/tmp/repo",
                "orch",
                "2026-04-23T00:00:00+00:00",
                "passed",
                json.dumps(["tests/e2e", "-v"]),
            ),
        )
        conn.commit()
        conn.close()

        db = E2EDB(db_path)
        run = db.get_run(1)
        assert run is not None
        assert run.command == ["pytest", "tests/e2e", "-v"]
        assert run.runner_kind == "pytest"
