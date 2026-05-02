"""PTY output quiescence detector — driven by an injected clock + sleep.

Per ``tests/unit/AGENTS.md`` (no timing-based unit coordination), these
tests must not depend on real wall-clock progression. The helper accepts
``now`` and ``sleep`` callables so we can drive every scenario
deterministically by mutating both the clock value and the file size
between simulated polls.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

import pytest

from issue_orchestrator.execution.output_quiescence import (
    MissingRecordingError,
    wait_for_pty_quiescence,
)


class _FakeClock:
    """Deterministic monotonic clock that advances on each ``sleep`` call."""

    def __init__(self) -> None:
        self.value = 0.0

    def now(self) -> float:
        return self.value

    def make_sleeper(
        self,
        on_sleep: Callable[[float], None] | None = None,
    ) -> Callable[[float], None]:
        def _sleep(seconds: float) -> None:
            self.value += seconds
            if on_sleep is not None:
                on_sleep(seconds)
        return _sleep


def _make_recording(tmp_path: Path, contents: str = "seed\n") -> Path:
    rec = tmp_path / "rec.jsonl"
    rec.write_text(contents)
    return rec


def test_returns_immediately_when_quiet_seconds_is_zero(tmp_path: Path) -> None:
    rec = _make_recording(tmp_path)
    clock = _FakeClock()

    assert wait_for_pty_quiescence(
        rec,
        quiet_seconds=0,
        now=clock.now,
        sleep=clock.make_sleeper(),
    ) is True
    assert clock.value == 0.0  # no waiting required


def test_quiescent_file_returns_true_after_one_quiet_window(tmp_path: Path) -> None:
    """If the file size never changes, the helper should return True after
    exactly ``quiet_seconds`` of advancement on the injected clock."""
    rec = _make_recording(tmp_path)
    clock = _FakeClock()

    assert wait_for_pty_quiescence(
        rec,
        quiet_seconds=1.0,
        max_wait_seconds=10.0,
        poll_interval_seconds=0.25,
        now=clock.now,
        sleep=clock.make_sleeper(),
    ) is True
    # Quiet window is 1s; with 0.25s polls we need 4 sleeps to cross it.
    assert clock.value >= 1.0


def test_growing_file_eventually_times_out(tmp_path: Path) -> None:
    """If the file keeps growing on every poll, the helper should hit the
    max_wait deadline and return False."""
    rec = _make_recording(tmp_path)
    clock = _FakeClock()

    def grow_on_each_sleep(_seconds: float) -> None:
        with rec.open("a") as fh:
            fh.write("more\n")

    assert wait_for_pty_quiescence(
        rec,
        quiet_seconds=0.5,
        max_wait_seconds=2.0,
        poll_interval_seconds=0.25,
        now=clock.now,
        sleep=clock.make_sleeper(on_sleep=grow_on_each_sleep),
    ) is False
    # Reached the deadline.
    assert clock.value >= 2.0


def test_growth_then_silence_returns_true(tmp_path: Path) -> None:
    """File grows for a while, then goes quiet. Helper should return True
    once silence persists for the quiet window — not before."""
    rec = _make_recording(tmp_path)
    clock = _FakeClock()
    sleep_count = {"n": 0}

    def grow_for_first_two_sleeps(_seconds: float) -> None:
        sleep_count["n"] += 1
        if sleep_count["n"] <= 2:
            with rec.open("a") as fh:
                fh.write("more\n")

    assert wait_for_pty_quiescence(
        rec,
        quiet_seconds=0.5,
        max_wait_seconds=10.0,
        poll_interval_seconds=0.25,
        now=clock.now,
        sleep=clock.make_sleeper(on_sleep=grow_for_first_two_sleeps),
    ) is True


def test_missing_recording_raises_by_default(tmp_path: Path) -> None:
    """Session-replay contract: a missing recording is a caller bug, not
    "quiescent because no output." The helper must surface that loudly."""
    absent = tmp_path / "absent.jsonl"
    clock = _FakeClock()

    with pytest.raises(MissingRecordingError):
        wait_for_pty_quiescence(
            absent,
            quiet_seconds=1.0,
            now=clock.now,
            sleep=clock.make_sleeper(),
        )


def test_missing_recording_can_be_opted_out_for_bootstrap_paths(tmp_path: Path) -> None:
    """A test/bootstrap path that genuinely has no recording yet may opt
    out of the existence check by passing ``require_recording=False``."""
    absent = tmp_path / "absent.jsonl"
    clock = _FakeClock()

    assert wait_for_pty_quiescence(
        absent,
        quiet_seconds=0.5,
        max_wait_seconds=10.0,
        poll_interval_seconds=0.25,
        require_recording=False,
        now=clock.now,
        sleep=clock.make_sleeper(),
    ) is True
