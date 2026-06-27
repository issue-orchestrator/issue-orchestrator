"""PTY I/O helpers for persistent round sessions."""

from __future__ import annotations

import logging
import select
import time
from collections.abc import Callable
from typing import Protocol

from ..infra.terminal_recording import MirroredTerminalRecordingWriter

logger = logging.getLogger(__name__)


class _ReadablePersistentSession(Protocol):
    master_fd: int
    closed: bool
    log_writer: MirroredTerminalRecordingWriter | None
    output_observer: Callable[[bytes], None] | None


def drain_pty_output_until_quiet(
    session: _ReadablePersistentSession,
    *,
    quiet_seconds: float,
    now: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Keep reading from a persistent PTY until output stays quiet."""
    deadline = now() + quiet_seconds
    hard_cap = now() + max(quiet_seconds, 1.0)
    while now() < deadline and now() < hard_cap:
        if session.closed:
            return
        try:
            ready, _, _ = select.select([session.master_fd], [], [], 0)
        except OSError:
            logger.debug(
                "[send_round] quiet-drain skipped for closed fd=%d",
                session.master_fd,
            )
            return
        if not ready:
            sleep(min(quiet_seconds / 4, 0.05))
            continue
        try:
            chunk = os_read(session.master_fd)
        except (BlockingIOError, OSError):
            sleep(min(quiet_seconds / 4, 0.05))
            continue
        if not chunk:
            return
        if session.log_writer is not None:
            session.log_writer.write(chunk)
        if session.output_observer is not None:
            session.output_observer(chunk)
        deadline = now() + quiet_seconds


def os_read(fd: int) -> bytes:
    """Isolate the raw read for focused tests/monkeypatching if needed."""
    import os

    return os.read(fd, 4096)
