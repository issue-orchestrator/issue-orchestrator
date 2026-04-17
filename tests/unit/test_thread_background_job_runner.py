"""Tests for :class:`ThreadBackgroundJobRunner`.

These tests deliberately use the real thread adapter rather than a fake: the
whole point of the runner is correct cross-thread bookkeeping, and that has
to be exercised. Each test still completes deterministically because the
jobs signal their state via :class:`threading.Event`.
"""

from __future__ import annotations

import threading

import pytest

from issue_orchestrator.execution.thread_background_job_runner import (
    ThreadBackgroundJobRunner,
)


def _barrier_job(release: threading.Event, finished: threading.Event):
    def run() -> None:
        release.wait(timeout=5.0)
        finished.set()
    return run


def test_submit_starts_job_and_reports_running() -> None:
    runner = ThreadBackgroundJobRunner()
    release = threading.Event()
    finished = threading.Event()

    accepted = runner.submit("j1", _barrier_job(release, finished))

    assert accepted
    assert runner.is_running("j1")
    assert not finished.is_set()

    release.set()
    finished.wait(timeout=5.0)

    # Allow the background thread to exit; drain_completed drives the wait.
    completed = _drain_until(runner, expected_ids={"j1"})
    assert completed[0].job_id == "j1"
    assert completed[0].error is None


def test_submit_rejects_duplicate_job_id_while_running() -> None:
    runner = ThreadBackgroundJobRunner()
    release = threading.Event()
    finished = threading.Event()

    assert runner.submit("dup", _barrier_job(release, finished)) is True
    assert runner.submit("dup", lambda: None) is False  # still running, rejected

    release.set()
    finished.wait(timeout=5.0)
    _drain_until(runner, expected_ids={"dup"})


def test_submit_accepts_same_job_id_after_completion() -> None:
    runner = ThreadBackgroundJobRunner()
    done = threading.Event()

    assert runner.submit("cycle", done.set) is True
    done.wait(timeout=5.0)
    _drain_until(runner, expected_ids={"cycle"})

    done.clear()
    assert runner.submit("cycle", done.set) is True
    done.wait(timeout=5.0)
    _drain_until(runner, expected_ids={"cycle"})


def test_drain_completed_surfaces_job_exception() -> None:
    runner = ThreadBackgroundJobRunner()

    def boom() -> None:
        raise RuntimeError("kaboom")

    assert runner.submit("fail", boom) is True
    completed = _drain_until(runner, expected_ids={"fail"})

    assert completed[0].job_id == "fail"
    assert isinstance(completed[0].error, RuntimeError)
    assert str(completed[0].error) == "kaboom"
    # Failed job is no longer reported as running.
    assert not runner.is_running("fail")


def test_drain_completed_is_destructive() -> None:
    runner = ThreadBackgroundJobRunner()
    done = threading.Event()

    runner.submit("once", done.set)
    done.wait(timeout=5.0)

    first = _drain_until(runner, expected_ids={"once"})
    second = runner.drain_completed()

    assert len(first) == 1
    assert second == []


def _drain_until(runner: ThreadBackgroundJobRunner, *, expected_ids: set[str]) -> list:
    """Deterministically wait until expected job_ids appear in drain_completed.

    Uses the runner's own ``wait_until_idle`` readiness hook rather than
    polling sleeps, matching the "explicit readiness signal" rule in the
    testing guide.
    """
    assert runner.wait_until_idle(timeout=5.0), "runner did not reach idle"
    drained = runner.drain_completed()
    missing = expected_ids - {job.job_id for job in drained}
    if missing:
        raise AssertionError(f"expected job_ids never completed: {missing}")
    return drained
