"""Run a single interactive agent round via subprocess.

Uses ``subprocess.Popen`` instead of ``pexpect.spawn`` to avoid forking
from a multi-threaded process (uvicorn + SSE threads), which crashes on
macOS with "multi-threaded process forked".

Lives in the execution layer because it directly manages OS processes.
"""

from __future__ import annotations

import logging
import os
import shlex
import signal
import subprocess
import time
from pathlib import Path
from typing import IO

from .agent_runner_env import build_filtered_env
from .agent_runner_types import AgentResult, AgentSpec

logger = logging.getLogger(__name__)

_POLL_INTERVAL = 2.0


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

    log_fh: IO[bytes] | int = open(log_path, "ab") if log_path else subprocess.DEVNULL  # noqa: SIM115
    start_time = time.monotonic()
    timed_out = False
    try:
        proc = subprocess.Popen(
            ["/bin/bash", "-c", shell_cmd],
            cwd=str(spec.working_dir),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )

        deadline = time.monotonic() + spec.timeout_seconds
        while time.monotonic() < deadline:
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

        exit_code = proc.returncode or 0
    finally:
        if not isinstance(log_fh, int):
            log_fh.close()

    return AgentResult(
        exit_code=exit_code,
        timed_out=timed_out,
        duration_seconds=time.monotonic() - start_time,
        stderr="",
        command=spec.command,
        stdout="",
    )
