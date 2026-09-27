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
    """An engine whose history grows by one tick per history read, up to ``ticks``."""

    def __init__(self, startup: list[dict[str, Any]], ticks: int, audit: dict[str, int]) -> None:
        self.history: list[dict[str, Any]] = list(startup)
        self.ticks_left = ticks
        self.audit = audit
        self.reads: list[str] = []
        self.alive = True

    def publish(self, event_type: str, payload: dict[str, Any]) -> None:
        self.history.append({"event_id": len(self.history) + 1, "type": event_type, "payload": payload})

    def is_running(self) -> bool:
        return self.alive

    def event_history(self) -> list[dict[str, Any]]:
        self.reads.append("history")
        snapshot = list(self.history)
        if self.ticks_left:
            self.ticks_left -= 1
            self.publish("tick.completed", {})
        return snapshot

    def gh_audit_report(self) -> dict[str, Any]:
        self.reads.append("audit")
        return {"by_command": dict(self.audit)}


async def _no_sleep(_: float) -> None:
    return None


def _capture(engine: FakeCandidate):
    return asyncio.run(
        capture_restart_window(engine, min_ticks=UPGRADE_EARLY_TICKS, timeout_s=60, sleep=_no_sleep)
    )


def test_startup_hazards_and_pages_before_any_watcher_fail_the_case() -> None:
    """Finding 1: what the candidate does at startup, before a watcher can
    connect, reaches the scorecard through the real capture path, even if
    the label is cleared and the work finishes later."""
    startup = [
        {"event_id": 1, "type": "session.run_unrestorable", "payload": {"issue_number": 921, "cause": "RUN_UNRESTORABLE"}},
        {"event_id": 2, "type": "issue.labels_changed", "payload": {"issue_number": 921, "added": ["io:needs-human"], "removed": []}},
    ]
    engine = FakeCandidate(startup, ticks=6, audit={"POST /repos/o/r/issues/921/comments": 1})
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
    engine = FakeCandidate([], ticks=UPGRADE_EARLY_TICKS + 3, audit={"POST /repos/o/r/issues/5/comments": 2})
    window = _capture(engine)

    assert engine.reads[-2:] == ["audit", "history"]
    assert engine.reads.count("audit") == 1
    assert window.ticks >= UPGRADE_EARLY_TICKS
    assert window.writes[WriteKind.COMMENT] == 2


def test_a_candidate_that_dies_in_the_window_fails_on_ticks() -> None:
    engine = FakeCandidate([], ticks=10, audit={})
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
    """Adds a hold label while the harness is reading its audit report."""

    def gh_audit_report(self) -> dict[str, Any]:
        report = super().gh_audit_report()
        self.publish("issue.labels_changed", {"issue_number": 921, "added": ["io:needs-human"], "removed": []})
        return report


def test_a_label_added_during_the_audit_read_is_still_seen() -> None:
    """Round 2 F1: the audit is read first and the history after it, so a
    write the audit may count always has its event in the history read."""
    engine = LateLabelCandidate([], ticks=UPGRADE_EARLY_TICKS, audit={"POST /repos/o/r/issues/921/labels": 1})
    window = _capture(engine)

    facts = upgrade_facts(window, whole_run=[], base_commit="b" * 40, candidate_commit="c" * 40, sessions_at_stop=(911, 921))
    card = grade(CASE_U, upgrade_observation(facts))
    assert "upgrade: hold label added in the restart window: #921 +io:needs-human" in card.failures


def test_a_complete_history_buffered_out_of_order_is_graded() -> None:
    """Round 2 F3: concurrent publishers can buffer ids out of order."""

    class OutOfOrder(FakeCandidate):
        def event_history(self) -> list[dict[str, Any]]:
            history = super().event_history()
            return [history[1], history[0], *history[2:]]

    startup = [
        {"event_id": 1, "type": "session.run_unrestorable", "payload": {"issue_number": 921, "cause": "X"}},
        {"event_id": 2, "type": "tick.completed", "payload": {}},
    ]
    engine = OutOfOrder(startup, ticks=UPGRADE_EARLY_TICKS, audit={})
    window = _capture(engine)

    assert [event["event_id"] for event in window.events] == list(range(1, len(window.events) + 1))
    facts = upgrade_facts(window, whole_run=[], base_commit="b" * 40, candidate_commit="c" * 40, sessions_at_stop=(911, 921))
    assert facts.hazards == ("session.run_unrestorable on #921 (X)",)
