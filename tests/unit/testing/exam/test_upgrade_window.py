"""Case U's harness capture (#7432): the restart window and a failed start."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from issue_orchestrator.testing.exam import grade
from issue_orchestrator.testing.exam.cases import UPGRADE_EARLY_TICKS
from issue_orchestrator.testing.exam.upgrade import WriteKind
from tests.e2e.exam.upgrade_window import capture_restart_window, upgrade_facts
from tests.unit.testing.exam.test_upgrade import CASE_U, upgrade_observation


def tick(event_id: int) -> dict[str, Any]:
    return {"event_id": event_id, "type": "tick.completed", "payload": {}}


class FakeCandidate:
    """A live engine on a fake clock: a tick completes every
    ``reads_per_tick`` history reads, publishing its actions' events
    (``pending``) before its own ``tick.completed``, as the engine does."""

    def __init__(
        self, startup: list[dict[str, Any]], audit: dict[str, int], *, reads_per_tick: int = 1
    ) -> None:
        self.reads_per_tick = reads_per_tick
        self.history_reads = 0
        self.history: list[dict[str, Any]] = list(startup)
        self.pending: list[tuple[str, dict[str, Any]]] = []
        self.audit = audit
        self.reads: list[str] = []
        self.alive = True
        self.now = 0.0

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds

    def publish(self, event_type: str, payload: dict[str, Any]) -> None:
        self.history.append({"event_id": len(self.history) + 1, "type": event_type, "payload": payload})

    def is_running(self) -> bool:
        return self.alive

    def event_history(self) -> list[dict[str, Any]]:
        self.reads.append("history")
        snapshot = list(self.history)
        self.history_reads += 1
        if self.history_reads % self.reads_per_tick == 0:
            for event_type, payload in self.pending:
                self.publish(event_type, payload)
            self.pending = []
            self.publish("tick.completed", {})
        return snapshot

    def gh_audit_report(self) -> dict[str, Any]:
        self.reads.append("audit")
        return {"by_command": dict(self.audit)}


def _capture(engine: FakeCandidate):
    return asyncio.run(
        capture_restart_window(
            engine,
            min_ticks=UPGRADE_EARLY_TICKS,
            timeout_s=600,
            clock=engine.clock,
            sleep=engine.sleep,
        )
    )


def test_startup_hazards_and_pages_before_any_watcher_fail_the_case() -> None:
    """Finding 1: what the candidate does at startup, before a watcher can
    connect, reaches the scorecard through the real capture path, even if
    the label is cleared and the work finishes later."""
    startup = [
        {"event_id": 1, "type": "session.run_unrestorable", "payload": {"issue_number": 921, "cause": "RUN_UNRESTORABLE"}},
        {"event_id": 2, "type": "issue.labels_changed", "payload": {"issue_number": 921, "added": ["io:needs-human"], "removed": []}},
    ]
    engine = FakeCandidate(startup, audit={"POST /repos/o/r/issues/921/comments": 1})
    window = _capture(engine)
    later = [{"event_id": 40, "type": "issue.labels_changed", "payload": {"issue_number": 921, "added": [], "removed": ["io:needs-human"]}}]

    facts = upgrade_facts(window, whole_run=later, base_commit="b" * 40, candidate_commit="c" * 40, sessions_at_stop=(911, 921))
    card = grade(CASE_U, upgrade_observation(facts))

    assert all(goal.passed for goal in card.goals)
    assert card.failures == (
        "upgrade: restore hazard: session.run_unrestorable on #921 (RUN_UNRESTORABLE)",
        "upgrade: 1 comment(s) posted in the restart window",
        "upgrade: hold label added in the restart window: #921 +io:needs-human",
    )


def test_the_window_closes_audit_first_then_history_and_reads_nothing_after() -> None:
    """Finding 2: the window is closed by the reads themselves (the cumulative
    audit, then the history), before the harness releases the work, so no
    tick boundary has to be timed."""
    engine = FakeCandidate([], audit={"POST /repos/o/r/issues/5/comments": 2})
    window = _capture(engine)

    # One audit read, then history reads only: the one that notes the
    # barrier and the one that finds a tick completed after it.
    audit_at = engine.reads.index("audit")
    assert engine.reads.count("audit") == 1
    assert engine.reads[audit_at + 1 :] == ["history", "history"]
    assert window.ticks >= UPGRADE_EARLY_TICKS
    assert window.writes[WriteKind.COMMENT] == 2


def test_a_candidate_that_dies_in_the_window_fails_on_ticks() -> None:
    engine = FakeCandidate([], audit={})
    engine.alive = False
    window = _capture(engine)

    facts = upgrade_facts(window, whole_run=[], base_commit="b" * 40, candidate_commit="c" * 40, sessions_at_stop=(911, 921))
    card = grade(CASE_U, upgrade_observation(facts))
    assert f"upgrade: candidate completed only 0 of {UPGRADE_EARLY_TICKS} ticks before the held work was released" in card.failures


def test_a_failed_start_stops_the_engine_it_launched(monkeypatch, tmp_path: Path) -> None:
    """Finding 3: a start that fails after launching the process stops it,
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


class LateLabelCandidate(FakeCandidate):
    """Its hold-label write is on GitHub (audited) before its event is
    published; the event lands with the tick in progress at the audit read."""

    def gh_audit_report(self) -> dict[str, Any]:
        report = super().gh_audit_report()
        self.pending.append(
            ("issue.labels_changed", {"issue_number": 921, "added": ["io:needs-human"], "removed": []})
        )
        return report


def test_an_audited_label_whose_event_is_still_unpublished_is_waited_for() -> None:
    """Round 3 F1: the window closes only at a tick boundary after the audit
    read, so an audited write's late event is in the history graded."""
    # A tick spans several reads: the label lands only when the tick that
    # was in progress at the audit read completes.
    engine = LateLabelCandidate([], audit={"POST /repos/o/r/issues/921/labels": 1}, reads_per_tick=4)
    window = _capture(engine)

    facts = upgrade_facts(window, whole_run=[], base_commit="b" * 40, candidate_commit="c" * 40, sessions_at_stop=(911, 921))
    card = grade(CASE_U, upgrade_observation(facts))
    assert "upgrade: hold label added in the restart window: #921 +io:needs-human" in card.failures


class HoleyHistory(FakeCandidate):
    """From the audit read on, returns its history with id 2 missing for
    ``holey_reads`` reads (a publisher still in flight)."""

    def __init__(self, *args: Any, holey_reads: int, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.holey_reads = holey_reads
        self.audited = False

    def gh_audit_report(self) -> dict[str, Any]:
        self.audited = True
        return super().gh_audit_report()

    def event_history(self) -> list[dict[str, Any]]:
        history = super().event_history()
        if self.audited and self.holey_reads:
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
    engine = HoleyHistory(STARTUP, audit={}, holey_reads=3)
    window = _capture(engine)

    assert [event["event_id"] for event in window.events] == list(range(1, len(window.events) + 1))
    facts = upgrade_facts(window, whole_run=[], base_commit="b" * 40, candidate_commit="c" * 40, sessions_at_stop=(911, 921))
    assert facts.hazards == ("session.run_unrestorable on #921 (X)",)


def test_a_persistent_hole_is_refused() -> None:
    engine = HoleyHistory(STARTUP, audit={}, holey_reads=10_000)
    with pytest.raises(ValueError, match="incomplete"):
        _capture(engine)
