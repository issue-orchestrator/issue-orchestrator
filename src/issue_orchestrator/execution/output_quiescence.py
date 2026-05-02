"""PTY output quiescence detection.

Used between completion-file-written and ``send_to_session(next_prompt)`` so
the TUI has time to finish rendering its post-tool output before we inject
the next round's prompt. The completion file is the authoritative round-end
signal; quiescence is just a "settle window" before sending the next message.
"""

from __future__ import annotations

import time
from pathlib import Path


def wait_for_pty_quiescence(
    recording_path: Path,
    *,
    quiet_seconds: float = 1.0,
    max_wait_seconds: float = 30.0,
    poll_interval_seconds: float = 0.25,
) -> bool:
    """Block until ``recording_path`` stops growing for ``quiet_seconds``.

    Returns ``True`` once quiescence is reached. Returns ``False`` if
    ``max_wait_seconds`` elapses without ever achieving the quiet window —
    the caller can decide whether to send the next prompt anyway (most
    TUIs buffer stdin) or escalate.

    The polling interval should be much smaller than ``quiet_seconds`` so
    a brief blip of output is observable before the quiet window closes.
    """
    if quiet_seconds <= 0:
        return True
    start = time.monotonic()
    deadline = start + max_wait_seconds
    last_size = _safe_size(recording_path)
    last_change = start
    while True:
        now = time.monotonic()
        if now >= deadline:
            return False
        size = _safe_size(recording_path)
        if size != last_size:
            last_size = size
            last_change = now
        elif now - last_change >= quiet_seconds:
            return True
        time.sleep(poll_interval_seconds)


def _safe_size(path: Path) -> int:
    """Return current file size in bytes, treating "missing" as zero.

    Recording files are written by the PTY adapter; in test setups and very
    early bring-up the file may not exist yet. Treat that as "no output yet"
    rather than failing — the loop will pick up the first byte and re-arm.
    """
    try:
        return path.stat().st_size
    except FileNotFoundError:
        return 0
