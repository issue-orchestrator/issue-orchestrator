from __future__ import annotations

import base64
import json

from issue_orchestrator.infra.terminal_recording import (
    MirroredTerminalRecordingWriter,
    TerminalRecordingWriter,
    append_output_event,
    first_terminal_geometry,
    iter_terminal_recording,
)


def test_terminal_recording_writer_flushes_events_immediately(tmp_path) -> None:
    recording_path = tmp_path / "terminal-recording.jsonl"

    writer = TerminalRecordingWriter(recording_path)
    writer.write_output(b"live output\n")

    raw = recording_path.read_text(encoding="utf-8")
    writer.close()

    event = json.loads(raw.strip())
    payload = base64.b64decode(event["data_b64"]).decode("utf-8", errors="replace")
    assert payload == "live output\n"


def test_terminal_recording_writer_records_initial_geometry_first(tmp_path) -> None:
    recording_path = tmp_path / "terminal-recording.jsonl"
    clock = IncrementingClock(now=100.0, step_seconds=0.002)

    writer = TerminalRecordingWriter(recording_path, initial_rows=40, initial_cols=120, clock=clock)
    writer.write_output(b"hello\n")
    writer.close()

    events = list(iter_terminal_recording(recording_path))
    assert events[0]["event_type"] == "resize"
    assert events[0]["rows"] == 40
    assert events[0]["cols"] == 120
    assert events[0]["offset_ms"] == 0
    assert first_terminal_geometry(recording_path) == (40, 120)
    assert events[1]["event_type"] == "output"


def test_append_output_event_uses_next_offset_after_existing_events(tmp_path) -> None:
    recording_path = tmp_path / "terminal-recording.jsonl"
    recording_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "event_type": "output",
                "offset_ms": 41,
                "data_b64": base64.b64encode(b"first").decode("ascii"),
            }
        )
        + "\n",
        encoding="utf-8",
    )

    append_output_event(recording_path, "second")

    events = list(iter_terminal_recording(recording_path))
    assert [event["offset_ms"] for event in events] == [41, 42]


def test_append_output_event_ignores_trailing_invalid_lines_when_finding_offset(tmp_path) -> None:
    recording_path = tmp_path / "terminal-recording.jsonl"
    recording_path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "schema_version": 1,
                        "event_type": "output",
                        "offset_ms": 3,
                        "data_b64": base64.b64encode(b"ok").decode("ascii"),
                    }
                ),
                "{not-json",
                "",
            ]
        ),
        encoding="utf-8",
    )

    append_output_event(recording_path, "next")

    raw_lines = [line for line in recording_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert json.loads(raw_lines[0])["offset_ms"] == 3
    assert json.loads(raw_lines[-1])["offset_ms"] == 4


def test_first_terminal_geometry_returns_none_without_resize_events(tmp_path) -> None:
    recording_path = tmp_path / "terminal-recording.jsonl"
    recording_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "event_type": "output",
                "offset_ms": 0,
                "data_b64": base64.b64encode(b"plain").decode("ascii"),
            }
        )
        + "\n",
        encoding="utf-8",
    )

    assert first_terminal_geometry(recording_path) is None


def test_terminal_recording_writer_reopens_with_monotonic_offsets(tmp_path) -> None:
    recording_path = tmp_path / "terminal-recording.jsonl"

    first = TerminalRecordingWriter(recording_path, initial_rows=24, initial_cols=80)
    first.write_output(b"first\n")
    first.close()

    second = TerminalRecordingWriter(recording_path, initial_rows=24, initial_cols=80)
    second.write_output(b"second\n")
    second.close()

    offsets = [event["offset_ms"] for event in iter_terminal_recording(recording_path)]
    assert offsets == sorted(offsets)
    assert max(offsets[:2]) < min(offsets[2:])


def test_mirrored_terminal_recording_writer_keeps_plain_text_mirror(tmp_path) -> None:
    recording_path = tmp_path / "terminal-recording.jsonl"
    mirror_path = tmp_path / "agent-output.log"

    writer = MirroredTerminalRecordingWriter(
        recording_path,
        mirror_path=mirror_path,
        initial_rows=30,
        initial_cols=100,
    )
    writer.write("hello\n")
    writer.write(b"world\n")
    writer.close()

    events = list(iter_terminal_recording(recording_path))
    payload = "".join(
        base64.b64decode(event["data_b64"]).decode("utf-8", errors="replace")
        for event in events
        if event.get("event_type") == "output"
    )
    assert payload == "hello\nworld\n"
    assert mirror_path.read_text(encoding="utf-8") == "hello\nworld\n"


def test_mirrored_terminal_recording_writer_can_mirror_to_additional_recordings(tmp_path) -> None:
    recording_path = tmp_path / "terminal-recording.jsonl"
    secondary_path = tmp_path / "secondary-terminal-recording.jsonl"
    clock = IncrementingClock(now=100.0, step_seconds=0.001)

    writer = MirroredTerminalRecordingWriter(
        recording_path,
        additional_recording_paths=[secondary_path],
        initial_rows=24,
        initial_cols=80,
        clock=clock,
    )
    writer.write("hello\n")
    writer.close()

    primary_events = list(iter_terminal_recording(recording_path))
    secondary_events = list(iter_terminal_recording(secondary_path))
    assert primary_events == secondary_events


def test_mirrored_terminal_recording_writer_invokes_output_callback(tmp_path) -> None:
    recording_path = tmp_path / "terminal-recording.jsonl"
    seen: list[bytes] = []

    writer = MirroredTerminalRecordingWriter(
        recording_path,
        on_output=seen.append,
        initial_rows=24,
        initial_cols=80,
    )
    writer.write("hello\n")
    writer.close()

    assert seen == [b"hello\n"]


class ManualClock:
    def __init__(self, now: float) -> None:
        self._now = now

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


class IncrementingClock:
    def __init__(self, *, now: float, step_seconds: float) -> None:
        self._now = now
        self._step_seconds = step_seconds
        self._first = True

    def __call__(self) -> float:
        if self._first:
            self._first = False
            return self._now
        self._now += self._step_seconds
        return self._now


def test_mirrored_terminal_recording_writer_preserves_per_path_base_offsets(
    tmp_path,
) -> None:
    recording_path = tmp_path / "terminal-recording.jsonl"
    aggregate_path = tmp_path / "aggregate-terminal-recording.jsonl"
    append_output_event(aggregate_path, "existing")
    clock = ManualClock(100.0)

    writer = MirroredTerminalRecordingWriter(
        recording_path,
        additional_recording_paths=[aggregate_path],
        initial_rows=24,
        initial_cols=80,
        clock=clock,
    )
    clock.advance(0.001)
    writer.write("hello\n")
    writer.close()

    primary_events = list(iter_terminal_recording(recording_path))
    aggregate_events = list(iter_terminal_recording(aggregate_path))

    primary_offsets = [event["offset_ms"] for event in primary_events]
    aggregate_offsets = [event["offset_ms"] for event in aggregate_events[-2:]]
    assert primary_offsets == [0, 1]
    assert aggregate_offsets == [1, 2]
