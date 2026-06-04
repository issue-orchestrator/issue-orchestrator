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
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from issue_orchestrator.infra.e2e_db import E2EDB
from issue_orchestrator.infra.e2e_reports import (
    CONFIGURED_JUNIT_XML_PATHS_NO_FRESH_FILES_ERROR,
    CONFIGURED_JUNIT_XML_PATHS_NO_FILES_ERROR,
)
from issue_orchestrator.infra.e2e_runtime_output import read_runtime_captured_output

from .conftest import xdist_timeout

pytestmark = pytest.mark.xdist_group("e2e_worker")


# These tests spawn nested pytest subprocesses. Under full pre-push load that
# startup path can take materially longer than it does when the module is run
# in isolation, so keep the budgets generous enough to avoid false flakes.
_WORKER_SUBPROCESS_TIMEOUT_S = xdist_timeout(90)
_RUN_CREATION_TIMEOUT_S = xdist_timeout(20.0)
_RUN_CREATION_POLL_INTERVAL_S = 0.1


# Note: These tests don't use the @pytest.mark.e2e marker because they don't
# require GitHub activity. They're still run as part of `make test-e2e` since
# they're in the tests/e2e/ directory. They're fully isolated with temp repos.


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def mock_ensure_e2e_worktree():
    """Bypass E2E worktree creation in integration tests.

    These tests use temporary (non-git) directories, so the real
    ensure_e2e_worktree would fail.  Patching it to return the
    repo_root unchanged keeps the worker running against the test repo.
    """
    with patch(
        "issue_orchestrator.infra.e2e_runner.ensure_e2e_worktree",
        side_effect=lambda repo_root: repo_root,
    ):
        yield


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


@pytest.fixture
def test_repo_with_passed_output(tmp_path: Path) -> Path:
    """Create a repo with passing tests that emit stdout and stderr."""
    repo = tmp_path / "test_repo_output"
    repo.mkdir()
    tests_dir = repo / "tests" / "e2e"
    tests_dir.mkdir(parents=True)
    (tests_dir / "test_output.py").write_text(
        """\
import sys


def test_passed_output():
    print("passed stdout from runtime")
    print("passed stderr from runtime", file=sys.stderr)
    assert True
""",
        encoding="utf-8",
    )
    (repo / ".issue-orchestrator").mkdir()
    return repo


# Resolve the source tree for the current checkout so the subprocess
# imports the code under test, not whatever is installed in the base
# venv.  Without this, a review worktree's tests would exercise the
# base repo's worker logic instead of the branch-under-review.
_WORKTREE_SRC = str(Path(__file__).resolve().parents[2] / "src")


def run_worker(
    repo_root: Path,
    orchestrator_id: str = "test-orch",
    pytest_args: list[str] | None = None,
    allow_retry_once: bool = False,
    quarantine_file: str = "tests/e2e/quarantine.txt",
    timeout: int = _WORKER_SUBPROCESS_TIMEOUT_S,
) -> subprocess.CompletedProcess:
    """Run the e2e_worker subprocess."""
    if pytest_args is None:
        pytest_args = ["tests/e2e", "-v"]
    execution_spec = {
        "runner_kind": "pytest",
        "pytest_args": pytest_args,
        "command": [],
        "junit_xml_paths": [],
        "artifact_paths": [],
        "allow_retry_once": allow_retry_once,
        "stop_on_first_failure": False,
    }
    return run_worker_with_execution_spec(
        repo_root,
        orchestrator_id=orchestrator_id,
        execution_spec=execution_spec,
        quarantine_file=quarantine_file,
        timeout=timeout,
    )


def run_worker_with_execution_spec(
    repo_root: Path,
    *,
    orchestrator_id: str = "test-orch",
    execution_spec: dict[str, object],
    quarantine_file: str = "tests/e2e/quarantine.txt",
    timeout: int = _WORKER_SUBPROCESS_TIMEOUT_S,
) -> subprocess.CompletedProcess:
    """Run the e2e_worker subprocess with a normalized execution spec."""
    db_path = repo_root / ".issue-orchestrator" / "e2e.db"
    log_path = repo_root / ".issue-orchestrator" / "e2e.log"
    timeline_db_path = repo_root / ".issue-orchestrator" / "state" / "timeline.sqlite"
    timeline_db_path.parent.mkdir(parents=True, exist_ok=True)

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
        "--execution-spec-json",
        json.dumps(execution_spec),
        "--quarantine-file",
        quarantine_file,
        "--log-file",
        str(log_path),
        "--timeline-db-path",
        str(timeline_db_path),
    ]

    # Prepend the current worktree's src/ to PYTHONPATH so the
    # subprocess imports the code under test rather than whatever
    # package is installed in the base venv.
    env = os.environ.copy()
    env["PYTHONPATH"] = _WORKTREE_SRC + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
    )

    return subprocess.run(
        cmd,
        cwd=repo_root,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )


def _wait_for_run(
    db: E2EDB,
    orchestrator_id: str,
    timeout_s: float = _RUN_CREATION_TIMEOUT_S,
):
    """Wait for a worker to persist its run row."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        run = db.latest_run(orchestrator_id)
        if run is not None:
            return run
        time.sleep(_RUN_CREATION_POLL_INTERVAL_S)
    return None


@pytest.fixture
def test_repo_with_command_reports(tmp_path: Path) -> Path:
    """Create a repo whose E2E command writes JUnit and report artifacts."""
    repo = tmp_path / "test_repo_command"
    repo.mkdir()
    (repo / ".issue-orchestrator").mkdir()
    scripts_dir = repo / "scripts"
    scripts_dir.mkdir()

    junit_path = repo / "artifacts" / "results.xml"
    html_path = repo / "artifacts" / "report.html"
    log_path = repo / "artifacts" / "command-output.log"
    scripts_dir.joinpath("run_command_suite.py").write_text(
        textwrap.dedent(
            f"""\
            from pathlib import Path

            ARTIFACTS = Path("artifacts")
            ARTIFACTS.mkdir(parents=True, exist_ok=True)
            Path({str(junit_path)!r}).write_text(
                \"\"\"<testsuite tests="2" failures="1">
                <testcase classname="ui.smoke" name="test_homepage" time="1.25" />
                <testcase classname="ui.smoke" name="test_checkout" time="2.50">
                  <failure message="checkout failed">AssertionError: checkout broke</failure>
                </testcase>
                </testsuite>\"\"\",
                encoding="utf-8",
            )
            Path({str(html_path)!r}).write_text(
                "<html><body><h1>command report</h1></body></html>",
                encoding="utf-8",
            )
            Path({str(log_path)!r}).write_text(
                "runner started\\ncheckout failed\\n",
                encoding="utf-8",
            )
            raise SystemExit(1)
            """
        ),
        encoding="utf-8",
    )
    return repo


@pytest.fixture
def test_repo_with_missing_command_junit(tmp_path: Path) -> Path:
    """Create a repo whose E2E command exits cleanly without writing reports."""
    repo = tmp_path / "test_repo_command_missing"
    repo.mkdir()
    (repo / ".issue-orchestrator").mkdir()
    scripts_dir = repo / "scripts"
    scripts_dir.mkdir()
    scripts_dir.joinpath("run_without_reports.py").write_text(
        'print("no reports generated")\n',
        encoding="utf-8",
    )
    return repo


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


def test_worker_pytest_runner_ingests_configured_junit_report(test_repo: Path):
    """Pytest-mode runs should ingest configured JUnit XML into generic results."""
    junit_relpath = ".issue-orchestrator/e2e-results/pytest-results.xml"
    (test_repo / ".issue-orchestrator" / "e2e-results").mkdir(parents=True, exist_ok=True)

    result = run_worker_with_execution_spec(
        test_repo,
        execution_spec={
            "runner_kind": "pytest",
            "pytest_args": [
                "tests/e2e",
                "-v",
                f"--junitxml={junit_relpath}",
            ],
            "command": [],
            "junit_xml_paths": [junit_relpath],
            "artifact_paths": [],
            "allow_retry_once": False,
            "stop_on_first_failure": False,
        },
    )

    assert result.returncode in (0, 1), result.stderr

    db = E2EDB(test_repo / ".issue-orchestrator" / "e2e.db")
    run = db.latest_run("test-orch")

    assert run is not None
    assert run.runner_kind == "pytest"
    assert run.command == ["pytest", "tests/e2e", "-v", f"--junitxml={junit_relpath}"]

    details = db.run_details(run.id)
    assert details is not None

    results = details["results"]
    assert len(results) == 3
    assert {row["result_source"] for row in results} == {"junit_xml"}

    outcomes = {row["nodeid"].split("::")[-1]: row["outcome"] for row in results}
    assert outcomes["test_passing"] == "passed"
    assert outcomes["test_failing"] == "failed"
    assert outcomes["test_another_passing"] == "passed"

    artifacts = details["artifacts"]
    artifact_kinds = {
        (artifact["kind"], Path(artifact["path"]).name) for artifact in artifacts
    }
    assert ("junit_xml", "pytest-results.xml") in artifact_kinds


def test_worker_pytest_runner_captures_passed_test_output_live(
    test_repo_with_passed_output: Path,
):
    """Runtime rows should expose passed-test stdout/stderr before JUnit ingest."""
    result = run_worker_with_execution_spec(
        test_repo_with_passed_output,
        execution_spec={
            "runner_kind": "pytest",
            "pytest_args": ["tests/e2e", "-v"],
            "command": [],
            "junit_xml_paths": [],
            "artifact_paths": [],
            "allow_retry_once": False,
            "stop_on_first_failure": False,
        },
    )

    assert result.returncode == 0, result.stderr

    db = E2EDB(test_repo_with_passed_output / ".issue-orchestrator" / "e2e.db")
    run = db.latest_run("test-orch")
    assert run is not None
    details = db.run_details(run.id)
    assert details is not None
    row = details["results"][0]
    assert row["outcome"] == "passed"
    assert row["stdout_available"] is True
    assert row["stderr_available"] is True

    captured = read_runtime_captured_output(
        test_repo_with_passed_output,
        run.id,
        row["nodeid"],
    )
    assert captured is not None
    assert captured.system_out == "passed stdout from runtime"
    assert captured.system_err == "passed stderr from runtime"


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
    # Status should be "warning" — the test passed, but only after retry
    assert run.status == "warning", f"Expected warning but got {run.status}"
    assert run.note is not None
    assert "required retry" in run.note

    # Check the flaky test has retry_outcome
    details = db.run_details(run.id)
    results = {r["nodeid"].split("::")[-1]: r for r in details["results"]}

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

    result = run_worker(test_repo_with_retry, allow_retry_once=False)

    db = E2EDB(test_repo_with_retry / ".issue-orchestrator" / "e2e.db")
    run = db.latest_run("test-orch")

    assert run is not None
    assert run.status == "failed"  # No retry, so stays failed

    # Check no retry_outcome
    details = db.run_details(run.id)
    results = {r["nodeid"].split("::")[-1]: r for r in details["results"]}

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
    results = {r["nodeid"].split("::")[-1]: r for r in details["results"]}

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
    from starlette.testclient import TestClient

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
    timeout = xdist_timeout(60)
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
    run = _wait_for_run(db, orchestrator_id)
    assert run is not None, "Worker did not create run in time"

    # Wait for completion
    timeout = xdist_timeout(60)
    start_time = time.time()
    saw_total_tests = False
    saw_current_test = False

    while time.time() - start_time < timeout:
        status = manager.status(orchestrator_id)
        if not status["running"]:
            break

        # Check for progress during run
        run = db.latest_run(orchestrator_id)
        if run and run.total_tests:
            saw_total_tests = True
        if run and run.current_test:
            saw_current_test = True

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

    timeout = xdist_timeout(60)
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
    run = _wait_for_run(db, orchestrator_id)
    assert run is not None, "Worker did not create run"

    progress = db.get_progress(run.id)
    assert progress["total_tests"] == 3
    assert progress["completed"] == 3
    assert progress["passed"] == 2  # test_passing and test_another_passing
    assert progress["failed"] == 1  # test_failing
    assert progress["percent"] == 100


def test_worker_pytest_runner_merges_configured_junit_results_with_runtime_nodeids(
    test_repo: Path,
):
    """Configured pytest JUnit XML should enrich, not duplicate, runtime results."""
    results_dir = test_repo / ".issue-orchestrator" / "e2e-results"
    results_dir.mkdir(parents=True, exist_ok=True)

    result = run_worker_with_execution_spec(
        test_repo,
        execution_spec={
            "runner_kind": "pytest",
            "pytest_args": [
                "tests/e2e",
                "-v",
                "--junitxml=.issue-orchestrator/e2e-results/pytest-results.xml",
            ],
            "command": [],
            "junit_xml_paths": [".issue-orchestrator/e2e-results/pytest-results.xml"],
            "artifact_paths": [],
            "allow_retry_once": False,
            "stop_on_first_failure": False,
        },
    )

    assert result.returncode == 1, result.stderr

    db = E2EDB(test_repo / ".issue-orchestrator" / "e2e.db")
    run = db.latest_run("test-orch")

    assert run is not None
    details = db.run_details(run.id)
    assert details is not None

    results = details["results"]
    assert len(results) == 3
    assert sorted(row["nodeid"] for row in results) == [
        "tests/e2e/test_basic.py::test_another_passing",
        "tests/e2e/test_basic.py::test_failing",
        "tests/e2e/test_basic.py::test_passing",
    ]
    assert {row["result_source"] for row in results} == {"junit_xml"}

    failure = next(row for row in results if row["nodeid"].endswith("::test_failing"))
    assert failure["outcome"] == "failed"
    assert "This test always fails" in (failure["longrepr"] or "")

    artifacts = details["artifacts"]
    assert [
        (artifact["kind"], Path(artifact["path"]).name)
        for artifact in artifacts
    ] == [("junit_xml", "pytest-results.xml")]


def test_worker_command_runner_ingests_junit_and_artifacts(test_repo_with_command_reports: Path):
    """Command-mode runs should ingest structured JUnit results and expose artifacts."""
    result = run_worker_with_execution_spec(
        test_repo_with_command_reports,
        execution_spec={
            "runner_kind": "command",
            "pytest_args": [],
            "command": [sys.executable, "scripts/run_command_suite.py"],
            "junit_xml_paths": ["artifacts/results.xml"],
            "artifact_paths": ["artifacts/report.html", "artifacts/command-output.log"],
            "allow_retry_once": True,
            "stop_on_first_failure": False,
        },
    )

    assert result.returncode == 1, result.stderr

    db = E2EDB(test_repo_with_command_reports / ".issue-orchestrator" / "e2e.db")
    run = db.latest_run("test-orch")

    assert run is not None
    assert run.status == "failed"
    assert run.runner_kind == "command"
    assert run.command == [sys.executable, "scripts/run_command_suite.py"]

    details = db.run_details(run.id)
    assert details is not None
    results = details["results"]
    assert len(results) == 2
    assert {row["result_source"] for row in results} == {"junit_xml"}

    outcomes = {row["nodeid"]: row["outcome"] for row in results}
    assert outcomes["ui.smoke::test_homepage"] == "passed"
    assert outcomes["ui.smoke::test_checkout"] == "failed"

    failed = db.get_failed_tests(run.id)
    assert [row.nodeid for row in failed] == ["ui.smoke::test_checkout"]

    artifacts = details["artifacts"]
    artifact_kinds = {(artifact["kind"], Path(artifact["path"]).name) for artifact in artifacts}
    assert ("junit_xml", "results.xml") in artifact_kinds
    assert ("html_report", "report.html") in artifact_kinds
    assert ("text_artifact", "command-output.log") in artifact_kinds

    progress = db.get_progress(run.id)
    assert progress["total_tests"] == 2
    assert progress["completed"] == 2
    assert progress["failed"] == 1


def test_worker_command_runner_snapshots_quarantined_only_junit_failures(
    tmp_path: Path,
):
    """Quarantined JUnit failures should retain output and not fail the worker."""
    repo = tmp_path / "test_repo_command_quarantine"
    repo.mkdir()
    (repo / ".issue-orchestrator").mkdir()
    (repo / "tests" / "e2e").mkdir(parents=True)
    (repo / "tests" / "e2e" / "quarantine.txt").write_text(
        "ui.smoke::test_known_flaky\n",
        encoding="utf-8",
    )
    scripts_dir = repo / "scripts"
    scripts_dir.mkdir()
    scripts_dir.joinpath("run_quarantined_suite.py").write_text(
        textwrap.dedent(
            """\
            from pathlib import Path

            artifacts = Path("artifacts")
            artifacts.mkdir(parents=True, exist_ok=True)
            (artifacts / "results.xml").write_text(
                \"\"\"<testsuite tests="1" failures="1">
                <testcase classname="ui.smoke" name="test_known_flaky" time="0.10">
                  <failure message="known flaky">AssertionError: still flaky</failure>
                  <system-out>captured stdout from flaky test</system-out>
                  <system-err>captured stderr from flaky test</system-err>
                </testcase>
                </testsuite>\"\"\",
                encoding="utf-8",
            )
            raise SystemExit(1)
            """
        ),
        encoding="utf-8",
    )

    result = run_worker_with_execution_spec(
        repo,
        execution_spec={
            "runner_kind": "command",
            "pytest_args": [],
            "command": [sys.executable, "scripts/run_quarantined_suite.py"],
            "junit_xml_paths": ["artifacts/results.xml"],
            "artifact_paths": [],
            "allow_retry_once": False,
            "stop_on_first_failure": False,
        },
    )

    assert result.returncode == 0, result.stderr

    db = E2EDB(repo / ".issue-orchestrator" / "e2e.db")
    run = db.latest_run("test-orch")
    assert run is not None
    assert run.status == "warning"
    assert "only quarantined tests failed" in (run.note or "")

    details = db.run_details(run.id)
    assert details is not None
    result_row = details["results"][0]
    assert result_row["nodeid"] == "ui.smoke::test_known_flaky"
    assert result_row["is_quarantined"] is True
    assert result_row["stdout_available"] is True
    assert result_row["stderr_available"] is True
    assert db.get_failed_tests(run.id) == []

    artifact = details["artifacts"][0]
    artifact_path = Path(artifact["path"])
    assert artifact_path.parent == repo / ".issue-orchestrator" / "e2e-results" / f"run_{run.id}"
    assert artifact_path.exists()
    (repo / "artifacts" / "results.xml").unlink()
    assert artifact_path.exists()


def test_worker_command_runner_requires_configured_junit_reports(
    test_repo_with_missing_command_junit: Path,
):
    """Configured JUnit paths should fail loudly when the command does not write them."""
    result = run_worker_with_execution_spec(
        test_repo_with_missing_command_junit,
        execution_spec={
            "runner_kind": "command",
            "pytest_args": [],
            "command": [sys.executable, "scripts/run_without_reports.py"],
            "junit_xml_paths": ["artifacts/results.xml"],
            "artifact_paths": [],
            "allow_retry_once": False,
            "stop_on_first_failure": False,
        },
    )

    assert result.returncode == 1, result.stderr

    db = E2EDB(test_repo_with_missing_command_junit / ".issue-orchestrator" / "e2e.db")
    run = db.latest_run("test-orch")

    assert run is not None
    assert run.status == "error"
    assert run.note == CONFIGURED_JUNIT_XML_PATHS_NO_FILES_ERROR


def test_worker_command_runner_rejects_stale_configured_junit_report(
    test_repo_with_missing_command_junit: Path,
):
    """A stale report from a prior run must not satisfy the current run."""
    stale_report = test_repo_with_missing_command_junit / "artifacts" / "results.xml"
    stale_report.parent.mkdir(parents=True)
    stale_report.write_text(
        """<testsuite tests="1">
        <testcase classname="ui.smoke" name="test_stale" time="0.01" />
        </testsuite>""",
        encoding="utf-8",
    )
    stale_ns = 1_700_000_000_000_000_000
    os.utime(stale_report, ns=(stale_ns, stale_ns))

    result = run_worker_with_execution_spec(
        test_repo_with_missing_command_junit,
        execution_spec={
            "runner_kind": "command",
            "pytest_args": [],
            "command": [sys.executable, "scripts/run_without_reports.py"],
            "junit_xml_paths": ["artifacts/results.xml"],
            "artifact_paths": [],
            "allow_retry_once": False,
            "stop_on_first_failure": False,
        },
    )

    assert result.returncode == 1, result.stderr

    db = E2EDB(test_repo_with_missing_command_junit / ".issue-orchestrator" / "e2e.db")
    run = db.latest_run("test-orch")

    assert run is not None
    assert run.status == "error"
    assert run.note == CONFIGURED_JUNIT_XML_PATHS_NO_FRESH_FILES_ERROR


# ---------------------------------------------------------------------------
# Test: Fixture error surfacing
# ---------------------------------------------------------------------------


@pytest.fixture
def test_repo_with_teardown_error(tmp_path: Path) -> Path:
    """Create a test repo where all tests pass but teardown raises an error."""
    repo = tmp_path / "test_repo_teardown"
    repo.mkdir()

    tests_dir = repo / "tests" / "e2e"
    tests_dir.mkdir(parents=True)

    (tests_dir / "conftest.py").write_text(
        """\
import pytest

@pytest.fixture(autouse=True)
def activity_guard():
    yield
    pytest.fail("GH activity exceeded limit: 100 > 50")
"""
    )

    (tests_dir / "test_basic.py").write_text(
        """\
def test_passing():
    assert True
"""
    )

    (repo / ".issue-orchestrator").mkdir()
    return repo


def test_fixture_error_surfaces_in_run_note(test_repo_with_teardown_error: Path):
    """When all tests pass but a teardown fixture fails, the run must be
    failed with a note explaining the fixture error."""
    result = run_worker(test_repo_with_teardown_error)

    assert result.returncode in (0, 1), f"Worker crashed: {result.stderr}"

    db = E2EDB(test_repo_with_teardown_error / ".issue-orchestrator" / "e2e.db")
    run = db.latest_run("test-orch")

    assert run is not None
    assert run.status == "failed", (
        f"Run with fixture errors must be failed, got {run.status}"
    )
    assert run.note is not None, "Run note must explain the fixture error"
    assert "Fixture errors:" in run.note
    assert "GH activity exceeded limit" in run.note


@pytest.fixture
def test_repo_with_retry_and_teardown_error(tmp_path: Path) -> Path:
    """Create a test repo where a test fails then passes on retry, but
    a teardown fixture also errors."""
    repo = tmp_path / "test_repo_retry_teardown"
    repo.mkdir()

    tests_dir = repo / "tests" / "e2e"
    tests_dir.mkdir(parents=True)

    marker_file = tmp_path / "retry_marker.txt"

    (tests_dir / "conftest.py").write_text(
        """\
import pytest

@pytest.fixture(autouse=True)
def activity_guard():
    yield
    pytest.fail("GH activity exceeded limit: 100 > 50")
"""
    )

    (tests_dir / "test_retry.py").write_text(
        f"""\
from pathlib import Path

MARKER = Path("{marker_file}")

def test_flaky():
    if not MARKER.exists():
        MARKER.write_text("ran once")
        assert False, "First attempt fails"
    else:
        assert True
"""
    )

    (repo / ".issue-orchestrator").mkdir()
    return repo


def test_fixture_error_not_masked_by_retry(test_repo_with_retry_and_teardown_error: Path):
    """A successful retry must not mask fixture errors — the run must
    still be failed with a note."""
    result = run_worker(test_repo_with_retry_and_teardown_error, allow_retry_once=True)

    assert result.returncode in (0, 1), f"Worker crashed: {result.stderr}"

    db = E2EDB(test_repo_with_retry_and_teardown_error / ".issue-orchestrator" / "e2e.db")
    run = db.latest_run("test-orch")

    assert run is not None
    assert run.status == "failed", (
        "Fixture errors must keep run failed even when retried tests pass, "
        f"got status={run.status}"
    )
    assert run.note is not None, "Run note must explain the fixture error"
    assert "Fixture errors:" in run.note
