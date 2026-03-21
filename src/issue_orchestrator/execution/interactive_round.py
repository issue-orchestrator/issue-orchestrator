"""Run a single interactive agent round via subprocess-managed PTY.

Uses ``subprocess.Popen`` instead of ``pexpect.spawn`` to avoid the
multi-threaded fork hazards called out in the review exchange loop, while
still attaching the agent to a PTY so the canonical session replay matches
what a user would see running the agent directly.
"""

from __future__ import annotations

import logging
import fcntl
import os
import shlex
import signal
import shutil
import struct
import subprocess
import time
import select
import termios
from pathlib import Path

from .agent_runner_env import build_filtered_env
from .agent_runner_types import AgentResult, AgentSpec
from ..infra.terminal_recording import MirroredTerminalRecordingWriter

logger = logging.getLogger(__name__)

_POLL_INTERVAL = 0.2
_DEFAULT_PTY_COLS = 120
_DEFAULT_PTY_ROWS = 40


def run_interactive_round(
    spec: AgentSpec,
    response_file: Path,
) -> AgentResult:
    """Run a single exchange round with an interactive provider.

    The prompt is already embedded in the command as a positional argument,
    so the agent starts working immediately.  We poll for the response file
    and kill the process once it appears (or on timeout).
    """
    env = build_filtered_env(
        scrub_vars=spec.env_scrub if spec.env_scrub else None,
        passthrough_vars=spec.env_passthrough if spec.env_passthrough else None,
        overrides=spec.env_overrides,
    )

    log_path = spec.log_path
    if log_path is not None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
    spec.output_dir.mkdir(parents=True, exist_ok=True)

    shell_cmd = shlex.join(spec.command)
    logger.info("Interactive round: %s", shell_cmd[:200])

    cols, rows = shutil.get_terminal_size(fallback=(_DEFAULT_PTY_COLS, _DEFAULT_PTY_ROWS))
    log_writer = (
        MirroredTerminalRecordingWriter(
            log_path,
            mirror_path=spec.mirror_log_path,
            initial_rows=rows,
            initial_cols=cols,
        )
        if log_path
        else None
    )
    master_fd, slave_fd = os.openpty()
    os.set_blocking(master_fd, False)
    _set_pty_geometry(slave_fd, rows=rows, cols=cols)
    start_time = time.monotonic()
    timed_out = False
    try:
        proc = subprocess.Popen(
            ["/bin/bash", "-c", shell_cmd],
            cwd=str(spec.working_dir),
            env=env,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            start_new_session=True,
        )
        os.close(slave_fd)
        slave_fd = -1

        deadline = time.monotonic() + spec.timeout_seconds
        while time.monotonic() < deadline:
            _drain_pty_output(master_fd, log_writer)
            if response_file.exists():
                logger.info("Interactive agent wrote response file")
                break
            ret = proc.poll()
            if ret is not None:
                logger.info("Interactive agent exited (code=%d) before writing response", ret)
                break
            time.sleep(_POLL_INTERVAL)
        else:
            timed_out = True
            logger.warning("Interactive agent timed out after %ds", spec.timeout_seconds)

        # Terminate the process group
        if proc.poll() is None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except (ProcessLookupError, OSError):
                pass
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except (ProcessLookupError, OSError):
                    pass
                proc.wait(timeout=5)

        _drain_pty_output(master_fd, log_writer)
        exit_code = proc.returncode or 0
    finally:
        if slave_fd >= 0:
            os.close(slave_fd)
        try:
            os.close(master_fd)
        except OSError:
            pass
        if log_writer is not None:
            log_writer.close()

    return AgentResult(
        exit_code=exit_code,
        timed_out=timed_out,
        duration_seconds=time.monotonic() - start_time,
        stderr="",
        command=spec.command,
        stdout="",
    )


def _set_pty_geometry(slave_fd: int, *, rows: int, cols: int) -> None:
    try:
        size = struct.pack("HHHH", rows, cols, 0, 0)
        fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, size)
    except OSError:
        logger.debug("Failed to seed interactive round PTY geometry", exc_info=True)


def _drain_pty_output(master_fd: int, log_writer: MirroredTerminalRecordingWriter | None) -> None:
    if log_writer is None:
        while True:
            ready, _, _ = select.select([master_fd], [], [], 0)
            if not ready:
                return
            try:
                chunk = os.read(master_fd, 4096)
            except BlockingIOError:
                return
            except OSError:
                return
            if not chunk:
                return
        return

    while True:
        ready, _, _ = select.select([master_fd], [], [], 0)
        if not ready:
            return
        try:
            chunk = os.read(master_fd, 4096)
        except BlockingIOError:
            return
        except OSError:
            return
        if not chunk:
            return
        log_writer.write(chunk)
