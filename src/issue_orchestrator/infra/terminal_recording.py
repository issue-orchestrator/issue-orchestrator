"""Raw terminal recording utilities for PTY session replay.

The canonical session artifact is a newline-delimited JSON stream of terminal
events. Each event preserves the original PTY bytes losslessly via base64 so
replay tooling can feed them into a real terminal emulator later.
"""

from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator


TERMINAL_RECORDING_FILENAME = "terminal-recording.jsonl"
TERMINAL_RECORDING_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class TerminalRecordingEvent:
    """One replayable terminal event."""

    event_type: str
    offset_ms: int
    data_b64: str | None = None
    rows: int | None = None
    cols: int | None = None
    schema_version: int = TERMINAL_RECORDING_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema_version": self.schema_version,
            "event_type": self.event_type,
            "offset_ms": self.offset_ms,
        }
        if self.data_b64 is not None:
            payload["data_b64"] = self.data_b64
        if self.rows is not None:
            payload["rows"] = self.rows
        if self.cols is not None:
            payload["cols"] = self.cols
        return payload


class TerminalRecordingWriter:
    """Append-only NDJSON writer for raw terminal events."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._file = open(path, "a", encoding="utf-8")  # noqa: SIM115
        self._started = time.monotonic()

    @property
    def name(self) -> str:
        return str(self._path)

    def write(self, data: bytes | str) -> int:
        """pexpect-compatible logfile write interface."""
        if isinstance(data, str):
            data = data.encode("utf-8")
        self.write_output(data)
        return len(data)

    def write_output(self, data: bytes) -> None:
        if not data:
            return
        event = TerminalRecordingEvent(
            event_type="output",
            offset_ms=self._offset_ms(),
            data_b64=base64.b64encode(data).decode("ascii"),
        )
        self._write_event(event)

    def write_resize(self, *, rows: int, cols: int) -> None:
        event = TerminalRecordingEvent(
            event_type="resize",
            offset_ms=self._offset_ms(),
            rows=rows,
            cols=cols,
        )
        self._write_event(event)

    def flush(self) -> None:
        self._file.flush()

    def close(self) -> None:
        self._file.close()

    def _offset_ms(self) -> int:
        return int((time.monotonic() - self._started) * 1000)

    def _write_event(self, event: TerminalRecordingEvent) -> None:
        self._file.write(json.dumps(event.to_dict(), sort_keys=True) + "\n")
        self._file.flush()

def iter_terminal_recording(path: Path) -> Iterator[dict[str, Any]]:
    """Iterate over a terminal recording NDJSON file for replay or inspection."""
    with path.open(encoding="utf-8") as handle:
        for raw_line in handle:
            if not raw_line.strip():
                continue
            yield json.loads(raw_line)


def append_output_event(path: Path, text: str) -> None:
    """Append a plain-text transcript snippet as one terminal output event."""
    if not text:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    next_offset = 0
    if path.exists():
        for event in iter_terminal_recording(path):
            offset_ms = event.get("offset_ms")
            if isinstance(offset_ms, int) and offset_ms >= next_offset:
                next_offset = offset_ms + 1
    payload = TerminalRecordingEvent(
        event_type="output",
        offset_ms=next_offset,
        data_b64=base64.b64encode(text.encode("utf-8")).decode("ascii"),
    )
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload.to_dict(), sort_keys=True) + "\n")
