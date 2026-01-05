"""Supervisor for managing orchestrator processes.

The supervisor manages the lifecycle of orchestrator processes:
- Starting new orchestrators (one per repo, enforced by lock)
- Stopping running orchestrators
- Querying status

The supervisor itself does NOT run orchestration logic - it only manages processes.
"""

import logging
import os
import signal
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .repo_identity import normalize_repo_root, state_dir
from .repo_lock import (
    AlreadyRunning,
    LockInfo,
    is_locked,
    read_lock,
    release_lock,
)

logger = logging.getLogger(__name__)


@dataclass
class SupervisorStatus:
    """Status of an orchestrator for a repository."""

    state: Literal["running", "stopped", "failed", "unknown"]
    pid: int | None = None
    port: int | None = None
    started_at: str | None = None
    recovered: bool = False
    error: str | None = None

    def to_dict(self) -> dict:
        """Convert to dict for JSON serialization."""
        return {
            "state": self.state,
            "pid": self.pid,
            "port": self.port,
            "started_at": self.started_at,
            "recovered": self.recovered,
            "error": self.error,
        }


def _ensure_log_dir(repo_root: Path) -> Path:
    """Ensure the logs directory exists and return the log file path."""
    log_dir = state_dir(repo_root) / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir / "orchestrator.log"


def start(
    repo_root: Path | str,
    port: int = 8080,
    config_path: Path | str | None = None,
) -> LockInfo:
    """Start an orchestrator for the given repository.

    Args:
        repo_root: Repository root path
        port: HTTP port for the orchestrator web server
        config_path: Optional path to config file

    Returns:
        LockInfo for the started orchestrator

    Raises:
        AlreadyRunning: If an orchestrator is already running for this repo
    """
    repo_root = normalize_repo_root(repo_root)

    # First try to acquire lock (this checks for existing running orchestrator)
    # Note: We acquire the lock in the parent process to fail fast
    # The child process will re-acquire it (and succeed since we release it)
    try:
        # Just check if something is already running
        if is_locked(repo_root):
            info = read_lock(repo_root)
            if info:
                raise AlreadyRunning(
                    pid=info.pid,
                    repo_root=repo_root,
                    port=info.http_port,
                )
    except AlreadyRunning:
        raise

    # Prepare log file
    log_file = _ensure_log_dir(repo_root)

    # Build command
    # Use --no-browser since user can use control center's "Open UI" button
    cmd = [
        sys.executable,
        "-m",
        "issue_orchestrator.entrypoints.run_orchestrator",
        "--repo-root",
        str(repo_root),
        "--port",
        str(port),
        "--no-browser",
    ]
    if config_path:
        cmd.extend(["--config", str(config_path)])

    logger.info("Starting orchestrator for %s on port %d", repo_root, port)
    logger.debug("Command: %s", " ".join(cmd))

    # Open log file for subprocess output
    with open(log_file, "a") as log_f:
        # Start the orchestrator process
        # Use start_new_session=True to detach from parent's process group
        process = subprocess.Popen(
            cmd,
            stdout=log_f,
            stderr=subprocess.STDOUT,
            cwd=str(repo_root),
            start_new_session=True,
        )

    logger.info("Orchestrator started with PID %d", process.pid)

    # Wait a moment for the process to create its lock file
    # The child process will acquire the lock with its own PID
    import time

    for _ in range(50):  # Wait up to 5 seconds
        info = read_lock(repo_root)
        if info is not None and info.pid == process.pid:
            return info
        time.sleep(0.1)

    # Check if process is still alive
    poll = process.poll()
    if poll is not None:
        # Process exited
        raise RuntimeError(
            f"Orchestrator process exited immediately with code {poll}. "
            f"Check logs at {log_file}"
        )

    # Process is running but didn't create lock file yet
    # Return a synthetic LockInfo
    return LockInfo(
        repo_root=str(repo_root),
        pid=process.pid,
        started_at="",
        http_port=port,
        state_dir=str(state_dir(repo_root)),
        recovered=False,
    )


def stop(repo_root: Path | str, force: bool = False) -> bool:
    """Stop the orchestrator for the given repository.

    Args:
        repo_root: Repository root path
        force: If True, use SIGKILL instead of SIGTERM

    Returns:
        True if orchestrator was stopped, False if not running
    """
    repo_root = normalize_repo_root(repo_root)

    info = read_lock(repo_root)
    if info is None:
        logger.debug("No lock file found for %s", repo_root)
        return False

    pid = info.pid

    # Check if process is alive
    try:
        os.kill(pid, 0)
    except OSError:
        # Process not running, clean up stale lock
        release_lock(repo_root, pid)
        logger.info("Cleaned up stale lock for %s (pid %d not running)", repo_root, pid)
        return False

    # Send signal
    sig = signal.SIGKILL if force else signal.SIGTERM
    logger.info("Sending %s to orchestrator pid %d", sig.name, pid)

    try:
        os.kill(pid, sig)
    except OSError as e:
        logger.warning("Failed to send signal to pid %d: %s", pid, e)
        return False

    # Wait for process to exit (with timeout)
    import time

    for _ in range(50):  # Wait up to 5 seconds
        try:
            os.kill(pid, 0)
            time.sleep(0.1)
        except OSError:
            # Process exited
            release_lock(repo_root, pid)
            logger.info("Orchestrator stopped (pid %d)", pid)
            return True

    if not force:
        # Try force kill
        logger.warning("Orchestrator did not stop gracefully, forcing")
        return stop(repo_root, force=True)

    logger.error("Failed to stop orchestrator pid %d", pid)
    return False


def status(repo_root: Path | str) -> SupervisorStatus:
    """Get the status of the orchestrator for the given repository.

    Args:
        repo_root: Repository root path

    Returns:
        SupervisorStatus with current state
    """
    repo_root = normalize_repo_root(repo_root)

    info = read_lock(repo_root)
    if info is None:
        return SupervisorStatus(state="stopped")

    # Check if process is alive
    try:
        os.kill(info.pid, 0)
    except OSError:
        # Process not running but lock exists = failed/crashed
        return SupervisorStatus(
            state="failed",
            pid=info.pid,
            port=info.http_port,
            started_at=info.started_at,
            recovered=info.recovered,
            error="Process not running (stale lock)",
        )

    return SupervisorStatus(
        state="running",
        pid=info.pid,
        port=info.http_port,
        started_at=info.started_at,
        recovered=info.recovered,
    )
