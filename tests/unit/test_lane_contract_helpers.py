"""The lane contract's own machinery, tested rather than trusted (#7264).

The streaming assertion is only as honest as the helpers underneath it: a wait
that reports an event it did not observe, a diagnosis that names the wrong
cause, or a cleanup that replaces the failure it was cleaning up after all turn
a precise answer back into a guess. #7264 was exactly one of those.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from tests import event_wait
from tests.event_wait import await_event
from tests.unit.lane_executor_contract import release_lane, streaming_failure


class _FakeClock:
    """A monotonic clock that only moves when something sleeps on it.

    Injected rather than measured: a unit test of a deadline loop that reads the
    wall clock is a race with odds, which is the shape this whole change exists
    to remove.
    """

    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _FakeClock:
    fake = _FakeClock()
    monkeypatch.setattr(event_wait.time, "monotonic", fake.monotonic)
    monkeypatch.setattr(event_wait.time, "sleep", fake.sleep)
    return fake


class TestAwaitEvent:
    def test_an_event_that_happens_is_reported(self, clock: _FakeClock) -> None:
        answers = iter([False, False, True])

        assert await_event(lambda: next(answers), backstop_seconds=5.0) is True

    def test_an_event_that_never_happens_is_reported_as_not_happening(
        self, clock: _FakeClock
    ) -> None:
        assert await_event(lambda: False, backstop_seconds=5.0) is False
        assert clock.now == pytest.approx(1005.0, abs=0.06), (
            "it did not use its whole window before answering"
        )

    def test_it_returns_without_sleeping_when_the_event_already_happened(
        self, clock: _FakeClock
    ) -> None:
        assert await_event(lambda: True, backstop_seconds=30.0) is True
        assert clock.slept == [], "it waited on an event that had already happened"

    def test_the_clock_is_read_before_the_predicate(self, clock: _FakeClock) -> None:
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

    def test_the_caller_chooses_the_poll_gap(self, clock: _FakeClock) -> None:
        """Granularity is the caller's: a scheduler probe costs a tool call."""
        await_event(lambda: False, backstop_seconds=2.0, poll_seconds=0.5)

        assert clock.slept == [0.5, 0.5, 0.5, 0.5]

    def test_a_raising_predicate_is_not_swallowed(self, clock: _FakeClock) -> None:
        with pytest.raises(ZeroDivisionError):
            await_event(lambda: 1 // 0 == 0, backstop_seconds=5.0)


class TestStreamingFailure:
    """Which cause the three observations actually support (#7264)."""

    def _verdict(self, **overrides: bool) -> str | None:
        observations: dict[str, bool] = {
            "announced_flush": True,
            "marker_seen": True,
            "still_running": True,
        }
        observations.update(overrides)
        return streaming_failure(
            first_flush_backstop_seconds=45.0, **observations
        )

    def test_a_healthy_stream_is_no_failure(self) -> None:
        assert self._verdict() is None

    def test_no_announced_flush_is_not_a_buffering_diagnosis(self) -> None:
        verdict = self._verdict(announced_flush=False, marker_seen=False)

        assert verdict is not None
        assert "never announced its first flush" in verdict
        assert "NOT a buffering diagnosis" in verdict

    def test_the_missing_flush_outranks_everything_else(self) -> None:
        """It is the precondition for reading anything into the rest."""
        verdict = self._verdict(
            announced_flush=False, marker_seen=False, still_running=False
        )

        assert verdict is not None and "never announced its first flush" in verdict

    def test_concluding_before_the_marker_was_seen_says_so(self) -> None:
        verdict = self._verdict(marker_seen=False, still_running=False)

        assert verdict is not None
        assert "before its output was ever observed" in verdict
        assert "buffers" not in verdict, "it blamed the backend for an early exit"

    def test_concluding_after_the_marker_was_seen_is_a_different_failure(self) -> None:
        """Both are early conclusions; only one leaves the streaming duty undone.

        Reporting either as the other is how #7264's original message came to
        name a cause its observation could not establish.
        """
        verdict = self._verdict(still_running=False)

        assert verdict is not None
        assert "its marker WAS observed" in verdict

    def test_flushed_but_never_observable_is_the_only_buffering_diagnosis(
        self,
    ) -> None:
        verdict = self._verdict(marker_seen=False)

        assert verdict is not None
        assert "buffers until completion" in verdict
        assert "relay it pumps on its own loop" in verdict, (
            "it claimed to have proven buffering, which this observation cannot"
        )


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

        release_lane(handshake, thread)

        assert handshake.exists()
        assert thread.joins == 1

    def test_a_release_failure_is_reported_when_nothing_else_failed(
        self, tmp_path: Path
    ) -> None:
        unwritable = tmp_path / "no-such-directory" / "proceed"
        thread = _FinishedThread()
        thread.start()

        with pytest.raises(OSError):
            release_lane(unwritable, thread)

        assert thread.joins == 1, "the lane was left unreleased and unjoined"

    def test_a_release_failure_never_replaces_the_failure_it_cleans_up_after(
        self, tmp_path: Path
    ) -> None:
        """The causal failure is the one the operator must read -- and the leak
        must still be visible, so it is kept as that exception's context."""
        unwritable = tmp_path / "no-such-directory" / "proceed"
        thread = _FinishedThread()
        thread.start()

        with pytest.raises(AssertionError, match="the original failure") as caught:
            try:
                raise AssertionError("the original failure")
            finally:
                release_lane(unwritable, thread)

        assert isinstance(caught.value.__context__, OSError), (
            "the release failure was discarded instead of kept as context"
        )
        assert thread.joins == 1
