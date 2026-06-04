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
        repo_root: Path,
        timeline_store: "SqliteTimelineStore",
    ):
        self.db = db
        self.run_id = run_id
        self.quarantine = quarantine
        self.repo_root = repo_root
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
        suite_name = nodeid.rsplit("::", 1)[0] if "::" in nodeid else None
        display_name = nodeid.split("::")[-1]
        captured_output = _persist_runtime_captured_output(
            self.repo_root,
            self.run_id,
            nodeid,
            report,
        )

        self.db.upsert_test_result(
            run_id=self.run_id,
            nodeid=nodeid,
            outcome=outcome,
            duration_seconds=duration,
            longrepr=longrepr,
            retry_outcome=None,
            is_quarantined=is_quarantined,
            display_name=display_name,
            suite_name=suite_name,
            result_source="runtime",
            stdout_available=(
                captured_output is not None and captured_output.system_out is not None
            ),
            stderr_available=(
                captured_output is not None and captured_output.system_err is not None
            ),
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
    repo_root: Path,
    timeline_store: "SqliteTimelineStore",
) -> tuple[int, list[str], list[str]]:
    """Run pytest with result plugin.

    Returns:
        Tuple of (exit_code, failed non-quarantined tests, setup/teardown errors)
    """
    import pytest

    plugin = ResultPlugin(
        db,
        run_id,
        quarantine,
        repo_root=repo_root,
        timeline_store=timeline_store,
    )

    # Run pytest in-process with our plugin
    exit_code = pytest.main(pytest_args, plugins=[plugin])

    return exit_code, plugin.failed_tests, plugin.errors


def _persist_runtime_captured_output(
    repo_root: Path,
    run_id: int,
    nodeid: str,
    report: Any,
):
    """Persist pytest's runtime-captured output for live dashboard viewing."""
    from issue_orchestrator.infra.e2e_runtime_output import (
        write_pytest_report_captured_output,
    )

    return write_pytest_report_captured_output(
        repo_root,
        run_id,
        nodeid,
        report,
    )


def _run_retry(
    failed_tests: list[str],
    db: "E2EDB",
    run_id: int,
) -> list[str]:
    """Retry failed tests and update their retry_outcome.

    Returns:
        List of nodeids that passed on retry.
    """
    import pytest

    logger.info("Retrying %d failed tests...", len(failed_tests))

    passed_nodeids: list[str] = []

    for nodeid in failed_tests:
        # Run single test
        exit_code = pytest.main([nodeid, "-v", "--tb=short"])

        if exit_code == 0:
            retry_outcome = "passed"
            passed_nodeids.append(nodeid)
            logger.info("Test passed on retry: %s", nodeid)
        else:
            retry_outcome = "failed"
            logger.warning("Test still failing after retry: %s", nodeid)

        db.update_retry_outcome(run_id, nodeid, retry_outcome)

    return passed_nodeids


def _run_command(command: list[str], repo_root: Path) -> int:
    """Run a generic test command, inheriting the worker's stdout/stderr."""
    if not command:
        raise ValueError("Command runner requires a non-empty command")
    logger.info("Running command: %s", command)
    completed = subprocess.run(command, cwd=repo_root, check=False)
    return int(completed.returncode)


def _emit_junit_case_events(
    run_id: int,
    cases: list[Any],
    *,
    quarantine: set[str],
    timeline_store: "SqliteTimelineStore",
) -> None:
    """Emit synthetic timeline events for post-run JUnit ingestion."""
    if not cases:
        return
    _emit_run_event(
        run_id,
        "e2e.tests_collected",
        {
            "total": len(cases),
            "nodeids": [case.case_id for case in cases],
            "result_source": "junit_xml",
        },
        timeline_store=timeline_store,
    )
    for case in cases:
        event_data: dict[str, Any] = {
            "nodeid": case.case_id,
            "display_name": case.display_name,
            "suite_name": case.suite_name,
            "outcome": case.outcome,
            "duration_seconds": case.duration_seconds,
            "result_source": "junit_xml",
            "is_quarantined": case.case_id in quarantine,
        }
        if case.failure_details:
            event_data["longrepr"] = case.failure_details[:4000]
        _emit_run_event(
            run_id,
            "e2e.test_completed",
            event_data,
            timeline_store=timeline_store,
        )


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
        "--execution-spec-json",
        help="JSON execution spec for pytest or generic command mode",
    )
    parser.add_argument(
        "--pytest-args-json",
        help="JSON array of pytest arguments (legacy compatibility path)",
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
    if args.execution_spec_json:
        raw_execution_spec = json.loads(args.execution_spec_json)
    elif args.pytest_args_json:
        raw_execution_spec = {
            "runner_kind": "pytest",
            "pytest_args": json.loads(args.pytest_args_json),
            "command": [],
            "junit_xml_paths": [],
            "artifact_paths": [],
            "allow_retry_once": args.allow_retry_once,
            "stop_on_first_failure": False,
        }
    else:
        logger.error("--execution-spec-json or --pytest-args-json is required")
        return 1

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
    from issue_orchestrator.infra.config_models import E2EExecutionSpec
    from issue_orchestrator.infra.e2e_db import (
        E2EDB,
        AlreadyRunning,
        load_quarantine_list,
        save_quarantine_list,
    )
    from issue_orchestrator.infra.e2e_reports import (
        discover_report_artifacts,
        junit_report_modified_after,
        normalize_pytest_junit_cases,
        snapshot_report_artifacts,
    )
    from issue_orchestrator.infra.e2e_run_completion import (
        decide_completion,
        run_report_artifact_dir,
        status_from_cases,
    )

    execution_spec = E2EExecutionSpec(
        runner_kind=str(raw_execution_spec.get("runner_kind") or "pytest"),
        pytest_args=tuple(raw_execution_spec.get("pytest_args") or []),
        command=tuple(raw_execution_spec.get("command") or []),
        junit_xml_paths=tuple(raw_execution_spec.get("junit_xml_paths") or []),
        artifact_paths=tuple(raw_execution_spec.get("artifact_paths") or []),
        allow_retry_once=bool(raw_execution_spec.get("allow_retry_once", args.allow_retry_once)),
        stop_on_first_failure=bool(raw_execution_spec.get("stop_on_first_failure", False)),
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
                pytest_args=list(execution_spec.pytest_args),
                commit_sha=commit_sha,
                branch=branch,
                worker_pid=os.getpid(),
                orchestrator_instance_id=args.orchestrator_instance_id,
                command=list(execution_spec.canonical_command),
                runner_kind=execution_spec.runner_kind,
            )
        except AlreadyRunning as e:
            logger.error("Cannot start: %s", e)
            return 1
        logger.info("Started run %d", run_id)

    pytest_args = list(execution_spec.pytest_args)
    # Add deselected tests to pytest args (for skipping already-passed tests on resume)
    if args.deselect and execution_spec.runner_kind == "pytest":
        for nodeid in args.deselect:
            pytest_args.extend(["--deselect", nodeid])
    start_time = time.time()

    # Determine log path for DB record
    log_path_for_db = args.log_file

    _emit_run_event(run_id, "e2e.run_started", {
        "pytest_args": list(execution_spec.pytest_args),
        "command": list(execution_spec.canonical_command),
        "runner_kind": execution_spec.runner_kind,
        "commit_sha": commit_sha,
        "branch": branch,
        "quarantined_count": len(quarantine),
        "is_resume": bool(args.resume_run_id),
    }, timeline_store=timeline_store)

    try:
        failed_tests: list[str] = []
        fixture_errors: list[str] = []
        retried_passed: list[str] = []
        structured_cases: list[Any] = []
        artifact_records: list[Any] = []
        structured_status = None

        if execution_spec.runner_kind == "pytest":
            logger.info("Running pytest with args: %s", pytest_args)
            exit_code, failed_tests, fixture_errors = _run_pytest(
                pytest_args,
                db,
                run_id,
                quarantine,
                repo_root,
                timeline_store=timeline_store,
            )

            if execution_spec.allow_retry_once and failed_tests:
                _emit_run_event(
                    run_id,
                    "e2e.retry_started",
                    {
                        "failed_count": len(failed_tests),
                        "nodeids": failed_tests,
                    },
                    timeline_store=timeline_store,
                )
                retried_passed = _run_retry(failed_tests, db, run_id)

                if len(retried_passed) == len(failed_tests):
                    logger.info("All %d retried tests passed!", len(retried_passed))
                    exit_code = 0
                else:
                    logger.warning(
                        "%d/%d tests still failing after retry",
                        len(failed_tests) - len(retried_passed),
                        len(failed_tests),
                    )

            if execution_spec.junit_xml_paths or execution_spec.artifact_paths:
                structured_cases, artifact_records = discover_report_artifacts(
                    repo_root,
                    junit_xml_paths=execution_spec.junit_xml_paths,
                    artifact_paths=execution_spec.artifact_paths,
                    modified_after=junit_report_modified_after(start_time),
                )
                structured_cases = normalize_pytest_junit_cases(structured_cases)
                if structured_cases:
                    db.record_junit_cases(run_id, structured_cases, quarantine=quarantine)
                    structured_status = status_from_cases(structured_cases, quarantine)
                artifact_records = snapshot_report_artifacts(
                    artifact_records,
                    run_report_artifact_dir(repo_root, run_id),
                )
                db.replace_run_artifacts(run_id, artifact_records)

        elif execution_spec.runner_kind == "command":
            if execution_spec.allow_retry_once:
                logger.info("Ignoring allow_retry_once for command runner")
            if execution_spec.stop_on_first_failure:
                logger.info("Ignoring stop_on_first_failure for command runner")
            exit_code = _run_command(list(execution_spec.command), repo_root)
            structured_cases, artifact_records = discover_report_artifacts(
                repo_root,
                junit_xml_paths=execution_spec.junit_xml_paths,
                artifact_paths=execution_spec.artifact_paths,
                modified_after=junit_report_modified_after(start_time),
            )
            artifact_records = snapshot_report_artifacts(
                artifact_records,
                run_report_artifact_dir(repo_root, run_id),
            )
            db.replace_run_artifacts(run_id, artifact_records)
            if structured_cases:
                db.update_progress(run_id, total_tests=len(structured_cases), current_test=None)
                db.record_junit_cases(run_id, structured_cases, quarantine=quarantine)
                _emit_junit_case_events(
                    run_id,
                    structured_cases,
                    quarantine=quarantine,
                    timeline_store=timeline_store,
                )
                structured_status = status_from_cases(structured_cases, quarantine)
                failed_tests = [
                    case.case_id
                    for case in structured_cases
                    if case.outcome in {"failed", "error"} and case.case_id not in quarantine
                ]
        else:
            raise ValueError(f"Unsupported runner_kind: {execution_spec.runner_kind}")

        # Determine final status and note.
        #
        # Fixture errors (setup/teardown failures) are independent of
        # test-level retries.  A successful retry clears test failures
        # but cannot clear fixture errors — they represent real
        # infrastructure problems (e.g. GH activity guard) that the
        # retry path does not address.  So if fixture_errors is
        # non-empty the run stays failed regardless of exit_code.
        #
        # When tests pass only after retry, the run is "warning" — not
        # silently "passed".  This ensures retries are visible so the
        # user can investigate flakiness.
        decision = decide_completion(
            db=db,
            run_id=run_id,
            runner_kind=execution_spec.runner_kind,
            exit_code=exit_code,
            failed_tests=failed_tests,
            fixture_errors=fixture_errors,
            retried_passed=retried_passed,
            structured_status=structured_status,
        )
        status = decision.status
        exit_code = decision.exit_code
        if execution_spec.runner_kind == "pytest" and exit_code == 5 and status == "passed":
            # pytest exit code 5 = no tests collected
            logger.warning("No tests collected (exit code 5), marking as passed")

        note = "; ".join(decision.notes) if decision.notes else None

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
