"""PTY output quiescence detector."""

from __future__ import annotations

import threading
import time
from pathlib import Path

from issue_orchestrator.execution.output_quiescence import wait_for_pty_quiescence


def test_returns_immediately_when_quiet_seconds_is_zero(tmp_path: Path) -> None:
    recording = tmp_path / "rec.jsonl"
    recording.write_text("seed\n")

    assert wait_for_pty_quiescence(recording, quiet_seconds=0) is True


def test_returns_true_when_file_size_stays_stable(tmp_path: Path) -> None:
    recording = tmp_path / "rec.jsonl"
    recording.write_text("seed\n")

    start = time.monotonic()
    result = wait_for_pty_quiescence(
        recording,
        quiet_seconds=0.2,
        max_wait_seconds=2.0,
        poll_interval_seconds=0.05,
    )
    elapsed = time.monotonic() - start

    assert result is True
    # We should have waited at least quiet_seconds for the window to close.
    assert elapsed >= 0.2


def test_returns_false_when_file_keeps_growing_past_max_wait(tmp_path: Path) -> None:
    recording = tmp_path / "rec.jsonl"
    recording.write_text("seed\n")

    stop_writer = threading.Event()

    def keep_writing() -> None:
        while not stop_writer.is_set():
            with recording.open("a") as fh:
                fh.write("more\n")
            time.sleep(0.05)

    writer = threading.Thread(target=keep_writing, daemon=True)
    writer.start()
    try:
        result = wait_for_pty_quiescence(
            recording,
            quiet_seconds=0.3,
            max_wait_seconds=0.6,
            poll_interval_seconds=0.05,
        )
    finally:
        stop_writer.set()
        writer.join(timeout=1.0)

    assert result is False


def test_treats_missing_recording_as_zero_bytes_until_it_appears(tmp_path: Path) -> None:
    recording = tmp_path / "not-yet.jsonl"  # absent at start
    # No file present; size is "zero" forever => stable => quiescent.
    assert wait_for_pty_quiescence(
        recording,
        quiet_seconds=0.1,
        max_wait_seconds=1.0,
        poll_interval_seconds=0.05,
    ) is True
