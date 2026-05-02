"""Persistent-PTY round runner for review-exchange.

Replaces the single-shot ``interactive_round`` flow for review-exchange
agents. One agent process is attached to a master/slave PTY pair at
exchange start and stays alive across all rounds. Each round:

  - ``send_round`` deletes any stale response file, writes the prompt
    plus a newline to the master fd, polls until the response file
    appears (or timeout), and returns the parsed response.
  - PTY output is captured continuously into a single recording — the
    session viewer plays one ``terminal-recording.jsonl`` per role
    spanning the whole exchange, instead of N per-phase files.

At exchange end, ``close_session`` sends ``SIGTERM`` to the agent's
process group and waits for it to exit. Closing the master fd alone is
not reliable — the spike showed Claude-shaped TUIs do not always exit
cleanly on stdin EOF.

This module is intentionally focused: spawn → drive rounds → terminate.
Wiring into ``control/review_exchange_loop.py`` lands in a follow-up so
the diff is reviewable in pieces.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import select
import shutil
import signal
import struct
import subprocess
import termios
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..infra.terminal_recording import MirroredTerminalRecordingWriter

logger = logging.getLogger(__name__)

_DEFAULT_PTY_COLS = 120
_DEFAULT_PTY_ROWS = 40
_DEFAULT_POLL_INTERVAL_SECONDS = 0.1
_DEFAULT_RESPONSE_DRAIN_SECONDS = 0.1
_DEFAULT_TERMINATE_GRACE_SECONDS = 5.0


class PersistentRoundError(RuntimeError):
    """Raised when the persistent agent dies unexpectedly mid-exchange."""


class PersistentRoundTimeoutError(TimeoutError):
    """Raised when a round's response file does not appear within the timeout."""


@dataclass
class PersistentSession:
    """One agent process attached to a PTY for the lifetime of an exchange.

    The same instance carries every round of the exchange. Callers must
    pair every ``open_persistent_session`` with a ``close_persistent_session``.
    """

    proc: subprocess.Popen[bytes]
    master_fd: int
    log_writer: MirroredTerminalRecordingWriter | None = None
    closed: bool = False


def open_persistent_session(
    *,
    command: list[str],
    working_dir: Path,
    env: dict[str, str],
    recording_path: Path | None = None,
    additional_recording_paths: list[Path] | None = None,
    mirror_path: Path | None = None,
) -> PersistentSession:
    """Spawn the agent attached to a PTY. Process stays alive across rounds.

    ``recording_path`` (when provided) gets the canonical raw recording for
    the role's session; ``additional_recording_paths`` is the run-level
    mirror that the session viewer reads. Pass nothing for tests that do
    not exercise the recording path.
    """
    cols, rows = shutil.get_terminal_size(fallback=(_DEFAULT_PTY_COLS, _DEFAULT_PTY_ROWS))
    master_fd, slave_fd = os.openpty()
    os.set_blocking(master_fd, False)
    _set_pty_geometry(slave_fd, rows=rows, cols=cols)

    log_writer: MirroredTerminalRecordingWriter | None = None
    if recording_path is not None:
        recording_path.parent.mkdir(parents=True, exist_ok=True)
        log_writer = MirroredTerminalRecordingWriter(
            recording_path,
            additional_recording_paths=additional_recording_paths or [],
            mirror_path=mirror_path,
            initial_rows=rows,
            initial_cols=cols,
        )

    try:
        proc = subprocess.Popen(
            command,
            cwd=str(working_dir),
            env=env,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            start_new_session=True,
        )
    except Exception:
        os.close(master_fd)
        os.close(slave_fd)
        if log_writer is not None:
            log_writer.close()
        raise
    os.close(slave_fd)
    logger.info(
        "Persistent agent session started: cmd=%s pid=%d",
        command[0] if command else "?",
        proc.pid,
    )
    return PersistentSession(proc=proc, master_fd=master_fd, log_writer=log_writer)


def send_round(
    session: PersistentSession,
    *,
    prompt: str,
    response_file: Path,
    timeout_seconds: float,
    poll_interval_seconds: float = _DEFAULT_POLL_INTERVAL_SECONDS,
    response_drain_seconds: float = _DEFAULT_RESPONSE_DRAIN_SECONDS,
    now: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Inject ``prompt`` into the persistent agent and wait for its response file.

    Stale response files are removed before the prompt is sent so the
    appearance of the file is unambiguously this round's response.

    A response file that exists but does not yet parse as JSON is treated
    as still-being-written: the loop keeps polling until the JSON parses,
    the agent exits, or the deadline expires. Without this tolerance, a
    non-atomic write from the agent (open, write opening brace, flush,
    write the rest) would race the orchestrator and surface as a fatal
    protocol error on the very first poll.

    ``now`` and ``sleep`` are injectable for deterministic tests.
    """
    if session.closed:
        raise PersistentRoundError("Session already closed; cannot send another round")
    response_file.unlink(missing_ok=True)
    payload = (prompt + "\n").encode("utf-8")
    os.write(session.master_fd, payload)

    deadline = now() + timeout_seconds
    while now() < deadline:
        _drain_pty_output(session)
        parsed = _try_read_response(response_file)
        if parsed is not None:
            _drain_pty_output_until_quiet(
                session,
                quiet_seconds=response_drain_seconds,
                now=now,
                sleep=sleep,
            )
            return parsed
        ret = session.proc.poll()
        if ret is not None:
            # Agent exited. Give one final read so we don't miss a response
            # that landed atomically right at exit.
            final = _try_read_response(response_file)
            if final is not None:
                return final
            if response_file.exists():
                raise PersistentRoundError(
                    f"Agent exited (code={ret}) leaving invalid JSON in {response_file}"
                )
            raise PersistentRoundError(
                f"Agent exited unexpectedly (code={ret}) before responding"
            )
        sleep(poll_interval_seconds)
    raise PersistentRoundTimeoutError(
        f"Agent did not produce valid JSON in {response_file} within {timeout_seconds}s"
    )


def _try_read_response(response_file: Path) -> dict[str, Any] | None:
    """Return the parsed JSON if the file exists and parses, else None.

    A returned ``None`` covers both "file not yet present" and "file
    present but the writer hasn't finished a complete JSON document yet"
    — both cases call for continued polling rather than escalation.
    """
    if not response_file.exists():
        return None
    try:
        text = response_file.read_text(encoding="utf-8")
    except OSError:
        return None
    if not text.strip():
        return None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed


def close_persistent_session(
    session: PersistentSession,
    *,
    grace_seconds: float = _DEFAULT_TERMINATE_GRACE_SECONDS,
) -> int | None:
    """Send SIGTERM to the agent's process group, then SIGKILL on grace expiry.

    Returns the exit code if reaped, ``None`` if the process refused to
    die. The master fd and log writer are closed regardless.
    """
    if session.closed:
        return session.proc.returncode
    try:
        if session.proc.poll() is None:
            try:
                os.killpg(os.getpgid(session.proc.pid), signal.SIGTERM)
            except (ProcessLookupError, OSError):
                pass
            try:
                session.proc.wait(timeout=grace_seconds)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(os.getpgid(session.proc.pid), signal.SIGKILL)
                except (ProcessLookupError, OSError):
                    pass
                try:
                    session.proc.wait(timeout=grace_seconds)
                except subprocess.TimeoutExpired:
                    logger.warning(
                        "Persistent agent (pid=%d) did not exit after SIGKILL",
                        session.proc.pid,
                    )
        # Final drain so any tail output makes it into the recording before
        # we close the writer.
        _drain_pty_output(session)
    finally:
        session.closed = True
        try:
            os.close(session.master_fd)
        except OSError:
            pass
        if session.log_writer is not None:
            session.log_writer.close()
    return session.proc.returncode


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
            if not isinstance(event, dict):
                raise CorruptRecordingError(
                    f"Recording event at {recording_path}:{lineno} is not a JSON object"
                )
            event_type = event.get("event_type")
            if not isinstance(event_type, str) or not event_type:
                raise CorruptRecordingError(
                    f"Recording event at {recording_path}:{lineno} missing event_type"
                )
            count += 1
    return count


def _drain_pty_output(session: PersistentSession) -> None:
    """Read everything currently available on the master fd into the log.

    When no log writer is configured (tests that don't care about
    output), the chunks are discarded — they've been read off the PTY,
    which is what matters to free the buffer.
    """
    while True:
        ready, _, _ = select.select([session.master_fd], [], [], 0)
        if not ready:
            return
        try:
            chunk = os.read(session.master_fd, 4096)
        except (BlockingIOError, OSError):
            return
        if not chunk:
            return
        if session.log_writer is not None:
            session.log_writer.write(chunk)


def _drain_pty_output_until_quiet(
    session: PersistentSession,
    *,
    quiet_seconds: float,
    now: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Keep reading until ``quiet_seconds`` of no output, or 1s hard cap."""
    deadline = now() + quiet_seconds
    hard_cap = now() + max(quiet_seconds, 1.0)
    while now() < deadline and now() < hard_cap:
        ready, _, _ = select.select([session.master_fd], [], [], 0)
        if not ready:
            sleep(min(quiet_seconds / 4, 0.05))
            continue
        try:
            chunk = os.read(session.master_fd, 4096)
        except (BlockingIOError, OSError):
            sleep(min(quiet_seconds / 4, 0.05))
            continue
        if not chunk:
            return
        if session.log_writer is not None:
            session.log_writer.write(chunk)
        deadline = now() + quiet_seconds


def _set_pty_geometry(slave_fd: int, *, rows: int, cols: int) -> None:
    try:
        size = struct.pack("HHHH", rows, cols, 0, 0)
        fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, size)
    except OSError:
        logger.debug("Failed to seed persistent-round PTY geometry", exc_info=True)
