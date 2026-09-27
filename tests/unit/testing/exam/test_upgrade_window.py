"""Case U's harness capture (#7432): the restart window and a failed start."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Mapping, Sequence

import pytest

from issue_orchestrator.testing.exam import grade
from issue_orchestrator.testing.exam.cases import UPGRADE_EARLY_TICKS
from issue_orchestrator.testing.exam.upgrade import WriteKind
from tests.e2e.exam.upgrade_window import RestartWindow, capture_restart_window, upgrade_facts
from tests.unit.testing.exam.test_upgrade import CASE_U, upgrade_observation

COMMENT = "POST /repos/o/r/issues/921/comments"
LABEL = "POST /repos/o/r/issues/921/labels"
NEEDS_HUMAN = {"issue_number": 921, "added": ["io:needs-human"], "removed": []}


class FakeCandidate:
    """A live engine on a fake clock.

    A tick completes every ``reads_per_tick`` history reads. Its actions
    (``schedule``d GitHub writes, each with the event it publishes, if any)
    hit GitHub and publish their events before the tick's own
    ``tick.completed``, as the engine does. The tick in progress at the
    pause may still land its writes (worst case for the window); a tick
    that starts after the pause applies nothing.
    """

    def __init__(
        self,
        startup: list[dict[str, Any]] | None = None,
        *,
        reads_per_tick: int = 1,
        audit: dict[str, int] | None = None,
        buffer_max: int = 1000,
    ) -> None:
        self.buffer_max = buffer_max
        self.history: list[dict[str, Any]] = list(startup or [])
        self.audit: dict[str, int] = dict(audit or {})
        self.reads_per_tick = reads_per_tick
        self.history_reads = 0
        self.scheduled: dict[int, list[tuple[str, tuple[str, dict[str, Any]] | None]]] = {}
        self.ticks = 0
        self.paused = False
        self.paused_after_tick: int | None = None
        self.at_pause = lambda: None
        self.reads: list[str] = []
        self.alive = True
        self.now = 0.0

    def schedule(self, tick: int, command: str, event: tuple[str, dict[str, Any]] | None = None) -> None:
        self.scheduled.setdefault(tick, []).append((command, event))

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds

    def publish(self, event_type: str, payload: dict[str, Any]) -> None:
        self.history.append({"event_id": len(self.history) + 1, "type": event_type, "payload": payload})

    def _complete_tick(self) -> None:
        self.ticks += 1
        started_paused = self.paused_after_tick is not None and self.ticks > self.paused_after_tick + 1
        for command, event in self.scheduled.pop(self.ticks, []):
            if started_paused:
                continue  # a paused engine applies no actions
            self.audit[command] = self.audit.get(command, 0) + 1
            if event is not None:
                self.publish(*event)
        self.publish("tick.completed", {})

    def is_running(self) -> bool:
        return self.alive

    def pause(self) -> None:
        self.reads.append("pause")
        self.paused = True
        self.paused_after_tick = self.ticks
        self.at_pause()

    def event_history(self, *, after: int = 0) -> list[dict[str, Any]]:
        """Like the engine's EventHub: only its newest ``buffer_max`` events."""
        self.reads.append("history")
        buffered = self.history[-self.buffer_max :]
        snapshot = [event for event in buffered if event["event_id"] > after]
        self.history_reads += 1
        if self.history_reads % self.reads_per_tick == 0:
            self._complete_tick()
        return snapshot

    def gh_audit_report(self) -> dict[str, Any]:
        self.reads.append("audit")
        return {"by_command": dict(self.audit)}


def _capture(engine: FakeCandidate) -> RestartWindow:
    return asyncio.run(
        capture_restart_window(
            engine, min_ticks=UPGRADE_EARLY_TICKS, timeout_s=600, clock=engine.clock, sleep=engine.sleep
        )
    )


def _grade(
    window: RestartWindow, whole_run: Sequence[Mapping[str, Any]] | None = None, *, complete: bool = True
):
    facts = upgrade_facts(
        window,
        whole_run=whole_run or [],
        complete=complete,
        base_commit="b" * 40,
        candidate_commit="c" * 40,
        sessions_at_stop=(911, 921),
    )
    return grade(CASE_U, upgrade_observation(facts))


def test_startup_hazards_and_pages_before_any_watcher_fail_the_case() -> None:
    """Round 1 F1: what the candidate does at startup, before a watcher can
    connect, reaches the scorecard through the real capture path, even if
    the label is cleared and the work finishes later."""
    startup = [
        {"event_id": 1, "type": "session.run_unrestorable", "payload": {"issue_number": 921, "cause": "RUN_UNRESTORABLE"}},
        {"event_id": 2, "type": "issue.labels_changed", "payload": NEEDS_HUMAN},
    ]
    engine = FakeCandidate(startup, audit={COMMENT: 1, LABEL: 1})
    window = _capture(engine)
    cleared = [{"event_id": 400, "type": "issue.labels_changed", "payload": {**NEEDS_HUMAN, "added": [], "removed": ["io:needs-human"]}}]

    card = _grade(window, cleared, complete=False)

    assert all(goal.passed for goal in card.goals)
    assert card.failures == (
        "upgrade: restore hazard: session.run_unrestorable on #921 (RUN_UNRESTORABLE)",
        "upgrade: 1 comment(s) posted in the restart window",
        "upgrade: hold label added in the restart window: #921 +io:needs-human",
    )


@pytest.mark.parametrize("reads_per_tick", [1, 3, 8], ids=["tick per read", "tick spans reads", "slow tick"])
def test_writes_up_to_the_pause_count_in_both_reads(reads_per_tick: int) -> None:
    """Round 4 F1: one cutoff for comments (audit) and labels (events). The
    tick in progress at the pause lands its writes and their events before
    the window closes, and both reads see them."""
    engine = FakeCandidate(reads_per_tick=reads_per_tick)

    def writes_in_the_tick_in_progress() -> None:
        in_progress = engine.ticks + 1
        engine.schedule(in_progress, COMMENT)
        engine.schedule(in_progress, LABEL, ("issue.labels_changed", NEEDS_HUMAN))

    engine.at_pause = writes_in_the_tick_in_progress
    window = _capture(engine)

    assert engine.reads.index("pause") < engine.reads.index("audit")
    assert window.writes[WriteKind.COMMENT] == 1
    card = _grade(window)
    assert "upgrade: 1 comment(s) posted in the restart window" in card.failures
    assert "upgrade: hold label added in the restart window: #921 +io:needs-human" in card.failures


def test_a_paused_engine_writes_nothing_more_and_both_reads_agree() -> None:
    """Writes the engine WOULD make after the pause never happen, so neither
    read can see one without the other."""
    engine = FakeCandidate()

    def writes_in_every_later_tick() -> None:
        for later in range(engine.ticks + 2, engine.ticks + 30):
            engine.schedule(later, COMMENT)
            engine.schedule(later, LABEL, ("issue.labels_changed", NEEDS_HUMAN))

    engine.at_pause = writes_in_every_later_tick
    window = _capture(engine)

    assert window.writes[WriteKind.COMMENT] == 0
    assert window.writes[WriteKind.LABEL_ADD] == 0
    assert _grade(window).passed


def test_a_candidate_that_dies_in_the_window_fails_on_ticks() -> None:
    engine = FakeCandidate()
    engine.alive = False
    card = _grade(_capture(engine), complete=False)
    assert (
        f"upgrade: candidate completed only 0 of {UPGRADE_EARLY_TICKS} ticks before the held work was released"
        in card.failures
    )


class HoleyHistory(FakeCandidate):
    """From the pause on, returns its history with id 2 missing for
    ``holey_reads`` reads (a publisher still in flight)."""

    def __init__(self, *args: Any, holey_reads: int, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.holey_reads = holey_reads

    def event_history(self, *, after: int = 0) -> list[dict[str, Any]]:
        history = super().event_history(after=after)
        if self.paused and self.holey_reads:
            self.holey_reads -= 1
            return [event for event in history if event["event_id"] != 2]
        return list(reversed(history))  # complete, but buffered out of order


STARTUP = [
    {"event_id": 1, "type": "session.run_unrestorable", "payload": {"issue_number": 921, "cause": "X"}},
    {"event_id": 2, "type": "issue.labels_changed", "payload": {"issue_number": 911, "added": [], "removed": ["stale"]}},
]


def test_a_briefly_holey_history_is_retried_and_graded_in_id_order() -> None:
    """Round 2 F3 / round 3 F2: a hole a concurrent publisher leaves for a
    moment is waited out; the complete history is graded in id order."""
    window = _capture(HoleyHistory(STARTUP, holey_reads=3))

    assert [event["event_id"] for event in window.events] == list(range(1, len(window.events) + 1))
    assert _grade(window).failures == ("upgrade: restore hazard: session.run_unrestorable on #921 (X)",)


def test_a_persistent_hole_is_refused() -> None:
    with pytest.raises(ValueError, match="incomplete"):
        _capture(HoleyHistory(STARTUP, holey_reads=10_000))


class TestWholeRun:
    """Round 4 F2: hazards after the window come from the engine's own tail
    too, not only from what the watcher had processed."""

    def window(self) -> RestartWindow:
        return _capture(FakeCandidate())

    def test_a_hazard_only_the_engine_tail_holds_fails_the_case(self) -> None:
        window = self.window()
        last = len(window.events)
        watcher_saw = [{"event_id": last + 1, "type": "tick.completed", "payload": {}}]
        engine_tail = [
            {"event_id": last + 2, "type": "session.claim_unreadable", "payload": {"issue_number": 911, "cause": "late"}}
        ]
        card = _grade(window, watcher_saw + engine_tail)
        assert card.failures == ("upgrade: restore hazard: session.claim_unreadable on #911 (late)",)

    def test_a_live_engines_run_with_a_hole_is_refused(self) -> None:
        window = self.window()
        last = len(window.events)
        with pytest.raises(ValueError, match="incomplete"):
            _grade(window, [{"event_id": last + 2, "type": "tick.completed", "payload": {}}])


def test_a_failed_start_stops_the_engine_it_launched(monkeypatch, tmp_path: Path) -> None:
    """Round 1 F3: a start that fails after launching the process stops it,
    so the checkout is never removed from under a running engine."""
    from tests.e2e.exam import engine as engine_module
    from tests.e2e.exam.case_engines import case_c_engine
    from tests.e2e.exam.engine_checkout import EngineCheckout
    from tests.unit.testing.exam.test_exam_engine_config import _TREE, _base_config

    checkout = EngineCheckout(root=_TREE, commit="0" * 40)
    spec = case_c_engine()
    exam_engine = spec.engine(
        spec.config(_base_config(tmp_path), checkout=checkout, run_label="io:e2e:exam-test"), checkout
    )
    stopped: list[bool] = []

    class LaunchedProcess:
        def stop(self) -> tuple[str, str]:
            stopped.append(True)
            return "", ""

    monkeypatch.setattr(exam_engine, "process", LaunchedProcess())

    async def launch_then_fail(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("Timed out waiting for control API readiness before watcher startup")

    monkeypatch.setattr(engine_module, "start_orchestrator_runtime", launch_then_fail)

    with pytest.raises(AssertionError, match="control API readiness"):
        asyncio.run(exam_engine.start())
    assert stopped == [True]


class TestPremiseAtStop:
    """Round 5 F1: both sessions must still be live at the stop itself."""

    class Base:
        def __init__(self, reads: list[tuple[tuple[str, int], ...]]) -> None:
            self.reads = reads

        def active_session_issues(self) -> tuple[tuple[str, int], ...]:
            return self.reads.pop(0) if len(self.reads) > 1 else self.reads[0]

    def items(self):
        from tests.e2e.exam.observe import TrackedItem

        return TrackedItem("coding", 911, external_id="M0-763"), TrackedItem("review", 921, external_id="M0-764")

    def test_both_live_at_the_stop(self, monkeypatch) -> None:
        from tests.e2e.exam import scenarios

        monkeypatch.setattr(scenarios, "linked_pull_requests", lambda repo, issue, *, state: [type("PR", (), {"number": 922})()])
        coding, review = self.items()
        both = (("issue-911", 911), ("review-922", 921))
        assert scenarios.in_flight_at_stop(self.Base([both]), "o/r", coding=coding, review=review) == (911, 921)

    @pytest.mark.parametrize(
        "at_stop",
        [(("issue-911", 911),), (("review-922", 921),), ()],
        ids=["review ended", "coder ended", "both ended"],
    )
    def test_a_session_that_ended_since_it_was_seen_is_refused(self, monkeypatch, at_stop) -> None:
        from tests.e2e.exam import scenarios

        monkeypatch.setattr(scenarios, "linked_pull_requests", lambda repo, issue, *, state: [type("PR", (), {"number": 922})()])
        coding, review = self.items()
        with pytest.raises(RuntimeError, match="did not hold at the stop"):
            scenarios.in_flight_at_stop(self.Base([at_stop]), "o/r", coding=coding, review=review)


def test_a_hazard_published_after_the_watchers_last_event_is_graded() -> None:
    """Round 5 F3: the final read quiesces the engine first, so a hazard the
    watcher has not processed (or that is published just before the read)
    is in the graded run."""
    from tests.e2e.exam.upgrade_window import quiesce

    engine = FakeCandidate()
    window = _capture(engine)
    engine.paused = False
    engine.paused_after_tick = None  # the harness resumed after the release
    watcher_saw = list(engine.history)
    late = engine.ticks + 1
    engine.schedule(late, "POST /repos/o/r/issues/911/labels", ("session.claim_unreadable", {"issue_number": 911, "cause": "late"}))

    whole_run = watcher_saw + list(asyncio.run(quiesce(engine, deadline=engine.clock() + 600, clock=engine.clock, sleep=engine.sleep)))

    assert _grade(window, whole_run).failures == ("upgrade: restore hazard: session.claim_unreadable on #911 (late)",)
    assert engine.paused  # stays paused through the observation


class TestLongRun:
    """Round 6 F1: a healthy run longer than the engine's replay buffer."""

    def run(self, *, drop_from_watcher: int | None = None):
        from tests.e2e.exam.upgrade_window import quiesce

        engine = FakeCandidate(buffer_max=50)
        window = _capture(engine)
        engine.paused = False
        engine.paused_after_tick = None  # resumed after the release
        for _ in range(200):  # a long run: the buffer has dropped the window's ids
            engine.publish("tick.completed", {})
        watcher = [event for event in engine.history if event["event_id"] > 3]
        if drop_from_watcher is not None:
            watcher = [event for event in watcher if event["event_id"] != drop_from_watcher]
        tail = asyncio.run(
            quiesce(engine, deadline=engine.clock() + 600, after=watcher[-1]["event_id"], clock=engine.clock, sleep=engine.sleep)
        )
        assert engine.history[0]["event_id"] < len(engine.history) - engine.buffer_max  # prefix really gone
        return _grade(window, [*watcher, *tail])

    def test_a_healthy_long_run_is_graded_from_window_watcher_and_tail(self) -> None:
        assert self.run().passed

    def test_an_event_missing_from_every_source_is_still_refused(self) -> None:
        with pytest.raises(ValueError, match="incomplete"):
            self.run(drop_from_watcher=120)
