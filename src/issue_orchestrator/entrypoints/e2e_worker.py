"""E2E test worker - runs pytest and captures results to SQLite.

This entrypoint is spawned as a subprocess by the E2E runner manager.
It runs pytest with a custom plugin that captures results incrementally.
"""

import argparse
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from issue_orchestrator.infra.e2e_db import E2EDB

# Configure logging before imports
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


def _get_git_info(repo_root: Path) -> tuple[Optional[str], Optional[str]]:
    """Get current commit SHA and branch from git."""
    try:
        sha = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root,
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        sha = None

    try:
        branch = subprocess.check_output(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=repo_root,
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        branch = None

    return sha, branch


class ResultPlugin:
    """Pytest plugin that captures test results incrementally to the database."""

    def __init__(
        self,
        db: "E2EDB",
        run_id: int,
        quarantine: set[str],
    ):
        self.db = db
        self.run_id = run_id
        self.quarantine = quarantine
        self.failed_tests: list[str] = []  # Non-quarantined failures for retry

    def pytest_collection_finish(self, session) -> None:
        """Called after test collection - record total test count."""
        total = len(session.items)
        logger.info("Collected %d tests", total)
        self.db.update_progress(self.run_id, total_tests=total)

    def pytest_runtest_logstart(self, nodeid: str, location) -> None:
        """Called when a test starts - update current test."""
        self.db.update_progress(self.run_id, current_test=nodeid)

    def pytest_runtest_logreport(self, report) -> None:
        """Called for each test phase (setup, call, teardown)."""
        # Only record the "call" phase (actual test execution)
        if report.when != "call":
            return

        nodeid = report.nodeid
        outcome = report.outcome  # passed, failed, skipped
        duration = getattr(report, "duration", None)

        # Get failure message if failed
        longrepr = None
        if outcome == "failed":
            longrepr = str(report.longrepr)[:4000] if report.longrepr else None

        is_quarantined = nodeid in self.quarantine

        self.db.upsert_test_result(
            run_id=self.run_id,
            nodeid=nodeid,
            outcome=outcome,
            duration_seconds=duration,
            longrepr=longrepr,
            retry_outcome=None,
            is_quarantined=is_quarantined,
        )

        # Clear current_test after completion
        self.db.update_progress(self.run_id, current_test=None)

        # Track non-quarantined failures for potential retry
        if outcome == "failed" and not is_quarantined:
            self.failed_tests.append(nodeid)
            logger.warning("Test failed: %s", nodeid)


def _run_pytest(
    pytest_args: list[str],
    db: "E2EDB",
    run_id: int,
    quarantine: set[str],
) -> tuple[int, list[str]]:
    """Run pytest with result plugin.

    Returns:
        Tuple of (exit_code, list of failed non-quarantined tests)
    """
    import pytest

    plugin = ResultPlugin(db, run_id, quarantine)

    # Run pytest in-process with our plugin
    exit_code = pytest.main(pytest_args, plugins=[plugin])

    return exit_code, plugin.failed_tests


def _run_retry(
    failed_tests: list[str],
    db: "E2EDB",
    run_id: int,
) -> int:
    """Retry failed tests and update their retry_outcome.

    Returns:
        Number of tests that passed on retry
    """
    import pytest

    logger.info("Retrying %d failed tests...", len(failed_tests))

    passed_on_retry = 0

    for nodeid in failed_tests:
        # Run single test
        exit_code = pytest.main([nodeid, "-v", "--tb=short"])

        if exit_code == 0:
            retry_outcome = "passed"
            passed_on_retry += 1
            logger.info("Test passed on retry: %s", nodeid)
        else:
            retry_outcome = "failed"
            logger.warning("Test still failing after retry: %s", nodeid)

        db.update_retry_outcome(run_id, nodeid, retry_outcome)

    return passed_on_retry


def main() -> int:
    """Main entry point for e2e worker."""
    parser = argparse.ArgumentParser(description="E2E test worker")
    parser.add_argument(
        "--repo-root",
        required=True,
        help="Path to repository root",
    )
    parser.add_argument(
        "--db-path",
        required=True,
        help="Path to SQLite database",
    )
    parser.add_argument(
        "--orchestrator-id",
        required=True,
        help="Orchestrator identifier",
    )
    parser.add_argument(
        "--pytest-args-json",
        required=True,
        help="JSON array of pytest arguments",
    )
    parser.add_argument(
        "--quarantine-file",
        default="tests/e2e/quarantine.txt",
        help="Path to quarantine file (relative to repo root)",
    )
    parser.add_argument(
        "--allow-retry-once",
        action="store_true",
        help="Retry failed tests once",
    )
    parser.add_argument(
        "--log-file",
        help="Path to log file (optional)",
    )
    parser.add_argument(
        "--resume-run-id",
        type=int,
        help="Resume an existing run instead of creating new one",
    )
    parser.add_argument(
        "--deselect",
        action="append",
        default=[],
        help="Tests to deselect (skip) - passed to pytest",
    )

    args = parser.parse_args()

    repo_root = Path(args.repo_root).resolve()
    db_path = Path(args.db_path)
    pytest_args = json.loads(args.pytest_args_json)

    # Configure file logging if specified
    if args.log_file:
        log_path = Path(args.log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_path)
        file_handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
        )
        logging.getLogger().addHandler(file_handler)
        logger.info("Logging to %s", log_path)

    # Change to repo root
    os.chdir(repo_root)
    logger.info("Working directory: %s", repo_root)

    # Import E2EDB after we're in the right directory
    # This ensures any local imports work correctly
    from issue_orchestrator.infra.e2e_db import E2EDB, AlreadyRunning, load_quarantine_list

    # Load quarantine list
    quarantine_path = repo_root / args.quarantine_file
    quarantine = load_quarantine_list(quarantine_path)
    if quarantine:
        logger.info("Loaded %d quarantined tests from %s", len(quarantine), quarantine_path)

    # Get git info
    commit_sha, branch = _get_git_info(repo_root)
    logger.info("Git: commit=%s, branch=%s", commit_sha, branch)

    # Initialize database
    db = E2EDB(db_path)

    # Handle resume vs new run
    if args.resume_run_id:
        run_id = args.resume_run_id
        logger.info("Resuming run %d (skipping %d already-passed tests)", run_id, len(args.deselect))
    else:
        # Start a new run
        try:
            run_id = db.start_run(
                repo_root=str(repo_root),
                orchestrator_id=args.orchestrator_id,
                pytest_args=pytest_args,
                commit_sha=commit_sha,
                branch=branch,
                worker_pid=os.getpid(),
            )
        except AlreadyRunning as e:
            logger.error("Cannot start: %s", e)
            return 1
        logger.info("Started run %d", run_id)

    # Add deselected tests to pytest args (for skipping already-passed tests on resume)
    if args.deselect:
        for nodeid in args.deselect:
            pytest_args.extend(["--deselect", nodeid])
    start_time = time.time()

    # Determine log path for DB record
    log_path_for_db = args.log_file

    try:
        # Run pytest
        logger.info("Running pytest with args: %s", pytest_args)
        exit_code, failed_tests = _run_pytest(pytest_args, db, run_id, quarantine)

        # Retry logic
        if args.allow_retry_once and failed_tests:
            passed_on_retry = _run_retry(failed_tests, db, run_id)

            # Update exit code if all retries passed
            if passed_on_retry == len(failed_tests):
                logger.info("All %d retried tests passed!", passed_on_retry)
                exit_code = 0
            else:
                logger.warning(
                    "%d/%d tests still failing after retry",
                    len(failed_tests) - passed_on_retry,
                    len(failed_tests),
                )

        # Determine final status
        if exit_code == 0:
            status = "passed"
        elif exit_code == 5:
            # pytest exit code 5 = no tests collected
            status = "passed"
            logger.warning("No tests collected (exit code 5), marking as passed")
        else:
            status = "failed"

        duration = time.time() - start_time

        db.finish_run(
            run_id=run_id,
            status=status,
            exit_code=exit_code,
            duration_seconds=duration,
            log_path=log_path_for_db,
        )

        logger.info(
            "Finished run %d: status=%s, exit_code=%d, duration=%.1fs",
            run_id,
            status,
            exit_code,
            duration,
        )

        return exit_code

    except KeyboardInterrupt:
        logger.warning("Interrupted, canceling run")
        db.finish_run(
            run_id=run_id,
            status="canceled",
            duration_seconds=time.time() - start_time,
            note="Interrupted by user",
        )
        return 130

    except Exception as e:
        logger.exception("Error running tests: %s", e)
        db.finish_run(
            run_id=run_id,
            status="error",
            duration_seconds=time.time() - start_time,
            note=str(e)[:500],
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
