"""Tests for repo_lock module."""

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from issue_orchestrator.infra.repo_lock import (
    AlreadyRunning,
    LockInfo,
    acquire_lock,
    held_repo_lock,
    is_locked,
    read_lock,
    release_lock,
)


class TestAcquireLock:
    """Tests for acquire_lock function."""

    def test_acquire_lock_success(self, tmp_path: Path) -> None:
        """Successfully acquire lock when no lock exists."""
        info = acquire_lock(tmp_path, port=8080)

        assert info.repo_root == str(tmp_path)
        assert info.pid == os.getpid()
        assert info.http_port == 8080
        assert info.recovered is False

        # Verify lock file was created
        lock_path = tmp_path / ".issue-orchestrator" / "lock.json"
        assert lock_path.exists()

        with open(lock_path) as f:
            data = json.load(f)
        assert data["pid"] == os.getpid()
        assert data["http_port"] == 8080

    def test_acquire_lock_stale_lock_recovery(self, tmp_path: Path) -> None:
        """Recover from stale lock (process no longer running)."""
        # Create a stale lock with a non-existent PID
        lock_dir = tmp_path / ".issue-orchestrator"
        lock_dir.mkdir(parents=True)
        lock_path = lock_dir / "lock.json"

        stale_lock = {
            "repo_root": str(tmp_path),
            "pid": 999999999,  # Very unlikely to be a real process
            "started_at": "2024-01-01T00:00:00Z",
            "http_port": 9999,
            "state_dir": str(tmp_path / ".issue-orchestrator" / "state"),
            "recovered": False,
        }
        with open(lock_path, "w") as f:
            json.dump(stale_lock, f)

        # Should succeed and mark as recovered
        info = acquire_lock(tmp_path, port=8080)

        assert info.pid == os.getpid()
        assert info.http_port == 8080
        assert info.recovered is True

    def test_acquire_lock_already_running(self, tmp_path: Path) -> None:
        """Raise AlreadyRunning when another process holds the lock."""
        # First acquire
        acquire_lock(tmp_path, port=8080)

        # Second acquire should fail (same process, but simulates another)
        # We need to mock _is_process_alive to return True for the existing PID
        with patch(
            "issue_orchestrator.infra.repo_lock._is_process_alive", return_value=True
        ):
            with pytest.raises(AlreadyRunning) as exc_info:
                acquire_lock(tmp_path, port=9090)

        assert exc_info.value.pid == os.getpid()
        assert exc_info.value.repo_root == tmp_path
        assert exc_info.value.port == 8080

    def test_acquire_lock_creates_parent_directories(self, tmp_path: Path) -> None:
        """Lock acquisition creates .issue-orchestrator directory."""
        repo_root = tmp_path / "nested" / "repo"
        repo_root.mkdir(parents=True)

        info = acquire_lock(repo_root, port=8080)

        lock_path = repo_root / ".issue-orchestrator" / "lock.json"
        assert lock_path.exists()
        assert info.repo_root == str(repo_root)


class TestHeldRepoLock:
    """F6: the one-shot commands HOLD the lock across their whole lifecycle."""

    def test_holds_lock_in_body_then_releases_on_exit(self, tmp_path: Path) -> None:
        lock_path = tmp_path / ".issue-orchestrator" / "lock.json"
        with held_repo_lock(tmp_path, port=8080) as info:
            assert info.pid == os.getpid()
            assert lock_path.exists()  # held for the whole body
        assert not lock_path.exists()  # released on exit

    def test_releases_lock_even_when_body_raises(self, tmp_path: Path) -> None:
        lock_path = tmp_path / ".issue-orchestrator" / "lock.json"
        with pytest.raises(RuntimeError):
            with held_repo_lock(tmp_path):
                assert lock_path.exists()
                raise RuntimeError("boom")
        assert not lock_path.exists()

    def test_refuses_when_single_instance_lock_is_live(self, tmp_path: Path) -> None:
        """A live single-instance holder makes held_repo_lock refuse — and it
        must NOT clobber that other process's lock."""
        acquire_lock(tmp_path, port=8080)  # a live holder (our pid)
        with patch(
            "issue_orchestrator.infra.repo_lock._is_process_alive", return_value=True
        ):
            with pytest.raises(AlreadyRunning):
                with held_repo_lock(tmp_path):
                    pass
        # The pre-existing lock is untouched (never acquired, never released).
        assert (tmp_path / ".issue-orchestrator" / "lock.json").exists()

    def test_refuses_when_multi_instance_engine_is_live(self, tmp_path: Path) -> None:
        """A one-shot (LOCK_EX) is refused while ANY multi-instance engine holds
        the SHARED repo gate — atomically, with no TOCTOU scan (#6824 R2/F6)."""
        import fcntl

        gate = tmp_path / ".issue-orchestrator" / "repo.lock"
        gate.parent.mkdir(parents=True)
        other = os.open(gate, os.O_RDWR | os.O_CREAT)
        # Simulate a live named multi-instance engine holding the shared gate.
        fcntl.flock(other, fcntl.LOCK_SH | fcntl.LOCK_NB)
        try:
            with pytest.raises(AlreadyRunning):
                with held_repo_lock(tmp_path):
                    pass
        finally:
            os.close(other)
        # Never acquired -> no metadata leaked.
        assert not (tmp_path / ".issue-orchestrator" / "lock.json").exists()


class TestLockAtomicity:
    """R2 (#6824): one atomic flock owner governs every startup mode.

    The exclusion is a real held flock, not a read-check-rename — so these
    tests assert exactly-one-winner under genuine contention, not pid fakery.
    """

    def test_concurrent_acquire_has_exactly_one_winner(self, tmp_path: Path) -> None:
        import threading
        from concurrent.futures import ThreadPoolExecutor

        workers = 16
        ready = threading.Barrier(workers)

        def attempt(_n: int) -> bool:
            ready.wait(timeout=5)
            try:
                acquire_lock(tmp_path)
                return True
            except AlreadyRunning:
                return False

        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(attempt, range(workers)))

        assert sum(results) == 1  # exactly one winner, no double-acquire

    def test_second_single_instance_acquire_refused(self, tmp_path: Path) -> None:
        acquire_lock(tmp_path)
        with pytest.raises(AlreadyRunning):
            acquire_lock(tmp_path)
        release_lock(tmp_path)
        # After release the gate is free again.
        acquire_lock(tmp_path)
        release_lock(tmp_path)

    def test_named_multi_instances_coexist_but_exclude_exclusive(
        self, tmp_path: Path
    ) -> None:
        acquire_lock(tmp_path, instance_id="a")
        acquire_lock(tmp_path, instance_id="b")  # different id -> both hold LOCK_SH
        try:
            # A single-instance engine / one-shot (LOCK_EX) is excluded by the
            # shared holders — the cross-mode guarantee the old scan raced on.
            with pytest.raises(AlreadyRunning):
                acquire_lock(tmp_path)
        finally:
            release_lock(tmp_path, instance_id="a")
            release_lock(tmp_path, instance_id="b")
        # Once both shared holders release, the exclusive lock is available.
        acquire_lock(tmp_path)
        release_lock(tmp_path)

    def test_same_instance_id_rejected(self, tmp_path: Path) -> None:
        acquire_lock(tmp_path, instance_id="a")
        with pytest.raises(AlreadyRunning):
            acquire_lock(tmp_path, instance_id="a")
        release_lock(tmp_path, instance_id="a")

    def test_stale_metadata_without_flock_is_taken_over(self, tmp_path: Path) -> None:
        """A crashed holder's flock is auto-released; its stale metadata is a
        takeover, marked recovered=True (not a false AlreadyRunning)."""
        lock_dir = tmp_path / ".issue-orchestrator"
        lock_dir.mkdir(parents=True)
        (lock_dir / "lock.json").write_text(
            json.dumps(
                {
                    "repo_root": str(tmp_path),
                    "pid": 999999999,
                    "started_at": "2024-01-01T00:00:00Z",
                    "http_port": 9999,
                    "state_dir": str(lock_dir / "state"),
                    "recovered": False,
                }
            )
        )
        info = acquire_lock(tmp_path, port=8080)
        assert info.pid == os.getpid()
        assert info.recovered is True
        release_lock(tmp_path)


class TestReleaseLock:
    """Tests for release_lock function."""

    def test_release_lock_success(self, tmp_path: Path) -> None:
        """Successfully release a lock owned by current process."""
        acquire_lock(tmp_path, port=8080)

        result = release_lock(tmp_path)

        assert result is True
        lock_path = tmp_path / ".issue-orchestrator" / "lock.json"
        assert not lock_path.exists()

    def test_release_lock_no_lock(self, tmp_path: Path) -> None:
        """Return False when no lock exists."""
        result = release_lock(tmp_path)
        assert result is False

    def test_release_lock_wrong_pid(self, tmp_path: Path) -> None:
        """Return False when lock belongs to another process."""
        acquire_lock(tmp_path, port=8080)

        # Try to release with wrong PID
        result = release_lock(tmp_path, pid=999999999)

        assert result is False
        # Lock should still exist
        lock_path = tmp_path / ".issue-orchestrator" / "lock.json"
        assert lock_path.exists()


class TestReadLock:
    """Tests for read_lock function."""

    def test_read_lock_exists(self, tmp_path: Path) -> None:
        """Read existing lock file."""
        acquire_lock(tmp_path, port=8080)

        info = read_lock(tmp_path)

        assert info is not None
        assert info.pid == os.getpid()
        assert info.http_port == 8080

    def test_read_lock_not_exists(self, tmp_path: Path) -> None:
        """Return None when no lock exists."""
        info = read_lock(tmp_path)
        assert info is None

    def test_read_lock_invalid_json(self, tmp_path: Path) -> None:
        """Return None when lock file contains invalid JSON."""
        lock_dir = tmp_path / ".issue-orchestrator"
        lock_dir.mkdir(parents=True)
        lock_path = lock_dir / "lock.json"

        with open(lock_path, "w") as f:
            f.write("not valid json")

        info = read_lock(tmp_path)
        assert info is None


class TestIsLocked:
    """Tests for is_locked function."""

    def test_is_locked_with_live_process(self, tmp_path: Path) -> None:
        """Return True when lock exists and process is alive."""
        acquire_lock(tmp_path, port=8080)

        result = is_locked(tmp_path)
        assert result is True

    def test_is_locked_no_lock(self, tmp_path: Path) -> None:
        """Return False when no lock exists."""
        result = is_locked(tmp_path)
        assert result is False

    def test_is_locked_stale_lock(self, tmp_path: Path) -> None:
        """Return False when lock exists but process is dead."""
        # Create stale lock
        lock_dir = tmp_path / ".issue-orchestrator"
        lock_dir.mkdir(parents=True)
        lock_path = lock_dir / "lock.json"

        stale_lock = {
            "repo_root": str(tmp_path),
            "pid": 999999999,
            "started_at": "2024-01-01T00:00:00Z",
            "http_port": 8080,
            "state_dir": str(tmp_path / ".issue-orchestrator" / "state"),
        }
        with open(lock_path, "w") as f:
            json.dump(stale_lock, f)

        result = is_locked(tmp_path)
        assert result is False


class TestLockInfo:
    """Tests for LockInfo dataclass."""

    def test_lock_info_to_dict(self) -> None:
        """Convert LockInfo to dict."""
        info = LockInfo(
            repo_root="/path/to/repo",
            pid=12345,
            started_at="2024-01-01T00:00:00Z",
            http_port=8080,
            state_dir="/path/to/repo/.issue-orchestrator/state",
            recovered=True,
        )

        data = info.to_dict()

        assert data["repo_root"] == "/path/to/repo"
        assert data["pid"] == 12345
        assert data["http_port"] == 8080
        assert data["recovered"] is True

    def test_lock_info_from_dict(self) -> None:
        """Create LockInfo from dict."""
        data = {
            "repo_root": "/path/to/repo",
            "pid": 12345,
            "started_at": "2024-01-01T00:00:00Z",
            "http_port": 8080,
            "state_dir": "/path/to/repo/.issue-orchestrator/state",
            "recovered": False,
        }

        info = LockInfo.from_dict(data)

        assert info.repo_root == "/path/to/repo"
        assert info.pid == 12345
        assert info.http_port == 8080
        assert info.recovered is False

    def test_lock_info_from_dict_missing_optional_fields(self) -> None:
        """Create LockInfo with missing optional fields."""
        data = {
            "repo_root": "/path/to/repo",
            "pid": 12345,
            "started_at": "2024-01-01T00:00:00Z",
            "state_dir": "/path/to/repo/.issue-orchestrator/state",
            # http_port and recovered are optional
        }

        info = LockInfo.from_dict(data)

        assert info.http_port is None
        assert info.recovered is False
