"""Assertion and waiting utilities for e2e tests.

Provides wait_with_process_check and related helpers.
"""

import time
from typing import Callable, TypeVar

from .orchestrator_process import OrchestratorProcess

T = TypeVar("T")

# Error patterns that indicate immediate failure (no point waiting)
FATAL_ERROR_PATTERNS = [
    "Traceback (most recent call last):",
    "FATAL:",
    "panic:",
    "Can't trigger event",  # State machine errors
    "RecursionError:",
    "MemoryError:",
    "session.failed",
    "session.start_failed",
    "ended without completion",
    "Terminated without completion record",
    "Timed out without completion record",
    "No completion record found",
]


def wait_with_process_check(
    condition_fn: Callable[[], T | None],
    timeout: int,
    orchestrator: OrchestratorProcess | None = None,
    interval: int = 2,  # Reduced from 5s for faster feedback
    description: str = "condition",
    show_progress: bool = True,
    snapshot_callback: Callable[[str], None] | None = None,
) -> T | None:
    """Wait for a condition with orchestrator health checks and progress.

    Args:
        condition_fn: Function that returns truthy value when condition is met, None otherwise
        timeout: Maximum time to wait in seconds
        orchestrator: If provided, fails fast if process crashes or logs errors
        interval: Polling interval in seconds
        description: Description for error messages and progress
        show_progress: If True, print progress every 10 seconds
        snapshot_callback: If provided, called with reason when logs should be snapshotted

    Returns:
        The truthy return value from condition_fn, or None on timeout

    Raises:
        RuntimeError: If orchestrator process crashes or logs fatal errors
    """
    start = time.time()
    last_progress = start
    last_snapshot = start

    while time.time() - start < timeout:
        elapsed = time.time() - start

        # Fast failure detection: check if orchestrator crashed
        if orchestrator is not None:
            if not orchestrator.is_running():
                stdout, stderr = orchestrator.stop()
                raise RuntimeError(
                    f"Orchestrator crashed while waiting for {description}.\n"
                    f"Log file: {orchestrator.log_path}\n"
                    f"stdout tail: {stdout[-1000:] if stdout else '(empty)'}\n"
                    f"stderr tail: {stderr[-1000:] if stderr else '(empty)'}"
                )

            # Check for fatal errors in recent log output
            recent_logs = "\n".join(orchestrator._output_lines[-20:])
            for pattern in FATAL_ERROR_PATTERNS:
                if pattern in recent_logs:
                    raise RuntimeError(
                        f"Fatal error detected while waiting for {description}:\n"
                        f"Pattern: {pattern}\n"
                        f"Log file: {orchestrator.log_path}\n"
                        f"Recent output:\n{recent_logs[-500:]}"
                    )
            if (
                orchestrator.last_log_age_seconds() > 20
                and (time.time() - last_snapshot) > 30
                and snapshot_callback is not None
            ):
                snapshot_callback(f"stall={description}")
                last_snapshot = time.time()

        result = condition_fn()
        if result:
            return result

        # Show progress every 10 seconds
        if show_progress and time.time() - last_progress >= 10:
            remaining = timeout - elapsed
            print(f"    ... waiting for {description} ({elapsed:.0f}s elapsed, {remaining:.0f}s remaining)", flush=True)
            last_progress = time.time()

        time.sleep(interval)

    # Timeout - provide helpful debug info
    if orchestrator and orchestrator.log_path:
        print(f"    [TIMEOUT] {description} after {timeout}s. Check: {orchestrator.log_path}", flush=True)
    return None
