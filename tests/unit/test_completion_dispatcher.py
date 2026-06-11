"""Tests for the completion dispatcher (sync + background)."""

import threading
from types import SimpleNamespace

import pytest

from issue_orchestrator.control.completion_dispatcher import (
    BackgroundCompletionDispatcher,
    SynchronousCompletionDispatcher,
)
from issue_orchestrator.execution.thread_background_job_runner import ThreadBackgroundJobRunner


def _session(terminal_id: str) -> SimpleNamespace:
    # The dispatcher only needs terminal_id; decisions are opaque pass-throughs.
    return SimpleNamespace(terminal_id=terminal_id)


class TestSynchronousCompletionDispatcher:
    def test_runs_inline_and_drains_once(self):
        d = SynchronousCompletionDispatcher()
        session = _session("issue-1")
        d.dispatch(session, lambda: "DECISION")

        out = d.drain()
        assert len(out) == 1
        assert out[0].session is session
        assert out[0].decision == "DECISION"
        assert out[0].error is None
        assert d.drain() == []  # results are forgotten after draining
        assert d.in_flight("issue-1") is False

    def test_decide_error_propagates(self):
        # The inline path raised on decide failure; the sync dispatcher preserves
        # that (the background dispatcher captures errors via CompletedDecision).
        d = SynchronousCompletionDispatcher()

        def decide():
            raise RuntimeError("decide failed")

        with pytest.raises(RuntimeError, match="decide failed"):
            d.dispatch(_session("issue-1"), decide)
        assert d.drain() == []


class TestBackgroundCompletionDispatcher:
    def test_runs_off_thread_and_drains_when_done(self):
        runner = ThreadBackgroundJobRunner()
        d = BackgroundCompletionDispatcher(runner)
        gate = threading.Event()
        session = _session("issue-1")

        def decide():
            gate.wait(5)
            return "DECISION"

        d.dispatch(session, decide)
        # While the decision runs, the tick thread is free and nothing drains.
        assert d.in_flight("issue-1") is True
        assert d.drain() == []

        gate.set()
        assert runner.wait_until_idle(5) is True

        out = d.drain()
        assert len(out) == 1
        assert out[0].session is session
        assert out[0].decision == "DECISION"
        assert out[0].error is None
        assert d.in_flight("issue-1") is False

    def test_dedups_in_flight_terminal(self):
        runner = ThreadBackgroundJobRunner()
        d = BackgroundCompletionDispatcher(runner)
        gate = threading.Event()
        calls: list[int] = []

        def decide():
            calls.append(1)
            gate.wait(5)
            return "D"

        session = _session("issue-1")
        d.dispatch(session, decide)
        d.dispatch(session, decide)  # same terminal already running -> rejected

        gate.set()
        assert runner.wait_until_idle(5) is True
        assert len(calls) == 1
        assert len(d.drain()) == 1

    def test_captures_decide_error_as_completed_decision(self):
        runner = ThreadBackgroundJobRunner()
        d = BackgroundCompletionDispatcher(runner)
        boom = RuntimeError("decide failed")

        def decide():
            raise boom

        d.dispatch(_session("issue-1"), decide)
        assert runner.wait_until_idle(5) is True

        out = d.drain()
        assert len(out) == 1
        assert out[0].decision is None
        assert out[0].error is boom
