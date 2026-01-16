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
    config_name: str = "default.yaml",
) -> LockInfo:
    """Start an orchestrator for the given repository.

    Args:
        repo_root: Repository root path
        config_name: Name of config file in .issue-orchestrator/config/ (default: default.yaml)

    Returns:
        LockInfo for the started orchestrator

    Raises:
        AlreadyRunning: If an orchestrator is already running for this repo
        FileNotFoundError: If config file not found
    """
    from .config import Config, get_config_path

    repo_root = normalize_repo_root(repo_root)

    # Load config to get port
    config_path = get_config_path(repo_root, config_name)
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    config = Config.load(config_path)
    port = config.web_port

    # Check for existing running orchestrator
    # If lock exists but process is dead, clean up the stale lock
    if is_locked(repo_root):
        info = read_lock(repo_root)
        if info:
            # Verify the process is actually running
            try:
                os.kill(info.pid, 0)
                # Process is alive - can't start another
                raise AlreadyRunning(
                    pid=info.pid,
                    repo_root=repo_root,
                    port=info.http_port,
                )
            except OSError:
                # Process is dead - clean up stale lock and continue
                logger.info("Cleaning up stale lock for %s (pid %d not running)", repo_root, info.pid)
                release_lock(repo_root, info.pid)

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
        "--config",
        str(config_path),
    ]

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
        # Process exited - try to extract error from log
        error_hint = ""
        if log_file.exists():
            try:
                lines = log_file.read_text().splitlines()
                # Find ERROR lines or last few lines
                error_lines = [l for l in lines if "ERROR" in l or "Traceback" in l or "ValueError" in l]
                if error_lines:
                    error_hint = f"\n\nError from log:\n  {error_lines[-1]}"
                elif lines:
                    # Show last non-empty line as hint
                    for line in reversed(lines):
                        if line.strip():
                            error_hint = f"\n\nLast log entry:\n  {line}"
                            break
            except Exception:
                pass  # Don't fail if we can't read logs

        raise RuntimeError(
            f"Orchestrator process exited immediately with code {poll}.{error_hint}\n\n"
            f"Full logs at: {log_file}"
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


def _kill_by_port(port: int, use_sigkill: bool = False) -> bool:
    """Kill processes using a specific port (fallback method).

    Returns True if any process was killed.
    """
    import subprocess
    try:
        result = subprocess.run(
            ["lsof", "-t", f"-i:{port}"],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0 and result.stdout.strip():
            pids = result.stdout.strip().split("\n")
            sig = signal.SIGKILL if use_sigkill else signal.SIGTERM
            killed = False
            for pid_str in pids:
                try:
                    pid = int(pid_str)
                    os.kill(pid, sig)
                    logger.info("Sent %s to process %d on port %d", sig.name, pid, port)
                    killed = True
                except (ProcessLookupError, ValueError):
                    pass
            return killed
    except FileNotFoundError:
        logger.debug("lsof not available for port-based kill")
    return False


def _is_port_in_use(port: int) -> bool:
    """Return True if any process is bound to the given port."""
    try:
        result = subprocess.run(
            ["lsof", "-t", f"-i:{port}"],
            capture_output=True,
            text=True,
        )
        return result.returncode == 0 and bool(result.stdout.strip())
    except FileNotFoundError:
        logger.debug("lsof not available for port check")
        return False


def _try_graceful_shutdown(port: int, pid: int, timeout: float = 2.0) -> bool:
    """Try to shut down orchestrator via HTTP API.

    This provides a clean shutdown with proper UI feedback (shows "Stopping...").

    Returns True if the process exited, False if we need to use signals.
    """
    import time
    import urllib.request
    import urllib.error

    try:
        logger.info("Requesting graceful shutdown via HTTP on port %d", port)
        # Use urllib from stdlib to avoid httpx dependency (architecture rule)
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/shutdown",
            method="POST",
            data=b"",
        )
        with urllib.request.urlopen(req, timeout=2.0) as resp:
            if resp.status == 200:
                logger.debug("Shutdown request accepted, waiting for process to exit")
                # Wait for process to actually exit
                start = time.time()
                while time.time() - start < timeout:
                    try:
                        os.kill(pid, 0)
                        time.sleep(0.1)
                    except OSError:
                        logger.info("Orchestrator exited cleanly via HTTP shutdown")
                        return True
    except (urllib.error.URLError, OSError) as e:
        logger.debug("HTTP shutdown failed: %s, will use signals", e)
    except Exception as e:
        logger.debug("HTTP shutdown failed: %s, will use signals", e)
    return False


def stop_by_port(port: int, force: bool = False) -> bool:
    """Stop an orchestrator by port when no lock file is available."""
    if not port:
        return False

    if not force:
        import urllib.request

        try:
            logger.info("Requesting shutdown on port %d", port)
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/shutdown",
                method="POST",
                data=b"",
            )
            with urllib.request.urlopen(req, timeout=2.0):
                pass
        except Exception as e:
            logger.debug("HTTP shutdown failed on port %d: %s", port, e)

        import time
        time.sleep(0.5)
        if not _is_port_in_use(port):
            return True

    killed = _kill_by_port(port, use_sigkill=force)
    if killed:
        import time
        time.sleep(0.5)
        return not _is_port_in_use(port)
    return False


def stop(repo_root: Path | str, force: bool = False) -> bool:
    """Stop the orchestrator for the given repository.

    Args:
        repo_root: Repository root path
        force: If True, use SIGKILL instead of SIGTERM

    Returns:
        True if orchestrator is stopped (or was already stopped/dead)
        False only if the kill operation truly failed (process still running)
    """
    repo_root = normalize_repo_root(repo_root)

    info = read_lock(repo_root)
    if info is None:
        logger.debug("No lock file found for %s (already stopped)", repo_root)
        # Return True - no running orchestrator means the stop goal is achieved
        return True

    pid = info.pid
    port = info.http_port

    # Check if process is alive
    try:
        os.kill(pid, 0)
    except OSError:
        # Process not running, clean up stale lock
        release_lock(repo_root, pid)
        logger.info("Cleaned up stale lock for %s (pid %d not running)", repo_root, pid)
        # Return True - the process IS stopped (which is what the caller wanted)
        # even if it died before we could kill it ourselves
        return True

    # Try graceful shutdown via HTTP first (unless force kill requested)
    # This gives the orchestrator a chance to show "Stopping..." in its UI
    if not force and port:
        if _try_graceful_shutdown(port, pid):
            release_lock(repo_root, pid)
            return True

    # Fall back to signals
    # Send signal to the process group (negative PID) to kill all children too.
    # The process was started with start_new_session=True, making it the leader
    # of a new process group where PGID == PID.
    sig = signal.SIGKILL if force else signal.SIGTERM
    logger.info("Sending %s to orchestrator process group %d", sig.name, pid)

    try:
        # Kill the entire process group
        os.killpg(pid, sig)
    except OSError as e:
        # Fallback to killing just the process if killpg fails
        logger.warning("Failed to kill process group %d: %s, trying single process", pid, e)
        try:
            os.kill(pid, sig)
        except OSError as e2:
            logger.warning("Failed to send signal to pid %d: %s", pid, e2)

    # Wait for process to exit (with timeout)
    import time

    for _ in range(30):  # Wait up to 3 seconds
        try:
            os.kill(pid, 0)
            time.sleep(0.1)
        except OSError:
            # Process exited
            release_lock(repo_root, pid)
            logger.info("Orchestrator stopped (pid %d)", pid)
            return True

    # Process didn't die - try killing by port as fallback
    if port:
        logger.warning("Process group kill failed, trying to kill by port %d", port)
        _kill_by_port(port, use_sigkill=force)

        # Wait again
        for _ in range(20):  # Wait up to 2 seconds
            try:
                os.kill(pid, 0)
                time.sleep(0.1)
            except OSError:
                release_lock(repo_root, pid)
                logger.info("Orchestrator stopped via port kill (pid %d)", pid)
                return True

    if not force:
        # Try force kill
        logger.warning("Orchestrator did not stop gracefully, forcing with SIGKILL")
        return stop(repo_root, force=True)

    # Last resort - force kill by port
    if port:
        logger.warning("Force killing by port %d", port)
        _kill_by_port(port, use_sigkill=True)
        time.sleep(0.5)

        try:
            os.kill(pid, 0)
        except OSError:
            release_lock(repo_root, pid)
            logger.info("Orchestrator force stopped via port kill")
            return True

    logger.error("Failed to stop orchestrator pid %d", pid)
    release_lock(repo_root, pid)  # Clean up lock anyway
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
