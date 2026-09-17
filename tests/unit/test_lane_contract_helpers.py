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
from tests.unit.lane_executor_contract import release_lane, streaming_verdict


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


class TestStreamingVerdictTable:
    """Which cause the observations actually support (#7264).

    Driven through `streaming_verdict`, the ONLY classifier -- the Boolean-taking
    form is private to it now, because while it was public the real call site
    could be changed back to pass the stale timed observation and every test here
    would still have exercised the correct helper.

    All TWELVE reachable combinations, because reading them in a fixed order
    produced wrong messages twice. The other four -- announced in the window but
    not eventually -- cannot happen: the sentinel is a file, so it does not
    un-appear, which is exactly why the two announcement observations are not
    independent of each other.
    """

    def _verdict(
        self,
        tmp_path: Path,
        announced_in_window: bool,
        announced_eventually: bool,
        marker_seen: bool,
        still_running: bool,
    ) -> str | None:
        tmp_path.mkdir(parents=True, exist_ok=True)
        sentinel = tmp_path / "flushed"
        if announced_eventually:
            sentinel.write_text("")
        return streaming_verdict(
            sentinel=sentinel,
            announced_in_window=announced_in_window,
            marker_seen=marker_seen,
            still_running=still_running,
            first_flush_backstop_seconds=45.0,
        )

    #: (in_window, eventually, marker, running) -> the diagnosis it must give.
    #: `in_window and not eventually` is impossible: the sentinel is durable.
    TABLE = {
        (True, True, True, True): None,
        (True, True, True, False): "ordering",
        (True, True, False, True): "buffering",
        (True, True, False, False): "early",
        (False, True, True, True): "late",
        (False, True, True, False): "ordering",
        (False, True, False, True): "late-unproved",
        (False, True, False, False): "late-unproved",
        (False, False, True, True): "fixture",
        (False, False, True, False): "ordering",
        (False, False, False, True): "never",
        (False, False, False, False): "never",
    }
    MARKERS = {
        "ordering": "cannot establish that the output was observable BEFORE",
        "buffering": "Either the backend buffers until completion",
        "early": "concluded before its output was ever observed",
        "late": "took longer than 45s to announce",
        "late-unproved": "only announced it after its 45s window had closed",
        "fixture": "the sentinel never appeared at all",
        "never": "never announced its first flush within 45s, and no marker",
    }

    def test_every_reachable_combination_is_classified(self, tmp_path: Path) -> None:
        for index, (observations, expected) in enumerate(self.TABLE.items()):
            verdict = self._verdict(tmp_path / str(index), *observations)
            if expected is None:
                assert verdict is None, f"{observations} was reported as {verdict!r}"
                continue
            assert verdict is not None, f"{observations} was reported as healthy"
            assert self.MARKERS[expected] in verdict, (
                f"{observations} should be the {expected!r} diagnosis,"
                f" got {verdict!r}"
            )

    def test_a_late_sentinel_is_never_reported_as_a_lane_that_never_ran(
        self, tmp_path: Path
    ) -> None:
        """The sentinel PROVES the lane executed and wrote output.

        Ignoring it when no marker was seen reported a lane that had
        demonstrably flushed as one that may never have been admitted.
        """
        verdict = self._verdict(tmp_path, False, True, False, True)

        assert verdict is not None
        assert "The lane DID execute and write output" in verdict
        assert "never have been admitted" not in verdict

    def test_a_missing_announcement_never_overrides_an_observed_marker(
        self, tmp_path: Path
    ) -> None:
        """The marker was seen WHILE RUNNING: the invariant held."""
        verdict = self._verdict(tmp_path, False, False, True, True)

        assert verdict is not None
        assert "the streaming invariant HELD" in verdict
        assert "Either the backend buffers" not in verdict

    def test_only_a_live_announced_lane_can_be_accused_of_buffering(
        self, tmp_path: Path
    ) -> None:
        verdict = self._verdict(tmp_path, True, True, False, True)

        assert verdict is not None
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
    """The lane declines to conclude while the handshake file is absent.

    Until its own safety clock expires -- which is what makes the fixture safe
    when the handshake never comes, and why releasing it is best-effort cleanup
    rather than the only thing standing between the lane and immortality.
    """

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
