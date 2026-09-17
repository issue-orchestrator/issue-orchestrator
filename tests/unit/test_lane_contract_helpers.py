"""The lane contract's own machinery, tested rather than trusted (#7264).

The streaming assertion is only as honest as the helpers underneath it: a wait
that reports an event it did not observe, or a cleanup that replaces the failure
it was cleaning up after, turns a precise diagnosis back into a guess.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from tests.event_wait import await_event
from tests.unit.lane_executor_contract import _release_lane


class TestAwaitEvent:
    def test_an_event_that_happens_is_reported(self) -> None:
        answers = iter([False, False, True])

        assert await_event(lambda: next(answers), backstop_seconds=5.0) is True

    def test_an_event_that_never_happens_is_reported_as_not_happening(self) -> None:
        assert await_event(lambda: False, backstop_seconds=0.2) is False

    def test_it_returns_as_soon_as_the_event_happens(self) -> None:
        started = time.monotonic()

        assert await_event(lambda: True, backstop_seconds=30.0) is True
        assert time.monotonic() - started < 5.0, "it waited out its backstop"

    def test_the_clock_is_read_before_the_predicate(self) -> None:
        """Strictly inside the window, or it did not happen.

        A probe-first loop would report an event that only became true during an
        overscheduled final sleep, which for a process-reaping wait is a quietly
        relaxed assertion.
        """
        probes = 0

        def probe() -> bool:
            nonlocal probes
            probes += 1
            return False

        assert await_event(probe, backstop_seconds=0.0) is False
        assert probes == 0, "the predicate ran after the window had closed"

    def test_a_raising_predicate_is_not_swallowed(self) -> None:
        with pytest.raises(ZeroDivisionError):
            await_event(lambda: 1 // 0 == 0, backstop_seconds=5.0)


class _FinishedThread(threading.Thread):
    def __init__(self) -> None:
        super().__init__(target=lambda: None)
        self.joins = 0

    def join(self, timeout: float | None = None) -> None:  # type: ignore[override]
        self.joins += 1
        super().join(timeout)


class TestReleaseLane:
    """The lane is held alive by the ABSENCE of the handshake file."""

    def test_it_releases_and_joins(self, tmp_path: Path) -> None:
        handshake = tmp_path / "proceed"
        thread = _FinishedThread()
        thread.start()

        _release_lane(handshake, thread)

        assert handshake.exists()
        assert thread.joins == 1

    def test_a_release_failure_is_reported_when_nothing_else_failed(
        self, tmp_path: Path
    ) -> None:
        unwritable = tmp_path / "no-such-directory" / "proceed"
        thread = _FinishedThread()
        thread.start()

        with pytest.raises(OSError):
            _release_lane(unwritable, thread)

        assert thread.joins == 1, "the lane was left unreleased and unjoined"

    def test_a_release_failure_never_masks_the_failure_it_cleans_up_after(
        self, tmp_path: Path
    ) -> None:
        """The causal failure is the one the operator must read.

        A handshake write that fails while an assertion is already propagating
        would otherwise replace it, leaving the real failure only in
        ``__context__`` -- and a queued lane's leak is the lesser problem.
        """
        unwritable = tmp_path / "no-such-directory" / "proceed"
        thread = _FinishedThread()
        thread.start()

        with pytest.raises(AssertionError, match="the original failure"):
            try:
                raise AssertionError("the original failure")
            finally:
                _release_lane(unwritable, thread)

        assert thread.joins == 1
