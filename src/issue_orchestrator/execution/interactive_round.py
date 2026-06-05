"""Run a single interactive agent round via subprocess-managed PTY.

Uses ``subprocess.Popen`` instead of ``pexpect.spawn`` to avoid the
multi-threaded fork hazards called out in the review exchange loop, while
still attaching the agent to a PTY so the canonical session replay matches
what a user would see running the agent directly.
"""

from __future__ import annotations

from dataclasses import dataclass
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
from ..infra.shutdown_signals import child_signal_reset_preexec
from ..infra.terminal_recording import MirroredTerminalRecordingWriter

logger = logging.getLogger(__name__)

_POLL_INTERVAL = 0.2
_DEFAULT_PTY_COLS = 120
_DEFAULT_PTY_ROWS = 40
_POST_RESPONSE_DRAIN_SECONDS = 0.1
_PROCESS_GROUP_WAIT_SECONDS = 5


@dataclass
class _RoundResources:
    master_fd: int
    slave_fd: int
    log_writer: MirroredTerminalRecordingWriter | None


def run_interactive_round(
    spec: AgentSpec,
    response_file: Path,
) -> AgentResult:
    """Run a single exchange round with an interactive provider.

    The prompt is already embedded in the command as a positional argument,
    so the agent starts working immediately.  We poll for the response file
    and kill the process once it appears (or on timeout).
    """
    shell_cmd = shlex.join(spec.command)
    logger.info("Interactive round: %s", shell_cmd[:200])

    env = _build_round_env(spec)
    resources = _prepare_round_resources(spec)
    start_time = time.monotonic()
    timed_out = False
    try:
        proc = _spawn_interactive_process(spec, shell_cmd, env, resources.slave_fd)
        os.close(resources.slave_fd)
        resources.slave_fd = -1

        timed_out = _wait_for_response_or_exit(
            proc=proc,
            response_file=response_file,
            master_fd=resources.master_fd,
            log_writer=resources.log_writer,
            timeout_seconds=spec.timeout_seconds,
        )
        _terminate_process_group(proc)
        _drain_pty_output(resources.master_fd, resources.log_writer)
        exit_code = proc.returncode or 0
    finally:
        _close_round_resources(resources)

    return AgentResult(
        exit_code=exit_code,
        timed_out=timed_out,
        duration_seconds=time.monotonic() - start_time,
        stderr="",
        command=spec.command,
        stdout="",
    )


def _build_round_env(spec: AgentSpec) -> dict[str, str]:
    return build_filtered_env(
        scrub_vars=spec.env_scrub if spec.env_scrub else None,
        passthrough_vars=spec.env_passthrough if spec.env_passthrough else None,
        overrides=spec.env_overrides,
    )


def _prepare_round_resources(spec: AgentSpec) -> _RoundResources:
    log_path = spec.log_path
    if log_path is not None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
    spec.output_dir.mkdir(parents=True, exist_ok=True)

    cols, rows = shutil.get_terminal_size(fallback=(_DEFAULT_PTY_COLS, _DEFAULT_PTY_ROWS))
    log_writer = (
        MirroredTerminalRecordingWriter(
            log_path,
            additional_recording_paths=spec.additional_recording_paths,
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
    return _RoundResources(master_fd=master_fd, slave_fd=slave_fd, log_writer=log_writer)


def _spawn_interactive_process(
    spec: AgentSpec,
    shell_cmd: str,
    env: dict[str, str],
    slave_fd: int,
) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        ["/bin/bash", "-c", shell_cmd],
        cwd=str(spec.working_dir),
        env=env,
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        start_new_session=True,
        # Don't let the agent inherit the orchestrator's blocked SIGTERM/SIGINT
        # mask (it is stopped via SIGTERM). See infra.shutdown_signals.
        preexec_fn=child_signal_reset_preexec(),
    )


def _wait_for_response_or_exit(
    *,
    proc: subprocess.Popen[bytes],
    response_file: Path,
    master_fd: int,
    log_writer: MirroredTerminalRecordingWriter | None,
    timeout_seconds: int,
) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        _drain_pty_output(master_fd, log_writer)
        if response_file.exists():
            _drain_pty_output_until_quiet(
                master_fd,
                log_writer,
                quiet_seconds=_POST_RESPONSE_DRAIN_SECONDS,
            )
            logger.info("Interactive agent wrote response file")
            return False
        ret = proc.poll()
        if ret is not None:
            logger.info("Interactive agent exited (code=%d) before writing response", ret)
            return False
        time.sleep(_POLL_INTERVAL)

    logger.warning("Interactive agent timed out after %ds", timeout_seconds)
    return True


def _terminate_process_group(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is not None:
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (ProcessLookupError, OSError):
        return

    try:
        proc.wait(timeout=_PROCESS_GROUP_WAIT_SECONDS)
        return
    except subprocess.TimeoutExpired:
        pass

    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, OSError):
        return
    try:
        proc.wait(timeout=_PROCESS_GROUP_WAIT_SECONDS)
    except subprocess.TimeoutExpired:
        logger.warning(
            "Interactive agent did not exit after SIGKILL within %ss; preserving captured response",
            _PROCESS_GROUP_WAIT_SECONDS,
        )


def _close_round_resources(resources: _RoundResources) -> None:
    if resources.slave_fd >= 0:
        os.close(resources.slave_fd)
    try:
        os.close(resources.master_fd)
    except OSError:
        pass
    if resources.log_writer is not None:
        resources.log_writer.close()


def _set_pty_geometry(slave_fd: int, *, rows: int, cols: int) -> None:
    try:
        size = struct.pack("HHHH", rows, cols, 0, 0)
        fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, size)
    except OSError:
        logger.debug("Failed to seed interactive round PTY geometry", exc_info=True)


def _drain_pty_output(master_fd: int, log_writer: MirroredTerminalRecordingWriter | None) -> None:
    for chunk in _iter_pty_chunks(master_fd):
        if log_writer is not None:
            log_writer.write(chunk)


def _drain_pty_output_until_quiet(
    master_fd: int,
    log_writer: MirroredTerminalRecordingWriter | None,
    *,
    quiet_seconds: float,
) -> None:
    deadline = time.monotonic() + quiet_seconds
    while time.monotonic() < deadline:
        chunks = _iter_pty_chunks(master_fd)
        if chunks:
            if log_writer is not None:
                for chunk in chunks:
                    log_writer.write(chunk)
            deadline = time.monotonic() + quiet_seconds
            continue
        time.sleep(0.01)


def _iter_pty_chunks(master_fd: int) -> list[bytes]:
    chunks: list[bytes] = []
    while True:
        ready, _, _ = select.select([master_fd], [], [], 0)
        if not ready:
            return chunks
        try:
            chunk = os.read(master_fd, 4096)
        except (BlockingIOError, OSError):
            return chunks
        if not chunk:
            return chunks
        chunks.append(chunk)
