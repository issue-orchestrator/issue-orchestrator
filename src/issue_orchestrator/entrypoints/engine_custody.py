"""Engine work that waits for state custody: off the event loop, never dropped.

Why this exists (#8222): an engine command — pause, resume, refresh, shutdown —
is applied under ``Orchestrator.state_lock``, and a running tick holds that lock
for the whole tick (tens of seconds routinely, minutes at worst). Waiting for it
on the engine's event loop froze every route and SSE stream, and the Control
Center's forward timed out with an empty error.

Every such wait goes through this module, which guarantees three things:

* **Off the loop.** The wait runs in a worker thread, so the engine keeps
  serving while it lasts.
* **Never dropped.** The work runs as its own task, shielded from the route or
  callback that started it and strongly referenced until it finishes. A caller
  that gives up — the Control Center's budget, the supervisor's 2s shutdown
  request, a closed tab — cannot cancel it, even before a worker picks it up.
  That is what makes the Control Center's "may still take effect" true.
* **In order, for pause/resume.** Operator transitions commit in arrival order
  on one worker. Waiters on a lock wake in no defined order, so independent
  threads could commit "pause, then resume" as resume-then-pause; the old
  inline wait gave this ordering for free by blocking the loop.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from concurrent.futures import ThreadPoolExecutor
from typing import Any, TypeVar

_T = TypeVar("_T")

_IN_FLIGHT: set[asyncio.Future[Any]] = set()
_TRANSITIONS = ThreadPoolExecutor(max_workers=1, thread_name_prefix="engine-pause-transition")


def detach(work: Coroutine[Any, Any, _T]) -> asyncio.Future[_T]:
    """Run ``work`` as a task no caller can cancel; keep it alive until done."""
    task = asyncio.ensure_future(work)
    _IN_FLIGHT.add(task)
    task.add_done_callback(_IN_FLIGHT.discard)
    return task


async def await_detached(work: Coroutine[Any, Any, _T]) -> _T:
    """Await ``work`` without letting this caller's cancellation reach it."""
    return await asyncio.shield(detach(work))


async def off_loop(apply: Callable[[], _T]) -> _T:
    """Run a state-custody wait in a worker thread, uncancellable."""
    return await await_detached(asyncio.to_thread(apply))


async def in_arrival_order(apply: Callable[[], _T]) -> _T:
    """Run an operator pause/resume on the one ordered worker, uncancellable."""
    future = asyncio.wrap_future(_TRANSITIONS.submit(apply))
    _IN_FLIGHT.add(future)
    future.add_done_callback(_IN_FLIGHT.discard)
    return await asyncio.shield(future)


class SignalShutdowns:
    """Signal-driven shutdown requests: escalation and the final server stop.

    Every signal after the first escalates to a forced shutdown, decided when
    the signal arrives rather than when its request runs — two quick signals
    must not both read "not yet shutting down". The server is stopped only
    after the last outstanding request has been applied, so a graceful request
    finishing first cannot exit the process under a forced one still stopping
    sessions. Lives on the event loop thread; no locking needed.
    """

    def __init__(self) -> None:
        self._seen = 0
        self._outstanding = 0

    def admit(self) -> bool:
        """Record one signal, synchronously; ``True`` if it must force.

        Called from the signal callback itself, so the escalation and the
        outstanding count are settled before any request has run.
        """
        self._seen += 1
        self._outstanding += 1
        return self._seen > 1

    async def apply(self, request: Callable[[], None], stop_server: Callable[[], None]) -> None:
        """Apply one admitted request off the loop; the last one stops the server."""
        try:
            await asyncio.to_thread(request)
        finally:
            self._outstanding -= 1
        if self._outstanding == 0:
            stop_server()


__all__ = ["SignalShutdowns", "await_detached", "detach", "in_arrival_order", "off_loop"]
