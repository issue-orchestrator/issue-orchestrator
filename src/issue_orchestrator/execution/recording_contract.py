"""Terminal-recording JSONL contract validation.

The chapter sidecar's recording offset is consumed by the session viewer to
scrub into the raw replay stream, so a wrong-but-plausible event count is
worse than a loud failure. ``recording_event_count`` parses a recording and
enforces the ``TerminalRecordingEvent`` shape on every line, raising
``CorruptRecordingError`` on anything the viewer could not faithfully replay.

Extracted from ``persistent_round_runner`` — these helpers operate purely on
recording files and carry no PTY/session coupling.
"""

from __future__ import annotations

import base64
import binascii
import json
from pathlib import Path
from typing import Any


class CorruptRecordingError(RuntimeError):
    """Raised when a recording file contains lines that aren't valid events.

    The chapter sidecar's recording offset is consumed by the session
    viewer to scrub into the raw replay stream, so a wrong-but-plausible
    count is worse than a loud failure. If the recording has been
    corrupted (truncated mid-write, accidentally overwritten, mixed with
    non-recording content), surface that loudly rather than silently
    producing chapter offsets that point at noise.
    """


def recording_event_count(
    recording_path: Path,
    *,
    require_recording: bool = True,
) -> int:
    """Return the current number of events in a JSONL recording.

    Used by chapter-sidecar writers to capture the current position in
    the role's recording stream at boundary moments. Because this number
    is recorded into chapters.json and the session viewer scrubs to it,
    a wrong-but-plausible offset is worse than a loud failure.

    Each non-blank line is parsed as a JSON object with at minimum an
    ``event_type`` string field — anything else raises
    ``CorruptRecordingError``. The contract this enforces matches what
    ``TerminalRecordingWriter`` produces: every event carries
    ``event_type`` plus optional payload fields.

    By default, raises ``FileNotFoundError`` if the recording is absent —
    in the persistent-session path the recording is created when the PTY
    writer is constructed at session open, so a missing file means wrong
    path or failed capture, not "no events yet." Bootstrap and test paths
    that genuinely operate before any recording exists must opt out by
    passing ``require_recording=False``.
    """
    if not recording_path.exists():
        if require_recording:
            raise FileNotFoundError(
                f"Recording not found at {recording_path}; cannot compute event count"
            )
        return 0
    count = 0
    with recording_path.open("r", encoding="utf-8") as handle:
        for lineno, raw in enumerate(handle, start=1):
            stripped = raw.strip()
            if not stripped:
                continue
            try:
                event = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise CorruptRecordingError(
                    f"Malformed JSON at {recording_path}:{lineno}: {exc.msg}"
                ) from exc
            _validate_recording_event_shape(event, recording_path, lineno)
            count += 1
    return count


def _validate_recording_event_shape(
    event: object, recording_path: Path, lineno: int,
) -> None:
    """Enforce the TerminalRecordingEvent contract on one parsed line.

    The chapter offset that ``recording_event_count`` produces is consumed
    by the session viewer's replay (``static/js/dashboard/session_replay.js``)
    which only applies ``resize`` events with integer rows/cols and
    ``output`` events with ``data_b64``. Anything outside that shape would
    advance the chapter offset past events the viewer cannot faithfully
    replay, so reject it loudly here.
    """
    where = f"{recording_path}:{lineno}"
    if not isinstance(event, dict):
        raise CorruptRecordingError(
            f"Recording event at {where} is not a JSON object"
        )
    _require_int_field(event, "schema_version", where)
    _require_int_field(event, "offset_ms", where)
    event_type = event.get("event_type")
    if not isinstance(event_type, str) or not event_type:
        raise CorruptRecordingError(
            f"Recording event at {where} missing event_type"
        )
    if event_type == "output":
        data_b64 = event.get("data_b64")
        if not isinstance(data_b64, str) or not data_b64:
            raise CorruptRecordingError(
                f"output event at {where} missing usable data_b64"
            )
        # The browser-side replay decoder calls atob() on this value
        # (static/js/dashboard/session_replay.js); a string that's
        # non-empty but not actually base64 would crash the player at
        # scrub time. Validate decodability here so chapter offsets
        # never point at output events the viewer can't render.
        try:
            base64.b64decode(data_b64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise CorruptRecordingError(
                f"output event at {where} has data_b64 that is not valid base64: {exc}"
            ) from exc
    elif event_type == "resize":
        _require_int_field(event, "rows", where, error_label="resize event")
        _require_int_field(event, "cols", where, error_label="resize event")
    else:
        raise CorruptRecordingError(
            f"Recording event at {where} has unsupported event_type={event_type!r}"
        )


def _require_int_field(
    event: dict[str, Any], key: str, where: str, *, error_label: str = "Recording event",
) -> int:
    """Read an integer field or raise CorruptRecordingError with a useful message.

    Centralizes the get + isinstance + raise pattern so individual call
    sites read like declarations of what the schema requires rather than
    untyped dict pokes followed by isinstance narrowing.
    """
    value = event.get(key)
    if not isinstance(value, int):
        raise CorruptRecordingError(
            f"{error_label} at {where} missing integer {key}"
        )
    return value
