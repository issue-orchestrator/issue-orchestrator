"""PTY output quiescence detection.

Used between completion-file-written and ``send_to_session(next_prompt)`` so
the TUI has time to finish rendering its post-tool output before we inject
the next round's prompt. The completion file is the authoritative round-end
signal; quiescence is just a "settle window" before sending the next message.

Tests drive this with injectable clock/sleep callables so they don't depend
on real wall-clock timing — see ``tests/unit/AGENTS.md`` rule against
timing-based unit coordination.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path


class MissingRecordingError(FileNotFoundError):
    """Raised when the recording file is absent and ``require_recording`` is on.

    The session-replay contract guarantees the PTY adapter creates
    ``terminal-recording.jsonl`` at session start. If the file is missing
    when the orchestrator is asking about quiescence, the caller passed
    the wrong path or capture failed — both are bugs the orchestrator
    must surface, not silently treat as "no output, therefore quiescent."
    """


def wait_for_pty_quiescence(
    recording_path: Path,
    *,
    quiet_seconds: float = 1.0,
    max_wait_seconds: float = 30.0,
    poll_interval_seconds: float = 0.25,
    require_recording: bool = True,
    now: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    """Block until ``recording_path`` stops growing for ``quiet_seconds``.

    Returns ``True`` once quiescence is reached. Returns ``False`` if
    ``max_wait_seconds`` elapses without ever achieving the quiet window —
    the caller can decide whether to send the next prompt anyway (most
    TUIs buffer stdin) or escalate.

    Raises ``MissingRecordingError`` if ``require_recording`` is True (the
    default) and the recording file does not exist when the helper is
    invoked. Bootstrap/test paths that genuinely have no recording yet
    must opt out by passing ``require_recording=False``.

    The polling interval should be much smaller than ``quiet_seconds`` so
    a brief blip of output is observable before the quiet window closes.

    ``now`` and ``sleep`` are injectable for deterministic tests; default
    to ``time.monotonic``/``time.sleep``.
    """
    if quiet_seconds <= 0:
        return True
    if require_recording and not recording_path.exists():
        raise MissingRecordingError(
            f"PTY recording not found at {recording_path}; cannot decide quiescence"
        )
    start = now()
    deadline = start + max_wait_seconds
    last_size = _safe_size(recording_path)
    last_change = start
    while True:
        current = now()
        if current >= deadline:
            return False
        size = _safe_size(recording_path)
        if size != last_size:
            last_size = size
            last_change = current
        elif current - last_change >= quiet_seconds:
            return True
        sleep(poll_interval_seconds)


def _safe_size(path: Path) -> int:
    """Return current file size in bytes, treating "missing" as zero.

    Used after the require_recording gate has already passed (or been
    explicitly opted out of), so a transient ``FileNotFoundError`` from
    e.g. concurrent rename is the only realistic source — treat as zero
    so the loop re-arms instead of crashing.
    """
    try:
        return path.stat().st_size
    except FileNotFoundError:
        return 0
