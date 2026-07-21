"""Repository lock management.

Ensures only one orchestrator runs per repository (or per instance in
multi-instance mode) by maintaining lock files with PID and process liveness
checks.

Single-instance mode: .issue-orchestrator/lock.json
Multi-instance mode:  .issue-orchestrator/locks/{instance_id}.json
"""

import fcntl
import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .repo_identity import lock_file, locks_dir, normalize_repo_root, state_dir

# Process-local registry of held gate file descriptors, keyed by the metadata
# lock path. ``flock`` is bound to the open file DESCRIPTION (the fd), so the
# descriptor MUST stay open for the whole lock lifetime — closing it releases
# the flock. release_lock (and the held_repo_lock finally) close them here.
_HELD_GATE_FDS: dict[str, list[int]] = {}


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


def _repo_gate_path(repo_root: Path) -> Path:
    """The ONE repo-wide flock gate that serializes every startup mode.

    Single-instance and one-shot commands take it ``LOCK_EX`` (excluding
    everything); a named multi-instance engine takes it ``LOCK_SH`` (so N named
    instances coexist, yet a one-shot's ``LOCK_EX`` still excludes them all).
    This is the single atomic owner the review's A1 requires — it replaces the
    old read-check-rename race AND the TOCTOU ``list_instance_locks`` scan.
    """
    return repo_root / ".issue-orchestrator" / "repo.lock"


def _instance_gate_path(repo_root: Path, instance_id: str) -> Path:
    """Per-instance-id flock gate: rejects a duplicate SAME instance_id."""
    return locks_dir(repo_root) / f"{instance_id}.lock"


def _acquire_gate(path: Path, *, exclusive: bool) -> int:
    """Open ``path`` and take a non-blocking flock; raise BlockingIOError on conflict.

    Returns the held fd (caller keeps it open for the lock lifetime). ``flock`` is
    bound to the open file description, so a second ``os.open`` of the same file —
    even in the same process — conflicts, which is exactly the mutual exclusion
    the old code lacked.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    flag = (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB
    try:
        fcntl.flock(fd, flag)
    except OSError:
        os.close(fd)
        raise
    return fd


def acquire_lock(
    repo_root: Path | str,
    port: int | None = None,
    instance_id: str | None = None,
) -> LockInfo:
    """Acquire the repository lock atomically (or instance-specific lock).

    Exclusion is a repo-wide ``flock`` gate (see :func:`_repo_gate_path`), NOT the
    old read-check-then-write on the metadata file — two callers can no longer
    both "win". The metadata ``lock.json`` / ``locks/{id}.json`` remains the
    on-disk advertisement (pid/port/heartbeat) that the supervisor and status
    endpoints read; it is no longer the exclusion primitive.

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
    exclusive = instance_id is None

    # 1. Repo-wide gate — the cross-mode atomic owner.
    try:
        gate_fd = _acquire_gate(_repo_gate_path(repo_root), exclusive=exclusive)
    except OSError as exc:
        raise _already_running(repo_root, lock_path, instance_id) from exc
    held = [gate_fd]

    # 2. Per-instance gate — reject a duplicate SAME instance_id (multi only;
    #    single-instance/one-shot are already fully excluded by the LOCK_EX gate).
    if instance_id is not None:
        try:
            held.append(
                _acquire_gate(_instance_gate_path(repo_root, instance_id), exclusive=True)
            )
        except OSError as exc:
            _release_fds(held)
            raise _already_running(repo_root, lock_path, instance_id) from exc

    # Gate(s) held: we are the sole owner. A pre-existing metadata file means we
    # took over after a crash (its holder's flock was auto-released on death).
    recovered = _read_lock(lock_path) is not None
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
    _HELD_GATE_FDS.setdefault(str(lock_path), []).extend(held)
    return info


def _already_running(
    repo_root: Path, lock_path: Path, instance_id: str | None
) -> AlreadyRunning:
    """Build AlreadyRunning from the metadata advertisement (best-effort)."""
    existing = _read_lock(lock_path)
    return AlreadyRunning(
        pid=existing.pid if existing else -1,
        repo_root=repo_root,
        port=existing.http_port if existing else None,
        instance_id=instance_id,
    )


def _release_fds(fds: list[int]) -> None:
    """Release + close held gate fds (releasing their flocks)."""
    for fd in fds:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
        except OSError:
            pass


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

    # Release our held gate fds first (this releases the flocks). Do this even
    # if the metadata was already removed so we never leak the exclusion.
    held = _HELD_GATE_FDS.pop(str(lock_path), [])
    _release_fds(held)

    existing = _read_lock(lock_path)
    if existing is None:
        return bool(held)

    if existing.pid != pid:
        # Metadata belongs to another process — never unlink another's advert.
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
    the check and then run concurrently against one repo.

    :func:`acquire_lock` now takes the repo-wide ``LOCK_EX`` gate (a one-shot
    passes ``instance_id=None``), which atomically excludes the single-instance
    engine, every multi-instance engine (they hold ``LOCK_SH`` on the same
    gate), and any other one-shot — so no post-acquire ``list_instance_locks``
    scan is needed (that scan was itself a TOCTOU). Releases on every exit path.
    """
    repo_root = normalize_repo_root(repo_root)
    info = acquire_lock(repo_root, port, instance_id)
    try:
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
