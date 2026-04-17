"""Consume :class:`BackgroundJobRunner` completions from the main tick.

The raw runner exposes a polling surface (``is_running`` / ``drain_completed``)
but no callers actually drained it, so background job **failures** never
reached the control layer. A crashed review-exchange thread would exit, the
tick would observe no running job and no ``summary.json``, and
:meth:`CompletionReviewExchange.run_review_exchange_if_needed` would happily
resubmit the same job — forever, once per tick, silently. This supervisor
plugs that gap.

Contract:

* :meth:`tick` is called once per main-loop iteration (or whenever the
  orchestrator reaches a safe point to reap completions). It drains the
  runner and records any exceptions under the corresponding ``job_id``.
* :meth:`take_failure` returns and clears a recorded failure so callers
  can surface it as a terminal outcome (e.g. mark the session FAILED)
  instead of spawning a fresh attempt.
* ``submit`` / ``is_running`` delegate to the underlying runner so
  consumers depend on the supervisor and nothing else.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

from ..ports.background_job import BackgroundJobRunner

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BackgroundJobFailure:
    """Terminal error captured for one job_id."""

    job_id: str
    error: BaseException
    recorded_at: float  # Epoch seconds


class BackgroundJobSupervisor:
    """Owner of the ``BackgroundJobRunner`` failure-handling contract."""

    def __init__(
        self,
        runner: BackgroundJobRunner,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._runner = runner
        self._clock = clock
        self._failures: dict[str, BackgroundJobFailure] = {}

    def submit(self, job_id: str, fn: Callable[[], None]) -> bool:
        return self._runner.submit(job_id, fn)

    def is_running(self, job_id: str) -> bool:
        return self._runner.is_running(job_id)

    def tick(self) -> None:
        """Drain completed jobs; store any failures keyed by job_id."""
        for done in self._runner.drain_completed():
            if done.error is None:
                continue
            self._failures[done.job_id] = BackgroundJobFailure(
                job_id=done.job_id,
                error=done.error,
                recorded_at=self._clock(),
            )
            logger.warning(
                "[BG] job_id=%s failed: %s",
                done.job_id,
                done.error,
            )

    def take_failure(self, job_id: str) -> BackgroundJobFailure | None:
        """Return and clear any recorded failure for *job_id*.

        Callers that see a failure here MUST NOT resubmit the same job_id
        without first either surfacing the error as a terminal outcome or
        making a deliberate retry decision. The supervisor deliberately
        "forgets" the failure after returning it, so callers hold the
        responsibility for escalation.
        """
        return self._failures.pop(job_id, None)

    def wait_until_idle(self, timeout: float = 60.0) -> bool:
        """Optional shutdown hook: block until no jobs are running.

        Delegates to the underlying runner when it exposes an idle check
        (``ThreadBackgroundJobRunner`` does); returns ``True`` immediately
        for synchronous adapters with no live state to drain.
        """
        hook = getattr(self._runner, "wait_until_idle", None)
        if not callable(hook):
            return True
        return bool(hook(timeout=timeout))
