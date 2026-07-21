"""Repository lock management.

Ensures only one orchestrator runs per repository (or per instance in
multi-instance mode) by maintaining lock files with PID and process liveness
checks.

Single-instance mode: .issue-orchestrator/lock.json
Multi-instance mode:  .issue-orchestrator/locks/{instance_id}.json
"""

import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .repo_identity import lock_file, locks_dir, normalize_repo_root, state_dir


class AlreadyRunning(Exception):
    """Raised when another orchestrator is already running for this repo/instance."""

    def __init__(
        self,
        pid: int,
        repo_root: Path,
        port: int | None,
        instance_id: str | None = None,
    ):
        self.pid = pid
        self.repo_root = repo_root
        self.port = port
        self.instance_id = instance_id
        instance_str = f" instance={instance_id}" if instance_id else ""
        super().__init__(
            f"Orchestrator already running for {repo_root}{instance_str} (pid={pid}, port={port})"
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
    instance_id: str | None = None  # For multi-instance deployments
    last_heartbeat_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dict for JSON serialization."""
        result = {
            "repo_root": self.repo_root,
            "pid": self.pid,
            "started_at": self.started_at,
            "http_port": self.http_port,
            "state_dir": self.state_dir,
            "recovered": self.recovered,
            "last_heartbeat_at": self.last_heartbeat_at,
        }
        if self.instance_id is not None:
            result["instance_id"] = self.instance_id
        return result

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
            instance_id=data.get("instance_id"),
            last_heartbeat_at=data.get("last_heartbeat_at"),
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


def acquire_lock(
    repo_root: Path | str,
    port: int | None = None,
    instance_id: str | None = None,
) -> LockInfo:
    """Acquire the repository lock (or instance-specific lock).

    Args:
        repo_root: Repository root path
        port: HTTP port the orchestrator will listen on
        instance_id: Optional instance ID for multi-instance deployments

    Returns:
        LockInfo for the acquired lock

    Raises:
        AlreadyRunning: If another orchestrator is running for this repo/instance
    """
    repo_root = normalize_repo_root(repo_root)
    lock_path = lock_file(repo_root, instance_id)

    # Check existing lock
    existing = _read_lock(lock_path)
    if existing is not None:
        if _is_process_alive(existing.pid):
            # Another orchestrator is running
            raise AlreadyRunning(
                pid=existing.pid,
                repo_root=repo_root,
                port=existing.http_port,
                instance_id=instance_id,
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
        instance_id=instance_id,
        last_heartbeat_at=datetime.now(timezone.utc).isoformat(),
    )

    _write_lock(lock_path, info)
    return info


def release_lock(
    repo_root: Path | str,
    pid: int | None = None,
    instance_id: str | None = None,
) -> bool:
    """Release the repository lock (or instance-specific lock).

    Only releases if the lock belongs to the specified PID (or current process).

    Args:
        repo_root: Repository root path
        pid: PID that should own the lock (defaults to current process)
        instance_id: Optional instance ID for multi-instance deployments

    Returns:
        True if lock was released, False if lock didn't exist or belonged to another process
    """
    repo_root = normalize_repo_root(repo_root)
    lock_path = lock_file(repo_root, instance_id)
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


@contextmanager
def held_repo_lock(
    repo_root: Path | str,
    port: int | None = None,
    instance_id: str | None = None,
) -> Iterator[LockInfo]:
    """Acquire and hold the repo lock across a whole in-process lifecycle.

    The supervised engine (:mod:`..entrypoints.run_orchestrator`) already
    acquires + releases the lock around its run; a one-shot in-process command
    must do the SAME rather than a read-only :func:`is_locked` peek. A peek is
    check-then-act: two one-shots (or a one-shot and the engine) can both pass
    the check and then run concurrently against one repo. Acquiring closes that
    race — :func:`acquire_lock` raises :class:`AlreadyRunning` when a live
    process already holds the target lock file, and this context manager
    releases on every exit path.

    ``acquire_lock`` only inspects the single ``lock.json`` (or this
    ``instance_id``'s file), so it would miss a multi-instance engine holding a
    ``locks/{id}.json``. We therefore also scan :func:`list_instance_locks` and
    refuse (releasing our just-taken lock) if ANY other live instance is
    present — the same "account for all conflicting instance locks" rule the
    supervisor's stop/status paths use.
    """
    repo_root = normalize_repo_root(repo_root)
    info = acquire_lock(repo_root, port, instance_id)
    try:
        conflicts = [
            other for other in list_instance_locks(repo_root) if other.pid != info.pid
        ]
        if conflicts:
            first = conflicts[0]
            raise AlreadyRunning(
                pid=first.pid,
                repo_root=repo_root,
                port=first.http_port,
                instance_id=first.instance_id,
            )
        yield info
    finally:
        release_lock(repo_root, pid=info.pid, instance_id=instance_id)


def read_lock(repo_root: Path | str, instance_id: str | None = None) -> LockInfo | None:
    """Read the current lock file for a repository (or specific instance).

    Args:
        repo_root: Repository root path
        instance_id: Optional instance ID for multi-instance deployments

    Returns:
        LockInfo if lock exists, None otherwise
    """
    repo_root = normalize_repo_root(repo_root)
    return _read_lock(lock_file(repo_root, instance_id))


def is_locked(repo_root: Path | str, instance_id: str | None = None) -> bool:
    """Check if a repository (or specific instance) has an active lock.

    Args:
        repo_root: Repository root path
        instance_id: Optional instance ID for multi-instance deployments

    Returns:
        True if there's an active lock with a live process
    """
    info = read_lock(repo_root, instance_id)
    if info is None:
        return False
    return _is_process_alive(info.pid)


def list_instance_locks(repo_root: Path | str) -> list[LockInfo]:
    """List all active instance locks for a repository.

    Args:
        repo_root: Repository root path

    Returns:
        List of LockInfo for all active instances
    """
    repo_root = normalize_repo_root(repo_root)
    locks_directory = locks_dir(repo_root)

    if not locks_directory.exists():
        return []

    active_locks = []
    for lock_path in locks_directory.glob("*.json"):
        info = _read_lock(lock_path)
        if info is not None and _is_process_alive(info.pid):
            active_locks.append(info)

    return active_locks


def touch_lock(
    repo_root: Path | str,
    pid: int | None = None,
    instance_id: str | None = None,
) -> bool:
    """Update lock heartbeat timestamp for the owning process.

    Returns False when the lock does not exist or belongs to another process.
    """
    repo_root = normalize_repo_root(repo_root)
    lock_path = lock_file(repo_root, instance_id)
    expected_pid = pid or os.getpid()
    existing = _read_lock(lock_path)
    if existing is None or existing.pid != expected_pid:
        return False
    existing.last_heartbeat_at = datetime.now(timezone.utc).isoformat()
    _write_lock(lock_path, existing)
    return True


def set_lock_http_port(
    repo_root: Path | str,
    port: int,
    pid: int | None = None,
    instance_id: str | None = None,
) -> bool:
    """Update the lock's HTTP port for the owning process."""
    repo_root = normalize_repo_root(repo_root)
    lock_path = lock_file(repo_root, instance_id)
    expected_pid = pid or os.getpid()
    existing = _read_lock(lock_path)
    if existing is None or existing.pid != expected_pid:
        return False
    existing.http_port = port
    existing.last_heartbeat_at = datetime.now(timezone.utc).isoformat()
    _write_lock(lock_path, existing)
    return True
