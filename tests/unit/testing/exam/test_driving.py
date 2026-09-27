"""The drive and settle loops, with an injected clock (no real waiting)."""

from __future__ import annotations

import asyncio

from issue_orchestrator.testing.exam import (
    RunEnd,
    TechLeadActionDisposition,
    TechLeadActionFact,
    TechLeadRunFact,
)
from issue_orchestrator.testing.exam.tech_lead import actions_resolved
from tests.e2e.exam.driving import drive, settle


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds


class _Engine:
    def __init__(self, *, pending: list[int], active: int = 0, running: bool = True) -> None:
        self.pending = pending  # pending_work() per call, last value repeats
        self.active = active
        self.running = running
        self.calls = 0

    def is_running(self) -> bool:
        return self.running

    def active_sessions(self) -> int:
        return self.active

    def pending_work(self) -> int:
        self.calls += 1
        return self.pending[min(self.calls - 1, len(self.pending) - 1)]

    def progress_events(self) -> int:
        return 7  # nothing ever changes


async def _never() -> bool:
    return False


def _drive(engine: _Engine, clock: _Clock, **kwargs) -> RunEnd:
    return asyncio.run(
        drive(engine, done=_never, clock=clock, sleep=clock.sleep, poll_s=10, **kwargs)
    )


def test_queued_work_keeps_the_run_going_past_the_quiet_window() -> None:
    """Round 10 F2: quiet, no sessions — but a review is queued."""
    clock = _Clock()
    engine = _Engine(pending=[1])

    assert _drive(engine, clock, quiet_s=30, timeout_s=300) is RunEnd.TIMEOUT
    assert clock.now >= 300


def test_quiet_with_nothing_queued_is_quiescent() -> None:
    clock = _Clock()
    assert _drive(_Engine(pending=[0]), clock, quiet_s=30, timeout_s=300) is RunEnd.QUIESCENT
    assert clock.now < 300


def test_quiescence_waits_for_the_queue_to_drain() -> None:
    clock = _Clock()
    engine = _Engine(pending=[1, 1, 1, 0])
    assert _drive(engine, clock, quiet_s=30, timeout_s=300) is RunEnd.QUIESCENT
    assert engine.calls == 4


def test_an_exited_engine_ends_the_run_first() -> None:
    assert _drive(_Engine(pending=[0], running=False), _Clock(), quiet_s=30, timeout_s=300) is RunEnd.ENGINE_EXITED


def _run(*dispositions: TechLeadActionDisposition, phase: str = "needs_human") -> TechLeadRunFact:
    return TechLeadRunFact(
        run_id="r", anchor_issue_number=1, flavor="failure_investigation", phase=phase, detail="",
        summary="", findings_text="", report_text="",
        actions=tuple(TechLeadActionFact("escalate_to_human", 1, "b", d) for d in dispositions),
    )


def test_settle_waits_for_a_receipt_that_lands_late() -> None:
    """Round 10 F3: the execution receipt lands after 120s; settle sees it."""
    clock = _Clock()
    runs = {"now": [_run(TechLeadActionDisposition.UNKNOWN)]}

    async def scenario() -> bool:
        async def receipt_after_200s(seconds: float) -> None:
            await clock.sleep(seconds)
            if clock.now >= 200:
                runs["now"] = [_run(TechLeadActionDisposition.EXECUTED)]

        return await settle(
            lambda: actions_resolved(runs["now"]), timeout_s=300, poll_s=15, clock=clock, sleep=receipt_after_200s
        )

    assert asyncio.run(scenario()) is True
    assert 200 <= clock.now < 300


def test_settle_gives_up_at_its_deadline() -> None:
    clock = _Clock()
    resolved = asyncio.run(
        settle(lambda: False, timeout_s=60, poll_s=15, clock=clock, sleep=clock.sleep)
    )
    assert resolved is False and clock.now >= 60


def test_only_concluded_runs_must_resolve() -> None:
    assert actions_resolved([_run(TechLeadActionDisposition.UNKNOWN, phase="running")])
    assert not actions_resolved([_run(TechLeadActionDisposition.UNKNOWN)])
    assert actions_resolved(
        [_run(TechLeadActionDisposition.EXECUTED, TechLeadActionDisposition.PROPOSED, TechLeadActionDisposition.REJECTED)]
    )
