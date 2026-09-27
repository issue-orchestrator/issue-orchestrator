"""Capture Case U's restart window from the candidate engine.

The window runs from the candidate's start until the harness PAUSES it,
after at least ``min_ticks`` ticks. Nothing in flight can finish inside the
window, because every scripted agent is still holding. So any comment it
posts or hold label it adds is the restart's own doing.

The pause gives both reads a single cutoff:
- a paused engine applies no actions (the tick in progress stops at its
  next action);
- the held work leaves no off-tick writer anything to do.
Once a tick has completed after the pause, the engine has stopped writing.
The harness then reads the cumulative GitHub audit report and the complete
event history (from its first event: startup restore publishes before any
watcher connects). Both cover exactly the same span. Only then is the work
released and the engine resumed.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Mapping, Protocol, Sequence

from issue_orchestrator.testing.exam.upgrade import (
    UpgradeFacts,
    WriteKind,
    event_id_of,
    complete_history,
    hazard_events,
    label_changes,
    merged_by_id,
    writes_by_kind,
)


class WindowEngine(Protocol):
    def is_running(self) -> bool: ...
    def event_history(self, *, after: int = 0) -> list[dict[str, Any]]: ...
    def gh_audit_report(self) -> dict[str, Any]: ...
    def pause(self) -> None: ...


@dataclass(frozen=True)
class RestartWindow:
    events: Sequence[Mapping[str, Any]]
    """The candidate's complete event history when the window closed."""
    writes: Mapping[WriteKind, int]
    engine_alive: bool

    @property
    def ticks(self) -> int:
        return sum(1 for event in self.events if event.get("type") == "tick.completed")


async def capture_restart_window(
    engine: WindowEngine,
    *,
    min_ticks: int,
    timeout_s: float,
    poll_s: float = 2.0,
    clock=time.monotonic,
    sleep=asyncio.sleep,
) -> RestartWindow:
    """Wait for ``min_ticks`` ticks (or the engine's death, or the backstop),
    then close the window: pause, wait out the tick in progress, read.
    The caller releases the work and resumes the engine."""
    deadline = clock() + timeout_s
    while engine.is_running() and clock() < deadline:
        history = engine.event_history()
        if sum(1 for event in history if event.get("type") == "tick.completed") >= min_ticks:
            break
        await sleep(poll_s)
    if not engine.is_running():
        # A dead engine has no control API; what it did before dying is lost
        # with it, and the grade fails on the tick shortfall and the exit.
        return RestartWindow(events=(), writes={}, engine_alive=False)
    await quiesce(engine, deadline=deadline, poll_s=poll_s, clock=clock, sleep=sleep)
    report = engine.gh_audit_report()
    events = await _settled_history(engine, deadline, poll_s, clock, sleep)
    return RestartWindow(
        events=tuple(events), writes=writes_by_kind(report["by_command"]), engine_alive=True
    )


async def quiesce(
    engine: WindowEngine,
    *,
    deadline: float,
    after: int = 0,
    poll_s: float = 2.0,
    clock=time.monotonic,
    sleep=asyncio.sleep,
) -> Sequence[Mapping[str, Any]]:
    """Pause the engine and wait out the tick in progress; returns its
    complete history past ``after`` at that point, after which it applies
    nothing more.

    ``after`` is how much the caller already holds (the watcher's last id):
    the engine buffers only its newest events, so a long run's history from
    id 1 may be gone from it while the watcher still has it.
    """
    engine.pause()
    at_pause = await _settled_history(engine, deadline, poll_s, clock, sleep, after=after)
    noted = max((event_id_of(event) for event in at_pause), default=after)
    while True:
        settled = await _settled_history(engine, deadline, poll_s, clock, sleep, after=after)
        if any(
            event.get("type") == "tick.completed" and event_id_of(event) > noted for event in settled
        ):
            return settled
        if clock() >= deadline:
            raise RuntimeError("no tick completed after the pause; the engine cannot be quiesced")
        await sleep(poll_s)


async def _settled_history(
    engine: WindowEngine, deadline: float, poll_s: float, clock, sleep, *, after: int = 0
):
    """The complete history past ``after``, retried while a concurrent
    publisher briefly leaves a hole; a hole or a dropped prefix that persists
    is refused."""
    while True:
        try:
            return complete_history(engine.event_history(after=after), after=after)
        except ValueError:
            if clock() >= deadline:
                raise
        await sleep(poll_s)


def upgrade_facts(
    window: RestartWindow,
    *,
    whole_run: Sequence[Mapping[str, Any]],
    complete: bool,
    base_commit: str,
    candidate_commit: str,
    sessions_at_stop: tuple[int, ...],
) -> UpgradeFacts:
    """The facts Case U grades: the window's writes and label changes, and
    every restore hazard of the candidate's run (the window's history merged
    with the rest of the run by event id).

    With ``complete`` (a live engine), the whole run must hold every id from
    the first; a hole means a hazard could be missing, so it is refused. A
    dead engine's run is graded as far as it was seen: it fails on the exit.
    """
    run = merged_by_id(window.events, whole_run)
    if complete:
        run = list(complete_history(run))
    return UpgradeFacts(
        base_commit=base_commit,
        candidate_commit=candidate_commit,
        sessions_at_stop=sessions_at_stop,
        early_ticks=window.ticks,
        early_writes=window.writes,
        early_label_changes=label_changes(window.events),
        hazards=hazard_events(run),
    )
