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

    writer = TerminalRecordingWriter(recording_path, initial_rows=40, initial_cols=120)
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
