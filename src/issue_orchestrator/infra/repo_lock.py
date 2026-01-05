"""Repository lock management.

Ensures only one orchestrator runs per repository by maintaining a lock file
with PID and process liveness checks.
"""

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .repo_identity import lock_file, normalize_repo_root, state_dir


class AlreadyRunning(Exception):
    """Raised when another orchestrator is already running for this repo."""

    def __init__(self, pid: int, repo_root: Path, port: int | None):
        self.pid = pid
        self.repo_root = repo_root
        self.port = port
        super().__init__(
            f"Orchestrator already running for {repo_root} (pid={pid}, port={port})"
        )


@dataclass
class LockInfo:
    """Information stored in the lock file."""

    repo_root: str
    pid: int
    started_at: str
    http_port: int | None
    state_dir: str
    recovered: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Convert to dict for JSON serialization."""
        return {
            "repo_root": self.repo_root,
            "pid": self.pid,
            "started_at": self.started_at,
            "http_port": self.http_port,
            "state_dir": self.state_dir,
            "recovered": self.recovered,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "LockInfo":
        """Create from dict (JSON deserialization)."""
        return cls(
            repo_root=data["repo_root"],
            pid=data["pid"],
            started_at=data["started_at"],
            http_port=data.get("http_port"),
            state_dir=data["state_dir"],
            recovered=data.get("recovered", False),
        )


def _is_process_alive(pid: int) -> bool:
    """Check if a process with the given PID is alive.

    Args:
        pid: Process ID to check

    Returns:
        True if process exists and is running
    """
    try:
        # Signal 0 doesn't kill, just checks if process exists
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _read_lock(lock_path: Path) -> LockInfo | None:
    """Read and parse lock file.

    Args:
        lock_path: Path to lock.json

    Returns:
        LockInfo if file exists and is valid, None otherwise
    """
    if not lock_path.exists():
        return None

    try:
        with open(lock_path) as f:
            data = json.load(f)
        return LockInfo.from_dict(data)
    except (json.JSONDecodeError, KeyError, TypeError):
        return None


def _write_lock(lock_path: Path, info: LockInfo) -> None:
    """Write lock file atomically.

    Args:
        lock_path: Path to lock.json
        info: Lock information to write
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    # Write to temp file first, then rename for atomicity
    tmp_path = lock_path.with_suffix(".tmp")
    with open(tmp_path, "w") as f:
        json.dump(info.to_dict(), f, indent=2)
    tmp_path.rename(lock_path)


def acquire_lock(repo_root: Path | str, port: int | None = None) -> LockInfo:
    """Acquire the repository lock.

    Args:
        repo_root: Repository root path
        port: HTTP port the orchestrator will listen on

    Returns:
        LockInfo for the acquired lock

    Raises:
        AlreadyRunning: If another orchestrator is running for this repo
    """
    repo_root = normalize_repo_root(repo_root)
    lock_path = lock_file(repo_root)

    # Check existing lock
    existing = _read_lock(lock_path)
    if existing is not None:
        if _is_process_alive(existing.pid):
            # Another orchestrator is running
            raise AlreadyRunning(
                pid=existing.pid,
                repo_root=repo_root,
                port=existing.http_port,
            )
        # Stale lock - process is dead, we can take over

    # Create new lock
    recovered = existing is not None  # True if we're recovering from stale lock
    info = LockInfo(
        repo_root=str(repo_root),
        pid=os.getpid(),
        started_at=datetime.now(timezone.utc).isoformat(),
        http_port=port,
        state_dir=str(state_dir(repo_root)),
        recovered=recovered,
    )

    _write_lock(lock_path, info)
    return info


def release_lock(repo_root: Path | str, pid: int | None = None) -> bool:
    """Release the repository lock.

    Only releases if the lock belongs to the specified PID (or current process).

    Args:
        repo_root: Repository root path
        pid: PID that should own the lock (defaults to current process)

    Returns:
        True if lock was released, False if lock didn't exist or belonged to another process
    """
    repo_root = normalize_repo_root(repo_root)
    lock_path = lock_file(repo_root)
    pid = pid or os.getpid()

    existing = _read_lock(lock_path)
    if existing is None:
        return False

    if existing.pid != pid:
        # Lock belongs to another process
        return False

    try:
        lock_path.unlink()
        return True
    except OSError:
        return False


def read_lock(repo_root: Path | str) -> LockInfo | None:
    """Read the current lock file for a repository.

    Args:
        repo_root: Repository root path

    Returns:
        LockInfo if lock exists, None otherwise
    """
    repo_root = normalize_repo_root(repo_root)
    return _read_lock(lock_file(repo_root))


def is_locked(repo_root: Path | str) -> bool:
    """Check if a repository has an active lock.

    Args:
        repo_root: Repository root path

    Returns:
        True if there's an active lock with a live process
    """
    info = read_lock(repo_root)
    if info is None:
        return False
    return _is_process_alive(info.pid)
