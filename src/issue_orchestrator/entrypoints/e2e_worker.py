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
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from issue_orchestrator.execution.timeline_store import SqliteTimelineStore
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


def _emit_run_event(
    run_id: int,
    event_name: str,
    data: dict,
    *,
    timeline_store: "SqliteTimelineStore",
) -> None:
    """Emit a timeline event for this E2E run.

    Writes to the shared timeline.sqlite store via TimelineKey.
    """
    from ..domain.timeline_key import TimelineKey
    from ..ports.timeline_store import TimelineRecord

    store_key = TimelineKey.for_e2e_run(run_id).to_store_key()
    record = TimelineRecord(
        event_id=uuid.uuid4().hex[:12],
        timestamp=datetime.now(timezone.utc).isoformat(),
        event=event_name,
        data={**data, "e2e_run_id": run_id},
        source_event=event_name,
    )
    timeline_store.append(store_key, record)


class ResultPlugin:
    """Pytest plugin that captures test results incrementally to the database."""

    def __init__(
        self,
        db: "E2EDB",
        run_id: int,
        quarantine: set[str],
        timeline_store: "SqliteTimelineStore",
    ):
        self.db = db
        self.run_id = run_id
        self.quarantine = quarantine
        self.timeline_store = timeline_store
        self.failed_tests: list[str] = []  # Non-quarantined failures for retry
        self.errors: list[str] = []  # Setup/teardown errors (not test failures)

    def pytest_collection_finish(self, session) -> None:
        """Called after test collection - record total test count."""
        total = len(session.items)
        logger.info("Collected %d tests", total)
        self.db.update_progress(self.run_id, total_tests=total)
        _emit_run_event(self.run_id, "e2e.tests_collected", {
            "total": total,
            "nodeids": [item.nodeid for item in session.items],
        }, timeline_store=self.timeline_store)

    def pytest_runtest_logstart(self, nodeid: str, location) -> None:
        """Called when a test starts - update current test."""
        self.db.update_progress(self.run_id, current_test=nodeid)
        _emit_run_event(self.run_id, "e2e.test_started", {
            "nodeid": nodeid,
        }, timeline_store=self.timeline_store)

    def pytest_runtest_logreport(self, report) -> None:
        """Called for each test phase (setup, call, teardown)."""
        # Capture setup/teardown errors so we can explain "all tests
        # passed but run failed" to the user.
        if report.when != "call":
            if report.failed:
                summary = str(report.longrepr)[:500] if report.longrepr else "unknown error"
                self.errors.append(f"{report.nodeid} ({report.when}): {summary}")
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

        event_data: dict[str, Any] = {
            "nodeid": nodeid,
            "outcome": outcome,
            "duration_seconds": duration,
            "is_quarantined": is_quarantined,
        }
        # Carry the pytest failure message into the timeline event so
        # the run drawer can surface it inline without the user having
        # to drill into a separate diagnosis view. We already truncate
        # to 4000 chars when persisting to e2e.db; reuse the same bound.
        if longrepr:
            event_data["longrepr"] = longrepr
        _emit_run_event(
            self.run_id, "e2e.test_completed", event_data,
            timeline_store=self.timeline_store,
        )

        # Track non-quarantined failures for potential retry
        if outcome == "failed" and not is_quarantined:
            self.failed_tests.append(nodeid)
            logger.warning("Test failed: %s", nodeid)


def _run_pytest(
    pytest_args: list[str],
    db: "E2EDB",
    run_id: int,
    quarantine: set[str],
    timeline_store: "SqliteTimelineStore",
) -> tuple[int, list[str], list[str]]:
    """Run pytest with result plugin.

    Returns:
        Tuple of (exit_code, failed non-quarantined tests, setup/teardown errors)
    """
    import pytest

    plugin = ResultPlugin(db, run_id, quarantine, timeline_store=timeline_store)

    # Run pytest in-process with our plugin
    exit_code = pytest.main(pytest_args, plugins=[plugin])

    return exit_code, plugin.failed_tests, plugin.errors


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


def main() -> int:  # noqa: C901, PLR0912 - CLI with argument parsing, test execution, quarantine handling, and retry logic
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
        "--auto-quarantine",
        action="store_true",
        help="Automatically add failing tests to the quarantine list",
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
    parser.add_argument(
        "--orchestrator-instance-id",
        default="",
        help="Orchestrator process instance_id for run-scoped timeline queries",
    )
    parser.add_argument(
        "--timeline-db-path",
        default="",
        help="Path to timeline.sqlite for shared timeline writes",
    )
    parser.add_argument(
        "--run-retention-count",
        type=int,
        default=50,
        help="Max runs to keep; older runs are pruned after completion (default: 50)",
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
    from issue_orchestrator.infra.e2e_db import (
        E2EDB,
        AlreadyRunning,
        load_quarantine_list,
        save_quarantine_list,
    )

    # Load quarantine list
    quarantine_path = repo_root / args.quarantine_file
    quarantine = load_quarantine_list(quarantine_path)
    if quarantine:
        logger.info("Loaded %d quarantined tests from %s", len(quarantine), quarantine_path)

    # Get git info
    commit_sha, branch = _get_git_info(repo_root)
    logger.info("Git: commit=%s, branch=%s", commit_sha, branch)

    # Initialize databases
    db = E2EDB(db_path)

    # Initialize shared timeline store (required)
    if not args.timeline_db_path:
        logger.error("--timeline-db-path is required")
        return 1
    from issue_orchestrator.execution.timeline_store import SqliteTimelineStore

    timeline_store = SqliteTimelineStore(
        db_path=Path(args.timeline_db_path),
        instance_id=args.orchestrator_instance_id,
    )
    logger.info("Timeline store initialized: %s", args.timeline_db_path)

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
                orchestrator_instance_id=args.orchestrator_instance_id,
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

    _emit_run_event(run_id, "e2e.run_started", {
        "pytest_args": pytest_args,
        "commit_sha": commit_sha,
        "branch": branch,
        "quarantined_count": len(quarantine),
        "is_resume": bool(args.resume_run_id),
    }, timeline_store=timeline_store)

    try:
        # Run pytest
        logger.info("Running pytest with args: %s", pytest_args)
        exit_code, failed_tests, fixture_errors = _run_pytest(pytest_args, db, run_id, quarantine, timeline_store=timeline_store)

        # Retry logic
        if args.allow_retry_once and failed_tests:
            _emit_run_event(run_id, "e2e.retry_started", {
                "failed_count": len(failed_tests),
                "nodeids": failed_tests,
            }, timeline_store=timeline_store)
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

        # Determine final status and note.
        #
        # Fixture errors (setup/teardown failures) are independent of
        # test-level retries.  A successful retry clears test failures
        # but cannot clear fixture errors — they represent real
        # infrastructure problems (e.g. GH activity guard) that the
        # retry path does not address.  So if fixture_errors is
        # non-empty the run stays failed regardless of exit_code.
        note: str | None = None
        if fixture_errors:
            status = "failed"
            note = "Fixture errors: " + "; ".join(fixture_errors[:5])
        elif exit_code == 0:
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
            note=note,
        )

        if args.auto_quarantine and status == "failed":
            failed_results = db.get_failed_tests(run_id)
            if failed_results:
                existing = load_quarantine_list(quarantine_path)
                failing = {r.nodeid for r in failed_results}
                updated = existing | failing
                if updated != existing:
                    save_quarantine_list(quarantine_path, updated)
                    added = len(updated) - len(existing)
                    logger.warning(
                        "Auto-quarantined %d test(s) (total=%d) -> %s",
                        added,
                        len(updated),
                        quarantine_path,
                    )
                else:
                    logger.info("Auto-quarantine: no new tests added")

        _emit_run_event(run_id, "e2e.run_finished", {
            "status": status,
            "exit_code": exit_code,
            "duration_seconds": round(duration, 1),
        }, timeline_store=timeline_store)

        logger.info(
            "Finished run %d: status=%s, exit_code=%d, duration=%.1fs",
            run_id,
            status,
            exit_code,
            duration,
        )

        # Prune old runs beyond retention count
        if args.run_retention_count > 0:
            from issue_orchestrator.infra.e2e_worktree import get_e2e_worktree_path
            wt_path = get_e2e_worktree_path(repo_root)
            db.prune_old_runs(
                args.run_retention_count,
                timeline_store=timeline_store,
                e2e_worktree_path=wt_path if wt_path.exists() else None,
            )

        return exit_code

    except KeyboardInterrupt:
        logger.warning("Interrupted, canceling run")
        _emit_run_event(run_id, "e2e.run_canceled", {
            "reason": "interrupted",
            "duration_seconds": round(time.time() - start_time, 1),
        }, timeline_store=timeline_store)
        db.finish_run(
            run_id=run_id,
            status="canceled",
            duration_seconds=time.time() - start_time,
            note="Interrupted by user",
        )
        return 130

    except Exception as e:
        logger.exception("Error running tests: %s", e)
        _emit_run_event(run_id, "e2e.run_error", {
            "error": str(e)[:500],
            "duration_seconds": round(time.time() - start_time, 1),
        }, timeline_store=timeline_store)
        db.finish_run(
            run_id=run_id,
            status="error",
            duration_seconds=time.time() - start_time,
            note=str(e)[:500],
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
