"""Capture Case U's restart window from the candidate engine.

The window runs from the candidate's start until the harness releases the
held work. Nothing in flight can finish inside it, because every scripted
agent is still holding. So any comment it posts or hold label it adds is the
restart's own doing, whenever in the window it happens. It spans at least
``min_ticks`` ticks, so the engine had time to act.

The window is closed by what the harness reads, in order:
1. the engine's cumulative GitHub audit report;
2. its complete event history from its first event (startup restore
   publishes before any watcher connects).
Only then is the work released. The order makes the two coherent: every
write the audit counted has its event in the later history. A write after
the audit shows up as an event only, and it is still inside the window.
Everything read belongs to the window by construction; no timing boundary
has to be guessed.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Mapping, Protocol, Sequence

from issue_orchestrator.testing.exam.upgrade import (
    UpgradeFacts,
    WriteKind,
    complete_history,
    hazard_events,
    label_changes,
    merged_by_id,
    writes_by_kind,
)


class WindowEngine(Protocol):
    def is_running(self) -> bool: ...
    def event_history(self) -> list[dict[str, Any]]: ...
    def gh_audit_report(self) -> dict[str, Any]: ...


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
    then close the window: the audit report first, then the history."""
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
    report = engine.gh_audit_report()
    events = complete_history(engine.event_history())
    return RestartWindow(
        events=tuple(events), writes=writes_by_kind(report["by_command"]), engine_alive=True
    )


def upgrade_facts(
    window: RestartWindow,
    *,
    whole_run: Sequence[Mapping[str, Any]],
    base_commit: str,
    candidate_commit: str,
    sessions_at_stop: tuple[int, ...],
) -> UpgradeFacts:
    """The facts Case U grades: the window's writes and label changes, and
    every restore hazard of the candidate's run (the window's history merged
    with the rest of the run by event id)."""
    return UpgradeFacts(
        base_commit=base_commit,
        candidate_commit=candidate_commit,
        sessions_at_stop=sessions_at_stop,
        early_ticks=window.ticks,
        early_writes=window.writes,
        early_label_changes=label_changes(window.events),
        hazards=hazard_events(merged_by_id(window.events, whole_run)),
    )
