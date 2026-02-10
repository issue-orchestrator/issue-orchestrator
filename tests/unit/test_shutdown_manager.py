"""Tests for the centralized shutdown manager.

These tests verify:
1. State machine transitions
2. Idempotent cleanup
3. Lock release
4. Thread safety
5. Callback execution
"""

import threading

from pathlib import Path
from unittest.mock import patch

import pytest

from src.issue_orchestrator.control.shutdown_manager import (
    ShutdownManager,
    ShutdownState,
    shutdown_manager,
)

@pytest.fixture(autouse=True)
def reset_singleton():
    """Reset the singleton before and after each test."""
    manager = ShutdownManager()
    manager.reset()
    yield
    manager.reset()

@pytest.fixture
def fresh_manager():
    """Get a fresh shutdown manager for each test."""
    manager = ShutdownManager()
    return manager

@pytest.fixture
def temp_lock_file(tmp_path: Path):
    """Create a temporary lock file."""
    state_dir = tmp_path / ".issue-orchestrator" / "state"
    state_dir.mkdir(parents=True)
    lock_file = state_dir / "lock.json"
    lock_file.write_text('{"pid": 12345, "http_port": 8080}')
    return tmp_path

class TestShutdownManagerState:
    """Test state machine transitions."""

    def test_initial_state_is_running(self, fresh_manager: ShutdownManager):
        """Manager starts in RUNNING state."""
        assert fresh_manager.state == ShutdownState.RUNNING
        assert fresh_manager.shutdown_requested is False

    def test_request_shutdown_changes_state(self, fresh_manager: ShutdownManager):
        """request_shutdown transitions to SHUTDOWN_REQUESTED."""
        result = fresh_manager.request_shutdown(reason="test")

        assert result is True
        assert fresh_manager.state == ShutdownState.SHUTDOWN_REQUESTED
        assert fresh_manager.shutdown_requested is True

    def test_request_shutdown_is_idempotent(self, fresh_manager: ShutdownManager):
        """Multiple shutdown requests don't change state."""
        fresh_manager.request_shutdown(reason="first")
        result = fresh_manager.request_shutdown(reason="second")

        assert result is False  # Second request returns False
        assert fresh_manager.state == ShutdownState.SHUTDOWN_REQUESTED

    def test_cleanup_changes_state_to_shutting_down(self, fresh_manager: ShutdownManager):
        """cleanup() transitions to SHUTTING_DOWN."""
        fresh_manager.request_shutdown(reason="test")
        fresh_manager.cleanup()

        assert fresh_manager.state == ShutdownState.SHUTTING_DOWN

    def test_cleanup_is_idempotent(self, fresh_manager: ShutdownManager):
        """Multiple cleanup calls are safe."""
        fresh_manager.request_shutdown(reason="test")

        result1 = fresh_manager.cleanup()
        result2 = fresh_manager.cleanup()

        assert result1 is True  # First cleanup runs
        assert result2 is False  # Second is a no-op

class TestShutdownManagerCallbacks:
    """Test cleanup callback execution."""

    def test_callbacks_are_called_during_cleanup(self, fresh_manager: ShutdownManager):
        """Registered callbacks are called during cleanup."""
        called = []

        fresh_manager.add_cleanup_callback(lambda: called.append("first"))
        fresh_manager.add_cleanup_callback(lambda: called.append("second"))

        fresh_manager.cleanup()

        # LIFO order
        assert called == ["second", "first"]

    def test_callback_exceptions_dont_stop_cleanup(self, fresh_manager: ShutdownManager):
        """Exceptions in callbacks don't prevent other callbacks."""
        called = []

        fresh_manager.add_cleanup_callback(lambda: called.append("first"))
        fresh_manager.add_cleanup_callback(lambda: (_ for _ in ()).throw(ValueError("boom")))
        fresh_manager.add_cleanup_callback(lambda: called.append("third"))

        # Should not raise
        fresh_manager.cleanup()

        # Both non-raising callbacks should have run
        assert "first" in called
        assert "third" in called

class TestShutdownManagerLockRelease:
    """Test lock file cleanup."""

    def test_initialize_sets_repo_root(self, fresh_manager: ShutdownManager, tmp_path: Path):
        """initialize() sets the repo root."""
        fresh_manager.initialize(tmp_path)

        assert fresh_manager.repo_root == str(tmp_path)

    def test_cleanup_releases_lock(self, fresh_manager: ShutdownManager, temp_lock_file: Path):
        """cleanup() releases the repository lock."""
        fresh_manager.initialize(temp_lock_file)

        # Mock the release_lock function where it's imported from
        with patch("src.issue_orchestrator.infra.repo_lock.release_lock") as mock_release:
            mock_release.return_value = True
            fresh_manager.cleanup()

            mock_release.assert_called_once_with(str(temp_lock_file))

    def test_cleanup_without_repo_root_is_safe(self, fresh_manager: ShutdownManager):
        """cleanup() works even without repo_root set."""
        # Don't initialize - repo_root is None
        # Should not raise
        fresh_manager.cleanup()

class TestShutdownManagerThreadSafety:
    """Test thread safety of shutdown manager."""

    def test_concurrent_shutdown_requests(self, fresh_manager: ShutdownManager):
        """Only one shutdown request should succeed."""
        results = []

        def request():
            result = fresh_manager.request_shutdown(reason="thread")
            results.append(result)

        threads = [threading.Thread(target=request) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Exactly one should return True
        assert results.count(True) == 1
        assert results.count(False) == 9

    def test_concurrent_cleanup_calls(self, fresh_manager: ShutdownManager):
        """Only one cleanup should actually run."""
        results = []

        def cleanup():
            result = fresh_manager.cleanup()
            results.append(result)

        threads = [threading.Thread(target=cleanup) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Exactly one should return True
        assert results.count(True) == 1
        assert results.count(False) == 9

class TestShutdownManagerExit:
    """Test the exit() method."""

    def test_exit_runs_cleanup_before_exiting(self, fresh_manager: ShutdownManager):
        """exit() should cleanup before calling os._exit."""
        cleanup_called = []
        fresh_manager.add_cleanup_callback(lambda: cleanup_called.append(True))

        with patch("os._exit") as mock_exit:
            fresh_manager.exit(0)

            assert cleanup_called == [True]
            mock_exit.assert_called_once_with(0)

    def test_exit_is_idempotent(self, fresh_manager: ShutdownManager):
        """Multiple exit() calls should only cleanup once."""
        cleanup_count = []
        fresh_manager.add_cleanup_callback(lambda: cleanup_count.append(1))

        with patch("os._exit"):
            fresh_manager.exit(0)
            # State is now EXITED, cleanup already done

        # Second exit should still call os._exit but not cleanup again
        with patch("os._exit") as mock_exit:
            fresh_manager.exit(0)
            mock_exit.assert_called_once()

        # Cleanup only ran once
        assert len(cleanup_count) == 1

class TestShutdownManagerSingleton:
    """Test singleton behavior."""

    def test_singleton_pattern(self):
        """ShutdownManager is a singleton."""
        m1 = ShutdownManager()
        m2 = ShutdownManager()

        assert m1 is m2

    def test_global_instance_is_singleton(self):
        """shutdown_manager global is the singleton instance."""
        assert shutdown_manager is ShutdownManager()

class TestTimingRaceConditions:
    """Tests for race conditions we've encountered."""

    def test_rapid_shutdown_requests(self, fresh_manager: ShutdownManager):
        """Rapid shutdown requests should be handled correctly."""
        # Simulate rapid shutdown requests from different sources
        results = []

        def rapid_requests():
            for i in range(100):
                result = fresh_manager.request_shutdown(reason=f"request-{i}")
                results.append(result)

        thread = threading.Thread(target=rapid_requests)
        thread.start()
        thread.join()

        # Only the first should succeed
        assert results[0] is True
        assert all(r is False for r in results[1:])

    def test_cleanup_during_request(self, fresh_manager: ShutdownManager):
        """Cleanup starting while request is in progress."""
        request_done = threading.Event()
        cleanup_done = threading.Event()

        def do_request():
            fresh_manager.request_shutdown(reason="request")
            request_done.set()

        def do_cleanup():
            request_done.wait(timeout=1)
            fresh_manager.cleanup()
            cleanup_done.set()

        t1 = threading.Thread(target=do_request)
        t2 = threading.Thread(target=do_cleanup)

        t1.start()
        t2.start()
        t1.join()
        t2.join()

        # Should end up in SHUTTING_DOWN state
        assert fresh_manager.state == ShutdownState.SHUTTING_DOWN
        # noqa: SLF001 - Verifying cleanup occurs exactly once during race
        assert fresh_manager._cleanup_done is True  # noqa: SLF001

    def test_timer_vs_direct_exit_race(self, fresh_manager: ShutdownManager):
        """Simulate the race between timer-based exit and direct exit.

        This is the bug we had: /api/shutdown schedules exit via timer,
        but run_with_web_dashboard exits directly. Both should be safe.
        """
        exit_count = []
        start_timer = threading.Event()

        with patch("os._exit") as mock_exit:
            mock_exit.side_effect = lambda code: exit_count.append(code)

            # Simulate timer scheduling exit
            def timer_exit():
                start_timer.wait(timeout=1)
                fresh_manager.exit(0)

            timer_thread = threading.Thread(target=timer_exit)
            timer_thread.start()

            # Simulate direct exit (should win the race)
            start_timer.set()
            fresh_manager.exit(0)

            timer_thread.join(timeout=1)

        # Both paths tried to exit, but cleanup only happened once
        # (os._exit was called, but that's mocked)
        # The important thing is no exception was raised
        assert fresh_manager._cleanup_done is True  # noqa: SLF001
