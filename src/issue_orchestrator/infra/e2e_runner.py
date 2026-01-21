"""E2E runner manager - manages worker subprocess lifecycle.

Provides start/stop/status operations for E2E test workers.
"""

import json
from datetime import datetime, timezone, timedelta
import logging
import os
import signal
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from .e2e_db import E2EDB

if TYPE_CHECKING:
    from .config import E2EConfig

logger = logging.getLogger(__name__)


def get_e2e_role(
    e2e_config: "E2EConfig",
    instance_id: str | None = None,
) -> str:
    """Determine the E2E role for this orchestrator instance.

    Args:
        e2e_config: E2E configuration from config file
        instance_id: Instance ID (e.g., "orchestrator-1") from INSTANCE_ID env var

    Returns:
        One of: "executor", "reader", "disabled"

    Role resolution:
    1. If role is explicitly set (not "auto"), use that
    2. Otherwise, orchestrator-1 (or single instance) is executor

    For multi-machine setups, use explicit role with env var:
        role: ${E2E_ROLE}  # Set E2E_ROLE=executor on designated machine
    """
    # Explicit role overrides auto-detection
    if e2e_config.role != "auto":
        return e2e_config.role

    # Auto mode: first instance on single-machine setup is executor
    # instance_id is None for single-instance mode, or "orchestrator-1" for first instance
    if instance_id is None or instance_id == "orchestrator-1":
        return "executor"

    return "reader"


class E2EAlreadyRunning(Exception):
    """Raised when attempting to start while already running."""

    def __init__(self, orchestrator_id: str, pid: int):
        self.orchestrator_id = orchestrator_id
        self.pid = pid
        super().__init__(f"E2E already running for {orchestrator_id} (pid={pid})")


class E2ERunnerManager:
    """Manages E2E worker subprocess lifecycle.

    Thread-safe for use from async web handlers.
    """

    def __init__(self):
        # Track running processes by orchestrator_id
        self._processes: dict[str, subprocess.Popen] = {}

    def start(
        self,
        repo_root: Path,
        orchestrator_id: str,
        pytest_args: list[str],
        allow_retry_once: bool = True,
        quarantine_file: str = "tests/e2e/quarantine.txt",
        stop_on_first_failure: bool = False,
    ) -> dict:
        """Start an E2E worker subprocess.

        Args:
            repo_root: Path to repository root
            orchestrator_id: Unique orchestrator identifier
            pytest_args: Arguments to pass to pytest
            allow_retry_once: Whether to retry failed tests once
            quarantine_file: Path to quarantine file (relative to repo root)
            stop_on_first_failure: If True, add -x flag to stop on first failure

        Returns:
            Dict with 'pid' and 'log_path'

        Raises:
            E2EAlreadyRunning: If a worker is already running for this orchestrator
        """
        # Check if already running
        proc = self._processes.get(orchestrator_id)
        if proc is not None and proc.poll() is None:
            raise E2EAlreadyRunning(orchestrator_id, proc.pid)

        # Prepare paths
        db_path = repo_root / ".issue-orchestrator" / "e2e.db"
        log_dir = repo_root / ".issue-orchestrator" / "logs" / "e2e"
        log_dir.mkdir(parents=True, exist_ok=True)

        # Generate log filename with timestamp
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        log_path = log_dir / f"run_{timestamp}.log"

        # Build effective pytest args (add -x if stop_on_first_failure)
        effective_pytest_args = list(pytest_args)
        if stop_on_first_failure and "-x" not in effective_pytest_args:
            effective_pytest_args.append("-x")

        # Build command
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
            json.dumps(effective_pytest_args),
            "--quarantine-file",
            quarantine_file,
            "--log-file",
            str(log_path),
        ]

        if allow_retry_once:
            cmd.append("--allow-retry-once")

        logger.info(
            "Starting E2E worker for %s: %s",
            orchestrator_id,
            " ".join(cmd[:6]) + "...",
        )

        # Start subprocess with output captured to log file
        # Use start_new_session to detach from parent's process group
        # This prevents signals from propagating to the worker
        log_file_handle = open(log_path, "w")
        proc = subprocess.Popen(
            cmd,
            cwd=repo_root,
            stdout=log_file_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        # Note: log_file_handle stays open - subprocess inherits it and will close on exit

        self._processes[orchestrator_id] = proc

        # Note: Worker sets its own PID in db.start_run() call, so we don't
        # need to update it here. The worker may not have created the run yet.

        logger.info(
            "Started E2E worker pid=%d for %s, log=%s",
            proc.pid,
            orchestrator_id,
            log_path,
        )

        return {
            "pid": proc.pid,
            "log_path": str(log_path),
        }

    def start_or_resume(
        self,
        repo_root: Path,
        orchestrator_id: str,
        pytest_args: list[str],
        allow_retry_once: bool = True,
        quarantine_file: str = "tests/e2e/quarantine.txt",
        stop_on_first_failure: bool = False,
    ) -> dict:
        """Start a new E2E run, or resume an interrupted one.

        If an interrupted run exists, resumes it by skipping already-passed tests.

        Args:
            repo_root: Path to repository root
            orchestrator_id: Unique orchestrator identifier
            pytest_args: Arguments to pass to pytest (for new runs)
            allow_retry_once: Whether to retry failed tests once
            quarantine_file: Path to quarantine file (relative to repo root)
            stop_on_first_failure: If True, add -x flag to stop on first failure

        Returns:
            Dict with 'pid', 'log_path', 'resumed', 'run_id', 'skipped_tests'

        Raises:
            E2EAlreadyRunning: If a worker is already running
        """
        # Check if already running
        proc = self._processes.get(orchestrator_id)
        if proc is not None and proc.poll() is None:
            raise E2EAlreadyRunning(orchestrator_id, proc.pid)

        db_path = repo_root / ".issue-orchestrator" / "e2e.db"
        db = E2EDB(db_path)

        # Check for interrupted run to resume
        interrupted = db.get_interrupted_run(orchestrator_id)
        if interrupted:
            passed_nodeids = db.get_passed_nodeids(interrupted.id)
            if passed_nodeids:
                logger.info(
                    "Resuming interrupted E2E run %d (%d tests already passed)",
                    interrupted.id,
                    len(passed_nodeids),
                )
                return self._resume_run(
                    repo_root=repo_root,
                    orchestrator_id=orchestrator_id,
                    run_id=interrupted.id,
                    pytest_args=interrupted.pytest_args,  # Use original args
                    passed_nodeids=passed_nodeids,
                    allow_retry_once=allow_retry_once,
                    quarantine_file=quarantine_file,
                    stop_on_first_failure=stop_on_first_failure,
                    db=db,
                )
            else:
                # No passed tests - mark as failed and start fresh
                logger.info(
                    "Interrupted run %d has no passed tests, starting fresh",
                    interrupted.id,
                )
                db.finish_run(interrupted.id, "failed", note="No progress, restarting")

        # Start fresh
        result = self.start(
            repo_root=repo_root,
            orchestrator_id=orchestrator_id,
            pytest_args=pytest_args,
            allow_retry_once=allow_retry_once,
            quarantine_file=quarantine_file,
            stop_on_first_failure=stop_on_first_failure,
        )
        result["resumed"] = False
        result["run_id"] = None
        result["skipped_tests"] = 0
        return result

    def _resume_run(
        self,
        repo_root: Path,
        orchestrator_id: str,
        run_id: int,
        pytest_args: list[str],
        passed_nodeids: set[str],
        allow_retry_once: bool,
        quarantine_file: str,
        stop_on_first_failure: bool,
        db: E2EDB,
    ) -> dict:
        """Resume an interrupted run by starting worker with --deselect for passed tests."""
        # Prepare paths
        log_dir = repo_root / ".issue-orchestrator" / "logs" / "e2e"
        log_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        log_path = log_dir / f"run_{run_id}_resume_{timestamp}.log"

        # Build effective pytest args (add -x if stop_on_first_failure)
        effective_pytest_args = list(pytest_args)
        if stop_on_first_failure and "-x" not in effective_pytest_args:
            effective_pytest_args.append("-x")

        # Build command with --deselect for passed tests
        cmd = [
            sys.executable,
            "-m",
            "issue_orchestrator.entrypoints.e2e_worker",
            "--repo-root",
            str(repo_root),
            "--db-path",
            str(repo_root / ".issue-orchestrator" / "e2e.db"),
            "--orchestrator-id",
            orchestrator_id,
            "--pytest-args-json",
            json.dumps(effective_pytest_args),
            "--quarantine-file",
            quarantine_file,
            "--log-file",
            str(log_path),
            "--resume-run-id",
            str(run_id),
        ]

        # Add --deselect for each passed test (passed to pytest)
        for nodeid in passed_nodeids:
            cmd.extend(["--deselect", nodeid])

        if allow_retry_once:
            cmd.append("--allow-retry-once")

        logger.info(
            "Resuming E2E run %d for %s (skipping %d passed tests)",
            run_id,
            orchestrator_id,
            len(passed_nodeids),
        )

        # Start subprocess with output captured to log file
        log_file_handle = open(log_path, "w")
        proc = subprocess.Popen(
            cmd,
            cwd=repo_root,
            stdout=log_file_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        # Note: log_file_handle stays open - subprocess inherits it and will close on exit

        self._processes[orchestrator_id] = proc

        # Update DB: mark as running again with new PID
        db.resume_run(run_id, proc.pid)

        logger.info(
            "Resumed E2E run %d, worker pid=%d, log=%s",
            run_id,
            proc.pid,
            log_path,
        )

        return {
            "pid": proc.pid,
            "log_path": str(log_path),
            "resumed": True,
            "run_id": run_id,
            "skipped_tests": len(passed_nodeids),
        }

    def stop(self, orchestrator_id: str, repo_root: Optional[Path] = None) -> bool:
        """Stop a running E2E worker.

        Args:
            orchestrator_id: Orchestrator to stop
            repo_root: Repository root (to update DB). If None, DB not updated.

        Returns:
            True if a process was stopped, False if none running
        """
        proc = self._processes.get(orchestrator_id)
        if proc is None or proc.poll() is not None:
            # Not running (or already exited)
            self._processes.pop(orchestrator_id, None)
            return False

        pid = proc.pid
        logger.info("Stopping E2E worker pid=%d for %s", pid, orchestrator_id)

        # Send SIGTERM for graceful shutdown
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            # Already gone
            self._processes.pop(orchestrator_id, None)
            return False

        # Wait for exit (with timeout)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            # Force kill
            logger.warning("E2E worker pid=%d did not exit, killing", pid)
            try:
                os.kill(pid, signal.SIGKILL)
                proc.wait(timeout=5)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                pass

        self._processes.pop(orchestrator_id, None)

        # Update DB to mark run as canceled
        if repo_root:
            try:
                db_path = repo_root / ".issue-orchestrator" / "e2e.db"
                if db_path.exists():
                    db = E2EDB(db_path)
                    db.cancel_running(orchestrator_id)
            except Exception as e:
                logger.warning("Failed to update DB after stop: %s", e)

        logger.info("Stopped E2E worker pid=%d for %s", pid, orchestrator_id)
        return True

    def status(self, orchestrator_id: str) -> dict:
        """Get status of E2E worker.

        Returns:
            Dict with 'running', 'pid', 'exit_code'
        """
        proc = self._processes.get(orchestrator_id)

        if proc is None:
            return {"running": False, "pid": None, "exit_code": None}

        exit_code = proc.poll()
        if exit_code is None:
            # Still running
            return {"running": True, "pid": proc.pid, "exit_code": None}
        else:
            # Exited
            self._processes.pop(orchestrator_id, None)
            return {"running": False, "pid": proc.pid, "exit_code": exit_code}

    def cleanup_finished(self) -> list[str]:
        """Clean up references to finished processes.

        Returns:
            List of orchestrator_ids that were cleaned up
        """
        finished = []
        for orch_id in list(self._processes.keys()):
            proc = self._processes[orch_id]
            if proc.poll() is not None:
                self._processes.pop(orch_id, None)
                finished.append(orch_id)
        return finished


# Singleton instance for use by API endpoints
_runner_manager: Optional[E2ERunnerManager] = None


def get_e2e_runner_manager() -> E2ERunnerManager:
    """Get the singleton E2E runner manager."""
    global _runner_manager
    if _runner_manager is None:
        _runner_manager = E2ERunnerManager()
    return _runner_manager


def _get_main_head(repo_root: Path) -> Optional[str]:
    """Get the current HEAD commit SHA of the main branch.

    Returns:
        Commit SHA string, or None if unable to determine.
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "origin/main"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            return result.stdout.strip()
        # Try 'main' without origin
        result = subprocess.run(
            ["git", "rev-parse", "main"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception as e:
        logger.warning("Failed to get main HEAD: %s", e)
    return None


def _should_skip_e2e_trigger(
    config: "Config", repo_root: Path, orchestrator_id: str, instance_id: str | None
) -> bool:
    """Check if E2E trigger should be skipped. Returns True to skip."""
    if not config.e2e.enabled or config.e2e.auto_run_interval_minutes <= 0:
        return True

    role = get_e2e_role(config.e2e, instance_id=instance_id)
    if role != "executor":
        logger.debug("E2E auto-trigger: skipping (role=%s, not executor)", role)
        return True

    manager = get_e2e_runner_manager()
    status = manager.status(orchestrator_id)
    if status["running"]:
        logger.debug("E2E auto-trigger: already running")
        return True

    return False


def _check_e2e_interval_and_head(config: "Config", repo_root: Path, orchestrator_id: str) -> bool:
    """Check if enough time passed and HEAD changed. Returns True to skip."""
    db_path = repo_root / ".issue-orchestrator" / "e2e.db"
    if not db_path.exists():
        return False

    try:
        db = E2EDB(db_path)
        last_run = db.latest_run(orchestrator_id)
        if not last_run or not last_run.finished_at:
            return False

        from datetime import datetime, timezone
        finished = datetime.fromisoformat(last_run.finished_at.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        minutes_since = (now - finished).total_seconds() / 60

        if minutes_since < config.e2e.auto_run_interval_minutes:
            logger.debug(
                "E2E auto-trigger: only %.1f min since last run (need %d)",
                minutes_since, config.e2e.auto_run_interval_minutes,
            )
            return True

        current_head = _get_main_head(repo_root)
        if current_head and last_run.commit_sha == current_head:
            logger.debug(
                "E2E auto-trigger: main HEAD unchanged (%s), skipping",
                current_head[:8] if current_head else "unknown",
            )
            return True
    except Exception as e:
        logger.warning("E2E auto-trigger: failed to check last run: %s", e)
    return False


def maybe_trigger_e2e(
    config: "Config",
    repo_root: Path,
    orchestrator_id: str,
    instance_id: str | None = None,
) -> bool:
    """Check if E2E tests should be auto-triggered and start if appropriate."""
    if _should_skip_e2e_trigger(config, repo_root, orchestrator_id, instance_id):
        return False

    if _check_e2e_interval_and_head(config, repo_root, orchestrator_id):
        return False

    # All conditions met - trigger E2E (or resume interrupted)
    try:
        current_head = _get_main_head(repo_root)
        logger.info(
            "E2E auto-trigger: starting/resuming (main HEAD: %s)",
            current_head[:8] if current_head else "unknown",
        )
        manager = get_e2e_runner_manager()
        result = manager.start_or_resume(
            repo_root=repo_root,
            orchestrator_id=orchestrator_id,
            pytest_args=config.e2e.pytest_args,
            allow_retry_once=config.e2e.allow_retry_once,
            quarantine_file=config.e2e.quarantine_file,
            stop_on_first_failure=config.e2e.stop_on_first_failure,
        )
        if result.get("resumed"):
            logger.info(
                "E2E auto-trigger: resumed run %d (skipping %d passed tests)",
                result["run_id"],
                result["skipped_tests"],
            )
        return True
    except E2EAlreadyRunning:
        # Race condition - another trigger started it
        return False
    except Exception as e:
        logger.error("E2E auto-trigger: failed to start: %s", e)
        return False


def get_next_run_info(
    config: "Config",
    repo_root: Path,
    last_run: "E2ERun | None",
) -> dict:
    """Compute the next scheduled E2E run time and reason for display."""
    if not config.e2e.enabled or config.e2e.auto_run_interval_minutes <= 0:
        return {
            "next_run_at": None,
            "next_run_reason": "auto_disabled",
        }

    if last_run and last_run.status == "running":
        return {
            "next_run_at": None,
            "next_run_reason": "running",
        }

    now = datetime.now(timezone.utc)
    next_run_at = None

    if last_run and last_run.finished_at:
        finished = datetime.fromisoformat(last_run.finished_at.replace("Z", "+00:00"))
        next_run_at = finished + timedelta(minutes=config.e2e.auto_run_interval_minutes)
        if now < next_run_at:
            return {
                "next_run_at": next_run_at.isoformat(),
                "next_run_reason": "interval",
            }

        current_head = _get_main_head(repo_root)
        if current_head and last_run.commit_sha and last_run.commit_sha == current_head:
            return {
                "next_run_at": None,
                "next_run_reason": "main_unchanged",
            }

        return {
            "next_run_at": now.isoformat(),
            "next_run_reason": "ready",
        }

    return {
        "next_run_at": now.isoformat(),
        "next_run_reason": "ready",
    }


# Import for type hints (at module level to avoid circular imports)
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import Config
    from .e2e_db import E2ERun
