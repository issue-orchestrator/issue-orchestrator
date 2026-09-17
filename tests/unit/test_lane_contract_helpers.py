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
from tests.unit.lane_executor_contract import (
    release_lane,
    streaming_failure,
    streaming_verdict,
)


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
    """Which cause the three observations actually support (#7264).

    All eight combinations, because the three observations are independent and
    reading them in a fixed order is what produced two wrong messages in an
    earlier round.
    """

    def _verdict(
        self,
        announced_in_window: bool,
        marker_seen: bool,
        still_running: bool,
        *,
        announced_eventually: bool | None = None,
    ) -> str | None:
        return streaming_failure(
            announced_in_window=announced_in_window,
            announced_eventually=(
                announced_in_window
                if announced_eventually is None
                else announced_eventually
            ),
            marker_seen=marker_seen,
            still_running=still_running,
            first_flush_backstop_seconds=45.0,
        )

    def test_the_whole_truth_table_is_classified(self) -> None:
        """No combination falls through to a wrong neighbour's message."""
        expectations = {
            # announced, marker, running
            (True, True, True): None,
            (False, True, True): "fixture",
            (True, True, False): "ordering",
            (False, True, False): "ordering",
            (True, False, True): "buffering",
            (True, False, False): "early",
            (False, False, True): "never got that far",
            (False, False, False): "never got that far",
        }
        markers = {
            "fixture": "the sentinel never appeared at all",
            "ordering": "cannot establish that the output was observable BEFORE",
            "buffering": "Either the backend buffers until completion",
            "early": "concluded before its output was ever observed",
            "never got that far": "never announced its first flush",
        }

        for observations, expected in expectations.items():
            verdict = self._verdict(*observations)
            if expected is None:
                assert verdict is None, f"{observations} was reported as {verdict!r}"
                continue
            assert verdict is not None, f"{observations} was reported as healthy"
            assert markers[expected] in verdict, (
                f"{observations} should be the {expected!r} diagnosis, got {verdict!r}"
            )

    def test_an_observed_marker_on_a_live_lane_is_the_invariant_holding(self) -> None:
        assert self._verdict(True, True, True) is None

    def test_a_missing_announcement_never_overrides_an_observed_marker(self) -> None:
        """The marker was seen WHILE RUNNING: the invariant held.

        The fixture that separates "never ran" from "buffered" is broken, which
        must be reported -- but as a fixture failure. Reading the announcement
        first, as an earlier round did, blamed the backend for a lane that had
        demonstrably streamed.
        """
        verdict = self._verdict(False, True, True, announced_eventually=False)

        assert verdict is not None
        assert "the streaming invariant HELD" in verdict
        assert "the sentinel never appeared at all" in verdict
        assert "Fix the fixture" in verdict
        assert "Either the backend buffers" not in verdict, (
            "it accused the backend for a lane that had demonstrably streamed"
        )

    def test_a_sentinel_that_lands_late_is_late_and_not_absent(self) -> None:
        """The timed observation goes stale the moment its window closes.

        The sentinel is written just after the print, so one that lands a poll
        gap past the boundary would otherwise be reported as a fixture that
        never announced at all. Both fail -- a breached backstop is a real
        result -- but only one of them is a broken fixture.
        """
        verdict = self._verdict(False, True, True, announced_eventually=True)

        assert verdict is not None
        assert "took longer than 45s to announce" in verdict
        assert "the sentinel never appeared" not in verdict
        assert "Either the backend buffers" not in verdict

    def test_a_marker_seen_after_conclusion_proves_no_ordering(self) -> None:
        """"Observable before completion" is exactly what this cannot show.

        An earlier round called this discharged, which it is not: the marker may
        have become visible only as the lane ended.
        """
        verdict = self._verdict(True, True, False)

        assert verdict is not None
        assert "cannot establish that the output was observable BEFORE" in verdict
        assert "discharged" not in verdict

    def test_only_a_live_announced_lane_can_be_accused_of_buffering(self) -> None:
        verdict = self._verdict(True, False, True)

        assert verdict is not None
        assert "Either the backend buffers until completion" in verdict
        assert "relay it pumps on its own loop" in verdict, (
            "it claimed to have proven buffering, which this observation cannot"
        )

    def test_nothing_observed_and_nothing_announced_blames_nobody(self) -> None:
        verdict = self._verdict(False, False, True)

        assert verdict is not None
        assert "NOT a buffering diagnosis" in verdict
        assert "NOT proof that no output was written" in verdict


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
        """The causal failure is the one the operator must read -- the SAME
        exception object, with its original traceback, and the leak kept as its
        context so it is still visible.

        Identity and traceback, not just type and message: re-raising a freshly
        constructed exception with the same text would satisfy a message check
        while throwing away the frames that say where the failure happened.
        """
        unwritable = tmp_path / "no-such-directory" / "proceed"
        thread = _FinishedThread()
        thread.start()
        original = AssertionError("the original failure")

        def raise_it_from_a_frame_of_its_own() -> None:
            """Uniquely named so the traceback can be checked for IT.

            Asserting on the test function's own frame proves nothing: the
            cleanup call contributes that frame anyway, so clearing
            ``__traceback__`` before re-raising the same object would still
            pass.
            """
            raise original

        with pytest.raises(AssertionError) as caught:
            try:
                raise_it_from_a_frame_of_its_own()
            finally:
                release_lane(unwritable, thread)

        assert caught.value is original, (
            "a different exception object was raised, so the original traceback"
            " is gone"
        )
        frames = []
        traceback = caught.value.__traceback__
        while traceback is not None:
            frames.append(traceback.tb_frame.f_code.co_name)
            traceback = traceback.tb_next
        assert "raise_it_from_a_frame_of_its_own" in frames, (
            f"the frame the failure was RAISED from is gone: {frames}"
        )
        assert isinstance(caught.value.__context__, OSError), (
            "the release failure was discarded instead of kept as context"
        )
        assert caught.value.__suppress_context__ is False, (
            "the release failure is attached but hidden from the rendered"
            " traceback, so the leak is invisible to whoever reads the failure"
        )
        assert thread.joins == 1


class TestStreamingVerdict:
    """The handoff from the timed observation to the classification (#7264).

    `streaming_failure` can tell a late sentinel from an absent one; this is
    what proves the caller actually gives it the chance to. The re-read lives
    inside `streaming_verdict` precisely so this is testable -- when it was one
    line of caller code, nothing stopped it from being replaced by the stale
    timed observation.
    """

    def test_a_sentinel_that_appears_after_the_window_is_read_again(
        self, tmp_path: Path
    ) -> None:
        sentinel = tmp_path / "flushed"
        sentinel.write_text("")  # it exists NOW; it did not when the window closed

        verdict = streaming_verdict(
            sentinel=sentinel,
            announced_in_window=False,
            marker_seen=True,
            still_running=True,
            first_flush_backstop_seconds=45.0,
        )

        assert verdict is not None
        assert "took longer than 45s to announce" in verdict, (
            "the stale timed observation was reused, so a late sentinel is"
            f" still being reported as an absent one: {verdict!r}"
        )

    def test_a_sentinel_that_never_appears_is_still_absent(
        self, tmp_path: Path
    ) -> None:
        verdict = streaming_verdict(
            sentinel=tmp_path / "never-written",
            announced_in_window=False,
            marker_seen=True,
            still_running=True,
            first_flush_backstop_seconds=45.0,
        )

        assert verdict is not None
        assert "the sentinel never appeared at all" in verdict

    def test_an_announcement_inside_the_window_needs_no_second_opinion(
        self, tmp_path: Path
    ) -> None:
        sentinel = tmp_path / "flushed"
        sentinel.write_text("")

        assert (
            streaming_verdict(
                sentinel=sentinel,
                announced_in_window=True,
                marker_seen=True,
                still_running=True,
                first_flush_backstop_seconds=45.0,
            )
            is None
        )
