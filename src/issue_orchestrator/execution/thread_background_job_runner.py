"""Thread-backed :class:`BackgroundJobRunner` implementation.

Uses :mod:`threading` rather than a process pool on purpose: the work we need
to offload (review exchange) spawns its own subprocesses via ``subprocess.run``
and mostly blocks in I/O waiting on them. One daemon thread per outstanding
job is simpler than a future/queue dance and gives us exact visibility over
which job_ids are in flight.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

from ..ports.background_job import CompletedJob

logger = logging.getLogger(__name__)


class ThreadBackgroundJobRunner:
    """Run background jobs on daemon threads keyed by stable ``job_id``."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._running: dict[str, threading.Thread] = {}
        self._completed: list[CompletedJob] = []

    def submit(self, job_id: str, fn: Callable[[], None]) -> bool:
        with self._lock:
            existing = self._running.get(job_id)
            if existing is not None and existing.is_alive():
                logger.debug("[BG] submit rejected (already running): %s", job_id)
                return False
            # Either no thread ever ran, or the previous one finished. Clear the
            # slot so the new thread takes ownership cleanly.
            self._running.pop(job_id, None)
            thread = threading.Thread(
                target=self._run_job,
                args=(job_id, fn),
                name=f"bgjob-{job_id}",
                daemon=True,
            )
            self._running[job_id] = thread
        thread.start()
        logger.info("[BG] started job_id=%s", job_id)
        return True

    def is_running(self, job_id: str) -> bool:
        with self._lock:
            thread = self._running.get(job_id)
            return thread is not None and thread.is_alive()

    def drain_completed(self) -> list[CompletedJob]:
        with self._lock:
            done = self._completed
            self._completed = []
        return done

    def _run_job(self, job_id: str, fn: Callable[[], None]) -> None:
        error: BaseException | None = None
        try:
            fn()
        except BaseException as exc:  # noqa: BLE001 — we capture every error for the caller
            error = exc
            logger.exception("[BG] job_id=%s raised", job_id)
        with self._lock:
            self._completed.append(CompletedJob(job_id=job_id, error=error))
            # Leave self._running entry; it will be replaced on next submit. The
            # `is_alive()` check in is_running distinguishes live vs. finished.
        if error is None:
            logger.info("[BG] completed job_id=%s", job_id)

    def wait_until_idle(self, timeout: float = 5.0) -> bool:
        """Block the caller until no jobs are running or *timeout* elapses.

        Returns True if the runner reached idle, False on timeout. This is a
        deterministic readiness hook for tests and graceful-shutdown paths;
        production code should rely on ``is_running`` / ``drain_completed``
        from the tick loop and never block.
        """
        with self._lock:
            threads = [t for t in self._running.values() if t.is_alive()]
        for thread in threads:
            thread.join(timeout=timeout)
            if thread.is_alive():
                return False
        return True
