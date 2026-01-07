"""Centralized shutdown manager for orchestrator processes.

This module provides a single source of truth for shutdown state and cleanup.
All exit paths should go through the ShutdownManager to ensure proper cleanup.

Why this exists:
- os._exit() skips atexit handlers, so we need explicit cleanup
- Multiple exit paths (API shutdown, signal handlers, errors) need coordination
- Race conditions between timers and exit paths caused stale locks
- A centralized manager ensures cleanup happens exactly once

Usage:
    from ..control.shutdown_manager import shutdown_manager

    # At startup
    shutdown_manager.initialize(repo_root="/path/to/repo")

    # When shutdown is requested
    shutdown_manager.request_shutdown(reason="API request")

    # Actually exit (releases lock, calls os._exit)
    shutdown_manager.exit()
"""

import atexit
import logging
import os
import signal
import threading
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from types import FrameType
from typing import Callable, Optional

logger = logging.getLogger(__name__)


class ShutdownState(Enum):
    """Shutdown state machine states."""
    RUNNING = "running"
    SHUTDOWN_REQUESTED = "shutdown_requested"
    SHUTTING_DOWN = "shutting_down"
    EXITED = "exited"


@dataclass
class ShutdownManager:
    """Centralized manager for orchestrator shutdown.

    Thread-safe singleton that coordinates all shutdown activities.
    Ensures lock cleanup happens exactly once, regardless of exit path.
    """

    _instance: Optional["ShutdownManager"] = None
    _lock = threading.Lock()

    def __new__(cls) -> "ShutdownManager":
        """Singleton pattern - only one shutdown manager per process."""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    instance = super().__new__(cls)
                    instance._initialized = False
                    cls._instance = instance
        return cls._instance

    def __init__(self) -> None:
        """Initialize shutdown manager (idempotent)."""
        if getattr(self, "_initialized", False):
            return

        self._state = ShutdownState.RUNNING
        self._state_lock = threading.Lock()
        self._repo_root: Optional[str] = None
        self._cleanup_done = False
        self._shutdown_reason: Optional[str] = None
        self._callbacks: list[Callable[[], None]] = []
        self._initialized = True

        # Register atexit handler as fallback (won't run with os._exit, but helps with sys.exit)
        atexit.register(self._atexit_cleanup)

        logger.debug("[shutdown] ShutdownManager initialized")

    def initialize(self, repo_root: str | Path) -> None:
        """Initialize with repo root for lock cleanup.

        Must be called during orchestrator startup.

        Args:
            repo_root: Path to the repository root (for lock file cleanup)
        """
        self._repo_root = str(repo_root) if repo_root else None
        logger.info("[shutdown] Initialized for repo: %s", self._repo_root)

    @property
    def state(self) -> ShutdownState:
        """Current shutdown state."""
        return self._state

    @property
    def shutdown_requested(self) -> bool:
        """Whether shutdown has been requested."""
        return self._state != ShutdownState.RUNNING

    @property
    def repo_root(self) -> Optional[str]:
        """Repo root path for lock cleanup."""
        return self._repo_root

    def add_cleanup_callback(self, callback: Callable[[], None]) -> None:
        """Add a callback to run during cleanup.

        Callbacks are run in LIFO order (last added, first called).
        Exceptions in callbacks are logged but don't prevent cleanup.
        """
        self._callbacks.append(callback)

    def request_shutdown(self, reason: str = "unknown") -> bool:
        """Request graceful shutdown.

        Args:
            reason: Why shutdown was requested (for logging)

        Returns:
            True if this was the first request, False if already shutting down
        """
        with self._state_lock:
            if self._state != ShutdownState.RUNNING:
                logger.debug("[shutdown] Already shutting down, ignoring request: %s", reason)
                return False

            self._state = ShutdownState.SHUTDOWN_REQUESTED
            self._shutdown_reason = reason
            logger.info("[shutdown] Shutdown requested: %s", reason)
            return True

    def _run_cleanup_callbacks(self) -> None:
        """Run registered cleanup callbacks."""
        for callback in reversed(self._callbacks):
            try:
                callback()
            except Exception as e:
                logger.warning("[shutdown] Cleanup callback failed: %s", e)

    def _release_lock(self) -> None:
        """Release the repository lock if we have one."""
        if not self._repo_root:
            logger.debug("[shutdown] No repo_root set, skipping lock release")
            return

        try:
            from ..infra.repo_lock import release_lock
            release_lock(self._repo_root)
            logger.info("[shutdown] Lock released for %s", self._repo_root)
        except Exception as e:
            logger.warning("[shutdown] Failed to release lock: %s", e)

    def cleanup(self) -> bool:
        """Run cleanup (lock release, callbacks). Idempotent.

        Returns:
            True if cleanup was performed, False if already done
        """
        with self._state_lock:
            if self._cleanup_done:
                logger.debug("[shutdown] Cleanup already done")
                return False

            self._state = ShutdownState.SHUTTING_DOWN
            self._cleanup_done = True

        logger.info("[shutdown] Running cleanup...")

        # Run callbacks first (in case they need the lock)
        self._run_cleanup_callbacks()

        # Release the lock
        self._release_lock()

        logger.info("[shutdown] Cleanup complete")
        return True

    def exit(self, code: int = 0) -> None:
        """Exit the process after cleanup.

        This is the single exit point for the orchestrator.
        Always use this instead of os._exit() or sys.exit().

        Args:
            code: Exit code (default 0)
        """
        with self._state_lock:
            if self._state == ShutdownState.EXITED:
                # Already exiting - just call os._exit to ensure we exit
                os._exit(code)
                return
            self._state = ShutdownState.EXITED

        logger.info("[shutdown] Exiting with code %d (reason: %s)",
                   code, self._shutdown_reason or "unknown")

        # Ensure cleanup runs
        self.cleanup()

        # Exit immediately (os._exit skips atexit, but we've already cleaned up)
        os._exit(code)

    def _atexit_cleanup(self) -> None:
        """Atexit handler as fallback cleanup.

        This runs if the process exits via sys.exit() or normal termination.
        Won't run with os._exit(), but we call cleanup() explicitly there.
        """
        if not self._cleanup_done:
            logger.debug("[shutdown] Running atexit cleanup")
            self.cleanup()

    def install_signal_handlers(self) -> None:
        """Install signal handlers for graceful shutdown.

        Handles SIGTERM and SIGINT to trigger graceful shutdown.
        """
        def signal_handler(signum: int, frame: FrameType | None) -> None:
            sig_name = signal.Signals(signum).name
            logger.info("[shutdown] Received signal %s", sig_name)
            self.request_shutdown(reason=f"signal {sig_name}")
            # Don't exit here - let the main loop handle the shutdown

        signal.signal(signal.SIGTERM, signal_handler)
        signal.signal(signal.SIGINT, signal_handler)
        logger.debug("[shutdown] Signal handlers installed")

    def reset(self) -> None:
        """Reset state for testing. DO NOT use in production."""
        with self._state_lock:
            self._state = ShutdownState.RUNNING
            self._cleanup_done = False
            self._shutdown_reason = None
            self._callbacks.clear()
            self._repo_root = None


# Global singleton instance
shutdown_manager = ShutdownManager()
