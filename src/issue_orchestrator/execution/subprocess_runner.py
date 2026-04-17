"""Subprocess-based agent runner (replaces _vendor/agent_runner/runner.py).

Used by ``provider_runner`` (runs INSIDE a pexpect PTY) and
``validation_retry``.  Stdout inherits the parent's file descriptor
(typically a PTY slave) so output flows in real-time; only stderr is
captured via PIPE for provider error classification.

Key safety properties:
- ``stdin=DEVNULL`` prevents SIGTTIN from fd 0 reads
- ``preexec_fn=_agent_preexec`` applies setpgrp + SIG_IGN for SIGTTIN/SIGTTOU
- stdout inherits (no PIPE) for real-time PTY streaming
- stderr via PIPE for error classification; tee'd to sys.stderr.buffer
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import threading
import time

from issue_orchestrator.execution.agent_runner_base import (
    BaseAgentRunner,
    _agent_preexec,
)
from issue_orchestrator.execution.agent_runner_env import build_filtered_env
from issue_orchestrator.execution.agent_runner_types import AgentResult, AgentSpec

logger = logging.getLogger(__name__)
_PROCESS_GROUP_WAIT_SECONDS = 5


def _tee_stream(
    source,  # noqa: ANN001
    dest,  # noqa: ANN001
    captured_chunks: list[bytes],
) -> None:
    """Read from *source* pipe, write to *dest* in real-time, accumulate for classification."""
    try:
        while True:
            chunk = source.read(4096)
            if not chunk:
                break
            dest.write(chunk)
            dest.flush()
            captured_chunks.append(chunk)
    except (OSError, ValueError):
        pass


class SubprocessAgentRunner(BaseAgentRunner):
    """Agent runner using ``subprocess.Popen``.

    Stdout inherits the parent PTY for real-time streaming to ui-session.log.
    Only stderr is captured via PIPE for provider error classification;
    a tee thread relays it to ``sys.stderr.buffer`` in real-time.
    """

    def _execute_once(self, spec: AgentSpec, *, attempt: int) -> AgentResult:
        """Execute a single attempt of the agent command."""
        spec.output_dir.mkdir(parents=True, exist_ok=True)

        env = build_filtered_env(
            scrub_vars=spec.env_scrub if spec.env_scrub else None,
            passthrough_vars=spec.env_passthrough if spec.env_passthrough else None,
            overrides=spec.env_overrides,
        )

        max_attempts = spec.retry_policy.max_attempts if spec.retry_policy else 1
        self._log_start(spec, attempt, max_attempts)

        start_time = time.monotonic()
        timed_out = False
        exit_code: int | None = None
        stderr = ""

        try:
            process = subprocess.Popen(
                spec.command,
                cwd=spec.working_dir,
                env=env,
                stdin=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                preexec_fn=_agent_preexec,
            )

            stderr_chunks: list[bytes] = []
            stderr_thread = threading.Thread(
                target=_tee_stream,
                args=(process.stderr, sys.stderr.buffer, stderr_chunks),
                daemon=True,
            )
            stderr_thread.start()

            try:
                exit_code = process.wait(timeout=spec.timeout_seconds)
            except subprocess.TimeoutExpired:
                timed_out = True
                logger.warning(
                    "Agent timed out after %ds, terminating",
                    spec.timeout_seconds,
                )
                self._terminate_popen(process)

            stderr_thread.join(timeout=5)
            stderr = b"".join(stderr_chunks).decode("utf-8", errors="replace")

        except FileNotFoundError:
            logger.error("Command not found: %s", spec.command[0])
            stderr = f"Command not found: {spec.command[0]}"
            exit_code = 127

        except PermissionError:
            logger.error("Permission denied executing: %s", spec.command[0])
            stderr = f"Permission denied: {spec.command[0]}"
            exit_code = 126

        except OSError as e:
            logger.error("OS error running agent: %s", e)
            stderr = f"OS error: {e}"
            exit_code = 1

        duration = time.monotonic() - start_time

        logger.info(
            "Agent finished: exit_code=%s, timed_out=%s, duration=%.1fs",
            exit_code,
            timed_out,
            duration,
        )

        return AgentResult(
            exit_code=exit_code,
            timed_out=timed_out,
            duration_seconds=duration,
            stderr=stderr,
            command=spec.command,
        )

    def _terminate_popen(self, process: subprocess.Popen) -> None:
        """Terminate a Popen process via process-group kill."""
        if process.poll() is not None:
            return
        try:
            pgid = os.getpgid(process.pid)
            os.killpg(pgid, signal.SIGTERM)
        except (ProcessLookupError, OSError):
            return

        try:
            process.wait(timeout=_PROCESS_GROUP_WAIT_SECONDS)
            return
        except subprocess.TimeoutExpired:
            pass

        logger.warning("Agent did not terminate gracefully, using SIGKILL")
        try:
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, OSError):
            return
        try:
            process.wait(timeout=_PROCESS_GROUP_WAIT_SECONDS)
        except subprocess.TimeoutExpired:
            pass
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
