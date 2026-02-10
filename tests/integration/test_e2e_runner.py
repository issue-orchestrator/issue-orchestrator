"""E2E tests for the async E2E runner facility.

These tests verify the E2E runner by spawning isolated worker subprocesses
with temporary test repos and separate databases. They don't require GitHub
or the real orchestrator - they're fully self-contained.

Tests cover:
- Worker subprocess lifecycle
- Retry-once policy
- Quarantine support
- Progress tracking
- API flow integration
"""

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from issue_orchestrator.infra.e2e_db import E2EDB

# Note: These tests don't use the @pytest.mark.e2e marker because they don't
# require GitHub activity. They're still run as part of `make test-e2e` since
# they're in the tests/e2e/ directory. They're fully isolated with temp repos.

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def test_repo(tmp_path: Path) -> Path:
    """Create a minimal test repository with pytest tests."""
    repo = tmp_path / "test_repo"
    repo.mkdir()

    # Create tests directory
    tests_dir = repo / "tests" / "e2e"
    tests_dir.mkdir(parents=True)

    # Create conftest.py
    (tests_dir / "conftest.py").write_text("")

    # Create test file with pass and fail
    (tests_dir / "test_basic.py").write_text(
        """\
def test_passing():
    assert True

def test_failing():
    assert False, "This test always fails"

def test_another_passing():
    assert 1 + 1 == 2
"""
    )

    # Create .issue-orchestrator directory
    (repo / ".issue-orchestrator").mkdir()

    return repo

@pytest.fixture
def test_repo_with_retry(tmp_path: Path) -> Path:
    """Create a test repo with a test that fails first, passes on retry."""
    repo = tmp_path / "test_repo_retry"
    repo.mkdir()

    tests_dir = repo / "tests" / "e2e"
    tests_dir.mkdir(parents=True)

    # Marker file path - test fails if missing, passes if present
    marker_file = tmp_path / "retry_marker.txt"

    (tests_dir / "test_retry.py").write_text(
        f"""\
from pathlib import Path

MARKER = Path("{marker_file}")

def test_flaky():
    '''Fails on first run, passes on retry.'''
    if not MARKER.exists():
        MARKER.write_text("ran once")
        assert False, "First attempt fails"
    else:
        assert True  # Retry passes

def test_stable():
    assert True
"""
    )

    (repo / ".issue-orchestrator").mkdir()
    return repo

@pytest.fixture
def test_repo_with_quarantine(tmp_path: Path) -> Path:
    """Create a test repo with a quarantine file."""
    repo = tmp_path / "test_repo_quarantine"
    repo.mkdir()

    tests_dir = repo / "tests" / "e2e"
    tests_dir.mkdir(parents=True)

    (tests_dir / "test_quarantined.py").write_text(
        """\
def test_known_flaky():
    '''This test is quarantined - should be marked but not counted as failure.'''
    assert False, "Known flaky"

def test_real_failure():
    '''This test is NOT quarantined - should be counted as failure.'''
    assert False, "Real failure"

def test_passing():
    assert True
"""
    )

    # Create quarantine file
    (tests_dir / "quarantine.txt").write_text(
        """\
# Known flaky tests
tests/e2e/test_quarantined.py::test_known_flaky
"""
    )

    (repo / ".issue-orchestrator").mkdir()
    return repo

def run_worker(
    repo_root: Path,
    orchestrator_id: str = "test-orch",
    pytest_args: list[str] | None = None,
    allow_retry_once: bool = False,
    quarantine_file: str = "tests/e2e/quarantine.txt",
    timeout: int = 30,
) -> subprocess.CompletedProcess:
    """Run the e2e_worker subprocess."""
    if pytest_args is None:
        pytest_args = ["tests/e2e", "-v"]

    db_path = repo_root / ".issue-orchestrator" / "e2e.db"
    log_path = repo_root / ".issue-orchestrator" / "e2e.log"

    cmd = [
        sys.executable,
        "-m",
        "issue_orchestrator.entrypoints.e2e_worker",
        "--repo-root",
        str(repo_root),
        "--db-path",
        str(db_path),
        "--orchestrator-id",
        orchestrator_id,
        "--pytest-args-json",
        json.dumps(pytest_args),
        "--quarantine-file",
        quarantine_file,
        "--log-file",
        str(log_path),
    ]

    if allow_retry_once:
        cmd.append("--allow-retry-once")

    return subprocess.run(
        cmd,
        cwd=repo_root,
        capture_output=True,
        text=True,
        timeout=timeout,
    )

# ---------------------------------------------------------------------------
# Test 1: Worker round-trip
# ---------------------------------------------------------------------------

def test_worker_round_trip(test_repo: Path):
    """Test that worker runs pytest and writes results to DB."""
    result = run_worker(test_repo)

    # Worker should complete (exit code reflects pytest results, not worker failure)
    # Exit code 1 = some tests failed, which is expected
    assert result.returncode in (0, 1), f"Worker failed: {result.stderr}"

    # Verify DB has the run
    db = E2EDB(test_repo / ".issue-orchestrator" / "e2e.db")
    run = db.latest_run("test-orch")

    assert run is not None
    assert run.status == "failed"  # Because test_failing fails
    assert run.orchestrator_id == "test-orch"

    # Verify test results
    details = db.run_details(run.id)
    assert details is not None

    results = details["results"]
    assert len(results) == 3

    outcomes = {r["nodeid"].split("::")[-1]: r["outcome"] for r in results}
    assert outcomes["test_passing"] == "passed"
    assert outcomes["test_failing"] == "failed"
    assert outcomes["test_another_passing"] == "passed"

    # Verify get_failed_tests returns the failure
    failed = db.get_failed_tests(run.id)
    assert len(failed) == 1
    assert "test_failing" in failed[0].nodeid

# ---------------------------------------------------------------------------
# Test 2: Retry logic
# ---------------------------------------------------------------------------

def test_worker_retry_logic(test_repo_with_retry: Path):
    """Test that --allow-retry-once retries failed tests."""
    result = run_worker(test_repo_with_retry, allow_retry_once=True)

    # Worker should complete
    assert result.returncode in (0, 1), f"Worker failed: {result.stderr}"

    db = E2EDB(test_repo_with_retry / ".issue-orchestrator" / "e2e.db")
    run = db.latest_run("test-orch")

    assert run is not None
    # Should pass because flaky test passes on retry
    assert run.status == "passed", f"Expected passed but got {run.status}"

    # Check the flaky test has retry_outcome
    details = db.run_details(run.id)
    results = {r["nodeid"].split("::")[-1]: r for r in details["results"]}  # type: ignore - Union type narrowing limitation
    flaky = results.get("test_flaky")
    assert flaky is not None
    # Original outcome was failed, retry passed
    assert flaky["outcome"] == "failed"
    assert flaky["retry_outcome"] == "passed"

def test_worker_no_retry_without_flag(test_repo_with_retry: Path):
    """Test that without --allow-retry-once, failed tests stay failed."""
    # Clean up marker file if it exists from previous run
    marker = test_repo_with_retry.parent / "retry_marker.txt"
    if marker.exists():
        marker.unlink()

    _result = run_worker(test_repo_with_retry, allow_retry_once=False)

    db = E2EDB(test_repo_with_retry / ".issue-orchestrator" / "e2e.db")
    run = db.latest_run("test-orch")

    assert run is not None
    assert run.status == "failed"  # No retry, so stays failed

    # Check no retry_outcome
    details = db.run_details(run.id)
    results = {r["nodeid"].split("::")[-1]: r for r in details["results"]}  # type: ignore - Union type narrowing limitation
    flaky = results.get("test_flaky")
    assert flaky is not None
    assert flaky["outcome"] == "failed"
    assert flaky["retry_outcome"] is None

# ---------------------------------------------------------------------------
# Test 3: Quarantine
# ---------------------------------------------------------------------------

def test_worker_quarantine(test_repo_with_quarantine: Path):
    """Test that quarantined tests are marked and excluded from failures."""
    result = run_worker(
        test_repo_with_quarantine,
        quarantine_file="tests/e2e/quarantine.txt",
    )

    assert result.returncode in (0, 1), f"Worker failed: {result.stderr}"

    db = E2EDB(test_repo_with_quarantine / ".issue-orchestrator" / "e2e.db")
    run = db.latest_run("test-orch")

    assert run is not None

    # Check test results
    details = db.run_details(run.id)
    results = {r["nodeid"].split("::")[-1]: r for r in details["results"]}  # type: ignore - Union type narrowing limitation
    # Quarantined test should be marked
    quarantined = results.get("test_known_flaky")
    assert quarantined is not None
    assert quarantined["is_quarantined"] is True
    assert quarantined["outcome"] == "failed"

    # Non-quarantined failure should not be marked
    real_failure = results.get("test_real_failure")
    assert real_failure is not None
    assert real_failure["is_quarantined"] is False
    assert real_failure["outcome"] == "failed"

    # get_failed_tests should only return non-quarantined failures
    failed = db.get_failed_tests(run.id)
    failed_nodeids = {f.nodeid for f in failed}

    assert any("test_real_failure" in n for n in failed_nodeids)
    assert not any("test_known_flaky" in n for n in failed_nodeids)

# ---------------------------------------------------------------------------
# Test 4: API flow
# ---------------------------------------------------------------------------

def test_api_flow(test_repo: Path):
    """Test the full API flow: start -> status -> run details."""

    # We need to set up the control API with proper config
    # This is more complex as it requires the full app setup
    # For now, test the E2ERunnerManager directly which the API uses

    from issue_orchestrator.infra.e2e_runner import E2ERunnerManager

    manager = E2ERunnerManager()
    orchestrator_id = "api-test-orch"

    # Start
    start_result = manager.start(
        repo_root=test_repo,
        orchestrator_id=orchestrator_id,
        pytest_args=["tests/e2e", "-v"],
        allow_retry_once=False,
    )
    assert "pid" in start_result
    pid = start_result["pid"]

    # Status should show running
    status = manager.status(orchestrator_id)
    assert status["running"] is True
    assert status["pid"] == pid

    # Wait for completion (with timeout)
    timeout = 60
    start_time = time.time()
    final_status = None
    while time.time() - start_time < timeout:
        status = manager.status(orchestrator_id)
        if not status["running"]:
            final_status = status  # Capture before process is cleaned up
            break
        time.sleep(0.5)
    else:
        manager.stop(orchestrator_id)
        pytest.fail("Worker did not complete within timeout")

    # Status should show not running with exit code
    assert final_status is not None
    assert final_status["running"] is False
    assert final_status["exit_code"] is not None

    # DB should have run details
    db = E2EDB(test_repo / ".issue-orchestrator" / "e2e.db")
    run = db.latest_run(orchestrator_id)

    assert run is not None
    assert run.status in ("passed", "failed")

    # Run details should include test results
    details = db.run_details(run.id)
    assert details is not None
    assert len(details["results"]) == 3

    # Signal score should be computable
    score = db.compute_signal_score(orchestrator_id)
    assert score["runs_analyzed"] == 1

# ---------------------------------------------------------------------------
# Test 5: Progress tracking
# ---------------------------------------------------------------------------

@pytest.fixture
def test_repo_with_slow_tests(tmp_path: Path) -> Path:
    """Create a test repo with tests that take measurable time."""
    repo = tmp_path / "test_repo_slow"
    repo.mkdir()

    tests_dir = repo / "tests" / "e2e"
    tests_dir.mkdir(parents=True)

    (tests_dir / "conftest.py").write_text("")

    # Create tests with small delays so we can observe progress
    (tests_dir / "test_progress.py").write_text(
        """\
import time

def test_first():
    time.sleep(0.1)
    assert True

def test_second():
    time.sleep(0.1)
    assert True

def test_third():
    time.sleep(0.1)
    assert True

def test_fourth():
    time.sleep(0.1)
    assert True
"""
    )

    (repo / ".issue-orchestrator").mkdir()
    return repo

def test_progress_tracking(test_repo_with_slow_tests: Path):
    """Test that progress tracking captures total_tests and completion counts."""
    from issue_orchestrator.infra.e2e_runner import E2ERunnerManager

    manager = E2ERunnerManager()
    orchestrator_id = "progress-test-orch"
    repo = test_repo_with_slow_tests

    # Start the worker
    start_result = manager.start(
        repo_root=repo,
        orchestrator_id=orchestrator_id,
        pytest_args=["tests/e2e", "-v"],
        allow_retry_once=False,
    )
    assert "pid" in start_result

    # Wait for worker to create its run (happens early in worker startup)
    db = E2EDB(repo / ".issue-orchestrator" / "e2e.db")
    run = None
    for _ in range(50):  # Wait up to 5 seconds
        run = db.latest_run(orchestrator_id)
        if run:
            break
        time.sleep(0.1)
    assert run is not None, "Worker did not create run in time"

    # Wait for completion
    timeout = 60
    start_time = time.time()
    _saw_total_tests = False
    _saw_current_test = False

    while time.time() - start_time < timeout:
        status = manager.status(orchestrator_id)
        if not status["running"]:
            break

        # Check for progress during run
        run = db.latest_run(orchestrator_id)
        if run and run.total_tests:
            _saw_total_tests = True
        if run and run.current_test:
            _saw_current_test = True

        time.sleep(0.05)  # Poll frequently
    else:
        manager.stop(orchestrator_id)
        pytest.fail("Worker did not complete within timeout")

    # After completion, verify final state
    run = db.latest_run(orchestrator_id)
    assert run is not None
    assert run.status == "passed"

    # total_tests should be set after collection
    assert run.total_tests == 4, f"Expected 4 tests, got {run.total_tests}"

    # Note: current_test may or may not be None at the end depending on timing
    # The important thing is that it was set during execution (saw_current_test)

    # get_progress should return correct counts
    progress = db.get_progress(run.id)
    assert progress["total_tests"] == 4
    assert progress["completed"] == 4
    assert progress["passed"] == 4
    assert progress["failed"] == 0
    assert progress["skipped"] == 0
    assert progress["percent"] == 100

    # We don't assert saw_total_tests or saw_current_test here because
    # on fast systems the worker may complete before we poll.
    # The important verification is that total_tests and progress counts
    # are correct AFTER completion, which we checked above.

def test_progress_with_failures(test_repo: Path):
    """Test progress tracking with mixed pass/fail results."""
    from issue_orchestrator.infra.e2e_runner import E2ERunnerManager

    manager = E2ERunnerManager()
    orchestrator_id = "progress-fail-test-orch"

    # Start and wait for completion
    manager.start(
        repo_root=test_repo,
        orchestrator_id=orchestrator_id,
        pytest_args=["tests/e2e", "-v"],
        allow_retry_once=False,
    )

    timeout = 60
    start_time = time.time()
    while time.time() - start_time < timeout:
        status = manager.status(orchestrator_id)
        if not status["running"]:
            break
        time.sleep(0.1)
    else:
        manager.stop(orchestrator_id)
        pytest.fail("Worker did not complete within timeout")

    db = E2EDB(test_repo / ".issue-orchestrator" / "e2e.db")

    # Wait for worker to have created its run (in case of fast exit)
    run = None
    for _ in range(20):  # Wait up to 2 seconds
        run = db.latest_run(orchestrator_id)
        if run:
            break
        time.sleep(0.1)
    assert run is not None, "Worker did not create run"

    progress = db.get_progress(run.id)
    assert progress["total_tests"] == 3
    assert progress["completed"] == 3
    assert progress["passed"] == 2  # test_passing and test_another_passing
    assert progress["failed"] == 1  # test_failing
    assert progress["percent"] == 100
