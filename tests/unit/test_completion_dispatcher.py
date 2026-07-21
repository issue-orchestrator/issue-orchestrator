"""Tests for the completion dispatcher (sync + background)."""

import threading
from types import SimpleNamespace

import pytest

from collections.abc import Callable

from issue_orchestrator.control.completion_dispatcher import (
    BackgroundCompletionDispatcher,
    SynchronousCompletionDispatcher,
)
from issue_orchestrator.execution.thread_background_job_runner import ThreadBackgroundJobRunner
from issue_orchestrator.ports.background_job import CompletedJob


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

    def test_in_flight_tracks_ownership_not_execution_status(self):
        """Regression: ``in_flight`` must stay True from dispatch until drain,
        even after the worker stops executing.

        A worker can finish after a tick takes its drain snapshot but before that
        tick's ``in_flight`` check. If ``in_flight`` followed the runner's
        ``is_running`` (``Thread.is_alive()``) it would report False in that
        window while the completed decision is still queued for the next drain,
        and the tick would re-dispatch the session into a duplicate decision.
        """

        class _FinishableRunner:
            """Runs a job on ``finish`` and stops reporting it running, WITHOUT
            draining — models a worker completing after a drain snapshot."""

            def __init__(self) -> None:
                self._pending: dict[str, Callable[[], None]] = {}
                self._alive: set[str] = set()
                self._done: list[CompletedJob] = []
                self.submit_count = 0

            def submit(self, job_id: str, fn: Callable[[], None]) -> bool:
                if job_id in self._alive:
                    return False
                self.submit_count += 1
                self._alive.add(job_id)
                self._pending[job_id] = fn
                return True

            def is_running(self, job_id: str) -> bool:
                return job_id in self._alive

            def finish(self, job_id: str) -> None:
                self._pending.pop(job_id)()
                self._alive.discard(job_id)
                self._done.append(CompletedJob(job_id=job_id, error=None))

            def drain_completed(self) -> list[CompletedJob]:
                done = self._done
                self._done = []
                return done

        runner = _FinishableRunner()
        d = BackgroundCompletionDispatcher(runner)
        session = _session("issue-1")

        d.dispatch(session, lambda: "DECISION")
        assert d.in_flight("issue-1") is True

        runner.finish("issue-1")  # worker done, result queued but NOT drained
        assert runner.is_running("issue-1") is False  # execution status: stopped
        assert d.in_flight("issue-1") is True  # owner: still in flight until drained

        out = d.drain()
        assert len(out) == 1
        assert out[0].session is session
        assert out[0].decision == "DECISION"
        assert d.in_flight("issue-1") is False  # drained -> ownership released
        assert runner.submit_count == 1  # never re-submitted
