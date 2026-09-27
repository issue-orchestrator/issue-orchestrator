"""Letting the real engine work until the case is decided (no e2e imports).

Two waits, both bounded and both event/state driven:

* :func:`drive` — until the case's own ``done``, quiescence (nothing about
  any work item changed for ``quiet_s``, no session running AND no review or
  rework queued), the engine exiting, or the deadline;
* :func:`settle` — until a condition holds or a deadline, e.g. a tech-lead
  decision's actions all resolving after its run ended.

Clock and sleep are injectable so the loops are unit-tested without waiting.
"""

from __future__ import annotations

import asyncio
import time
from typing import Awaitable, Callable, Protocol

from issue_orchestrator.testing.exam import RunEnd


class DrivenEngine(Protocol):
    def is_running(self) -> bool: ...
    def active_sessions(self) -> int: ...
    def pending_work(self) -> int: ...
    def progress_events(self) -> int: ...


async def drive(
    engine: DrivenEngine,
    *,
    done: Callable[[], Awaitable[bool]],
    quiet_s: float,
    timeout_s: float,
    reached: RunEnd = RunEnd.GOAL_REACHED,
    poll_s: float = 20.0,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> RunEnd:
    """Let the engine work until ``done`` (reported as ``reached``),
    quiescence, its exit, or the deadline."""
    started = clock()
    last_count = -1
    last_change = started
    while True:
        if not engine.is_running():
            return RunEnd.ENGINE_EXITED
        if await done():
            return reached
        now = clock()
        count = engine.progress_events()
        if count != last_count:
            last_count, last_change = count, now
        elif (
            now - last_change >= quiet_s
            and engine.active_sessions() == 0
            # Queued work is about to happen: a snapshot now is premature.
            and engine.pending_work() == 0
        ):
            return RunEnd.QUIESCENT
        if now - started >= timeout_s:
            return RunEnd.TIMEOUT
        await sleep(poll_s)


async def settle(
    condition: Callable[[], bool],
    *,
    timeout_s: float,
    poll_s: float = 15.0,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> bool:
    """Wait until ``condition()`` holds; ``False`` if the deadline passed first."""
    deadline = clock() + timeout_s
    while True:
        if condition():
            return True
        if clock() >= deadline:
            return False
        await sleep(poll_s)
