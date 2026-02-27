"""Core agent runner implementation.

This module provides the AgentRunner class that executes AI agents as subprocesses.
It handles:
- Subprocess invocation with proper isolation
- Timeout management
- Clean process termination

Output is NOT captured by AgentRunner. The agent's stdout/stderr are inherited
from the parent process so they flow through the pexpect PTY to CleaningLogWriter.
"""

import logging
import os
import signal
import subprocess
import time
from .env_filter import build_filtered_env
from .ports import RunResult, RunSpec

logger = logging.getLogger(__name__)


class AgentRunner:
    """Executes AI agents as subprocesses.

    AgentRunner is a simple, single-shot executor. It:
    - Runs the agent command exactly once
    - Enforces a timeout
    - Returns a result with exit code and timing

    Output flows through the parent's PTY (pexpect) to CleaningLogWriter.
    AgentRunner does NOT capture stdout/stderr — that's the terminal plugin's job.

    It does NOT:
    - Retry on failure
    - Run validation
    - Parse completion files
    - Manage terminal sessions (tmux, etc.)

    Those responsibilities belong to the orchestrator.
    """

    def run(self, spec: RunSpec) -> RunResult:
        """Run an agent according to the spec.

        Args:
            spec: Specification for what to run

        Returns:
            RunResult with exit code, timing, and timeout status
        """
        # Ensure output directory exists
        spec.output_dir.mkdir(parents=True, exist_ok=True)

        # Build filtered environment
        env = build_filtered_env(
            scrub_vars=spec.env_scrub if spec.env_scrub else None,
            passthrough_vars=spec.env_passthrough if spec.env_passthrough else None,
            overrides=spec.env_overrides,
        )

        logger.info(
            "Starting agent: %s in %s (timeout: %ds)",
            spec.command[0],
            spec.working_dir,
            spec.timeout_seconds,
        )

        start_time = time.monotonic()
        timed_out = False
        exit_code: int | None = None
        stderr = ""

        try:
            process = subprocess.Popen(
                spec.command,
                cwd=spec.working_dir,
                env=env,
                # Inherit stdout/stderr from parent so output flows through
                # the pexpect PTY to CleaningLogWriter -> ui-session.log.
                # setpgrp creates a new process group (for clean killpg)
                # without creating a new session (which would disconnect
                # from the controlling terminal and break interactive mode).
                preexec_fn=os.setpgrp,
            )

            try:
                exit_code = process.wait(timeout=spec.timeout_seconds)
            except subprocess.TimeoutExpired:
                logger.warning(
                    "Agent timed out after %ds, terminating",
                    spec.timeout_seconds,
                )
                timed_out = True
                self._terminate_process(process)

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

        return RunResult(
            exit_code=exit_code,
            stderr=stderr,
            duration_seconds=duration,
            timed_out=timed_out,
            command=spec.command,
        )

    def _terminate_process(self, process: subprocess.Popen) -> None:
        """Terminate a process gracefully, then forcefully if needed."""
        # Try SIGTERM first (graceful)
        try:
            pgid = os.getpgid(process.pid)
            os.killpg(pgid, signal.SIGTERM)
        except (ProcessLookupError, OSError):
            return

        # Wait briefly for graceful termination
        try:
            process.wait(timeout=5)
            return
        except subprocess.TimeoutExpired:
            pass

        # Force kill with SIGKILL
        logger.warning("Agent did not terminate gracefully, using SIGKILL")
        try:
            pgid = os.getpgid(process.pid)
            os.killpg(pgid, signal.SIGKILL)
            process.wait(timeout=5)
        except (ProcessLookupError, OSError, subprocess.TimeoutExpired):
            pass
