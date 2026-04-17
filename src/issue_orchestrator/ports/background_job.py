"""Background job runner port.

Fire-and-forget execution of long-running work (e.g. review-exchange
subprocesses) so that the main orchestrator tick never blocks on it. Callers
submit a callable under a stable ``job_id``, then poll ``is_running`` or
``drain_completed`` on subsequent ticks to learn when the work finished.

This port deliberately exposes a **polling** surface, not a futures/async one,
so it plugs cleanly into the orchestrator's single-threaded tick loop: each
tick checks status in O(1) and advances state accordingly.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class CompletedJob:
    """Outcome of a background job picked up during ``drain_completed``."""

    job_id: str
    error: BaseException | None  # None means the job returned cleanly


@runtime_checkable
class BackgroundJobRunner(Protocol):
    """Port for running work off the main orchestrator thread."""

    def submit(self, job_id: str, fn: Callable[[], None]) -> bool:
        """Start *fn* in the background under *job_id*.

        Returns ``True`` if the job was accepted, ``False`` if a job with the
        same ``job_id`` is already running (caller should not re-submit).
        Idempotent: re-submitting a running job_id is a no-op that returns
        ``False``.
        """
        ...

    def is_running(self, job_id: str) -> bool:
        """Return ``True`` iff a job with *job_id* is currently executing."""
        ...

    def drain_completed(self) -> list[CompletedJob]:
        """Return and forget all jobs that have finished since the last call.

        The caller owns the returned list — after draining, the runner no longer
        tracks those job_ids. Exceptions raised inside a job appear as
        ``error`` on the corresponding ``CompletedJob`` entry.
        """
        ...


class NullBackgroundJobRunner:
    """No-op runner used in tests that never exercise async paths.

    ``submit`` always returns ``False`` — the same return value a real runner
    uses to reject duplicate submissions. Consumers MUST treat a ``False``
    return as "this runner did not start the job", which is the contract
    :class:`CompletionReviewExchange` relies on to fall back to inline
    execution when no real runner is wired. Do NOT "fix" this to run ``fn``
    synchronously and return ``True``: that would turn a test-env fallback
    into a silent swallow everywhere the null runner is used.
    """

    def submit(self, job_id: str, fn: Callable[[], None]) -> bool:
        del job_id, fn
        return False

    def is_running(self, job_id: str) -> bool:
        del job_id
        return False

    def drain_completed(self) -> list[CompletedJob]:
        return []
