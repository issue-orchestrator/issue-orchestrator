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
from typing import Any, Iterator, Sequence


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

    def __init__(self, path: Path, *, initial_rows: int | None = None, initial_cols: int | None = None) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._base_offset_ms = _next_recording_offset(path)
        self._file = open(path, "a", encoding="utf-8")  # noqa: SIM115
        self._started = time.monotonic()
        if initial_rows is not None and initial_cols is not None:
            self.write_resize(rows=initial_rows, cols=initial_cols)

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
        elapsed = int((time.monotonic() - self._started) * 1000)
        return self._base_offset_ms + elapsed

    def _write_event(self, event: TerminalRecordingEvent) -> None:
        self._file.write(json.dumps(event.to_dict(), sort_keys=True) + "\n")
        self._file.flush()


class MirroredTerminalRecordingWriter:
    """Write canonical terminal replay events and optionally mirror plain text."""

    def __init__(
        self,
        recording_path: Path,
        *,
        additional_recording_paths: Sequence[Path] | None = None,
        mirror_path: Path | None = None,
        initial_rows: int | None = None,
        initial_cols: int | None = None,
    ) -> None:
        recording_paths = [recording_path]
        for extra_path in additional_recording_paths or ():
            if extra_path not in recording_paths:
                recording_paths.append(extra_path)
        self._recordings = [
            TerminalRecordingWriter(
                path,
                initial_rows=initial_rows,
                initial_cols=initial_cols,
            )
            for path in recording_paths
        ]
        self._mirror = None
        if mirror_path is not None:
            mirror_path.parent.mkdir(parents=True, exist_ok=True)
            self._mirror = open(mirror_path, "a", encoding="utf-8")  # noqa: SIM115

    @property
    def name(self) -> str:
        return self._recordings[0].name

    def write(self, data: bytes | str) -> int:
        if isinstance(data, str):
            text = data
            raw = data.encode("utf-8")
        else:
            raw = data
            text = data.decode("utf-8", errors="ignore")
        written = 0
        for recording in self._recordings:
            written = recording.write(raw)
        if self._mirror is not None and text:
            self._mirror.write(text)
            self._mirror.flush()
        return written

    def flush(self) -> None:
        for recording in self._recordings:
            recording.flush()
        if self._mirror is not None:
            self._mirror.flush()

    def close(self) -> None:
        for recording in self._recordings:
            recording.close()
        if self._mirror is not None:
            self._mirror.close()

def iter_terminal_recording(path: Path) -> Iterator[dict[str, Any]]:
    """Iterate over a terminal recording NDJSON file for replay or inspection."""
    with path.open(encoding="utf-8") as handle:
        for raw_line in handle:
            if not raw_line.strip():
                continue
            yield json.loads(raw_line)


def first_terminal_geometry(path: Path) -> tuple[int, int] | None:
    """Return the first recorded terminal geometry as (rows, cols)."""
    for event in iter_terminal_recording(path):
        if event.get("event_type") != "resize":
            continue
        rows = event.get("rows")
        cols = event.get("cols")
        if isinstance(rows, int) and isinstance(cols, int):
            return rows, cols
    return None


def append_output_event(path: Path, text: str) -> None:
    """Append a plain-text transcript snippet as one terminal output event."""
    if not text:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    next_offset = _next_recording_offset(path)
    payload = TerminalRecordingEvent(
        event_type="output",
        offset_ms=next_offset,
        data_b64=base64.b64encode(text.encode("utf-8")).decode("ascii"),
    )
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload.to_dict(), sort_keys=True) + "\n")


def _next_recording_offset(path: Path) -> int:
    if not path.exists():
        return 0
    last_offset = _read_last_recording_offset(path)
    if last_offset is None:
        return 0
    return last_offset + 1


def _read_last_recording_offset(path: Path) -> int | None:
    with path.open("rb") as handle:
        handle.seek(0, 2)
        file_size = handle.tell()
        if file_size <= 0:
            return None

        buffer = b""
        position = file_size
        while position > 0:
            read_size = min(4096, position)
            position -= read_size
            handle.seek(position)
            buffer = handle.read(read_size) + buffer
            lines = buffer.splitlines()
            if position > 0 and len(lines) <= 1:
                continue
            for raw_line in reversed(lines):
                if not raw_line.strip():
                    continue
                try:
                    payload = json.loads(raw_line.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                offset_ms = payload.get("offset_ms")
                if isinstance(offset_ms, int):
                    return offset_ms
                return None
        return None
