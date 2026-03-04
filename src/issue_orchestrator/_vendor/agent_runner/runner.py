"""Core agent runner implementation (vendored, used by provider_runner).

This module provides the AgentRunner class that executes AI agents as subprocesses.
It handles:
- Subprocess invocation with proper isolation
- Timeout management
- Clean process termination
- Stderr capture for provider error classification

Stdout inherits the parent process's file descriptor (typically a PTY slave)
so output flows in real-time through pexpect → CleaningLogWriter → ui-session.log.
Only stderr is captured via PIPE for provider error classification (retry logic);
a tee thread relays it to the parent's stderr in real-time.
"""

import logging
import os
import random
import shlex
import subprocess
import sys
import threading
import time

from .env_filter import build_filtered_env
from .errors import ProviderErrorType, classify_provider_error
from .ports import RunResult, RunSpec

logger = logging.getLogger(__name__)


def _tee_stream(
    source,
    dest,
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


def _format_command_for_log(command: list[str], max_arg_length: int = 160) -> str:
    """Render argv for logs while keeping long prompt args bounded."""
    rendered: list[str] = []
    for arg in command:
        text = str(arg)
        if len(text) > max_arg_length:
            text = text[:max_arg_length] + "..."
        rendered.append(shlex.quote(text))
    return " ".join(rendered)


class AgentRunner:
    """Executes AI agents as subprocesses.

    AgentRunner is a simple, single-shot executor. It:
    - Runs the agent command exactly once
    - Captures stdout/stderr to files
    - Enforces a timeout
    - Returns a result with exit code, output, and timing

    It does NOT:
    - Retry on failure
    - Run validation
    - Parse completion files
    - Manage terminal sessions (tmux, etc.)

    Those responsibilities belong to the orchestrator.

    Example:
        runner = AgentRunner()
        result = runner.run(RunSpec(
            command=["claude", "-p", "Fix the bug"],
            working_dir=Path("/repo"),
            timeout_seconds=300,
            output_dir=Path("/output"),
        ))

        if result.succeeded:
            print("Agent completed successfully")
        elif result.timed_out:
            print("Agent timed out")
        else:
            print(f"Agent failed with exit code {result.exit_code}")
    """

    def run(self, spec: RunSpec) -> RunResult:
        """Run an agent according to the spec.

        Args:
            spec: Specification for what to run

        Returns:
            RunResult with exit code, output, timing, and timeout status
        """
        attempts = 0
        last_result: RunResult | None = None
        last_error_type: ProviderErrorType | None = None

        max_attempts = spec.retry_policy.max_attempts if spec.retry_policy else 1

        while True:
            attempts += 1
            last_result = self._run_once(spec, attempts=attempts, max_attempts=max_attempts)
            last_error_type = classify_provider_error(
                stdout=last_result.stdout,
                stderr=last_result.stderr,
                exit_code=last_result.exit_code,
                timed_out=last_result.timed_out,
            )

            if last_result.succeeded:
                break
            if spec.retry_policy is None:
                break
            if last_error_type != ProviderErrorType.TRANSIENT:
                break
            if attempts >= spec.retry_policy.max_attempts:
                break

            backoff = self._compute_backoff(spec, attempts)
            logger.warning(
                "Transient provider error, retrying in %.1fs (attempt %d/%d)",
                backoff,
                attempts + 1,
                spec.retry_policy.max_attempts,
            )
            time.sleep(backoff)

        assert last_result is not None
        return RunResult(
            exit_code=last_result.exit_code,
            stdout="",
            stderr=last_result.stderr,
            duration_seconds=last_result.duration_seconds,
            timed_out=last_result.timed_out,
            command=last_result.command,
            provider_error_type=last_error_type,
            attempts=attempts,
        )

    def _compute_backoff(self, spec: RunSpec, attempts: int) -> float:
        """Compute backoff delay for the next retry."""
        if spec.retry_policy is None:
            return 0.0
        base = spec.retry_policy.initial_backoff_seconds * (2 ** max(0, attempts - 1))
        delay = min(base, spec.retry_policy.max_backoff_seconds)
        if spec.retry_policy.jitter:
            return random.uniform(0, delay)
        return delay

    def _run_once(self, spec: RunSpec, *, attempts: int, max_attempts: int) -> RunResult:
        """Execute a single attempt of the agent command."""
        # Ensure output directory exists
        spec.output_dir.mkdir(parents=True, exist_ok=True)

        # Build filtered environment
        env = build_filtered_env(
            scrub_vars=spec.env_scrub if spec.env_scrub else None,
            passthrough_vars=spec.env_passthrough if spec.env_passthrough else None,
            overrides=spec.env_overrides,
        )

        logger.info(
            "Starting agent: %s in %s (timeout: %ds) attempt=%d/%d",
            spec.command[0],
            spec.working_dir,
            spec.timeout_seconds,
            attempts,
            max_attempts,
        )
        logger.info("Agent argv: %s", _format_command_for_log(spec.command))

        start_time = time.monotonic()
        timed_out = False
        exit_code: int | None = None
        stdout = ""
        stderr = ""

        try:
            process = subprocess.Popen(
                spec.command,
                cwd=spec.working_dir,
                env=env,
                # stdin=DEVNULL prevents SIGTTIN: setpgrp puts the child in a
                # background process group relative to the parent's PTY, so
                # any read from the inherited PTY slave triggers SIGTTIN and
                # stops the process. Agents don't need stdin.
                #
                # stdout inherits the parent's fd (PTY slave) so output
                # streams in real-time through pexpect → ui-session.log.
                # Only stderr is captured via PIPE for error classification.
                #
                # setpgrp creates a new process group (for clean killpg)
                # without creating a new session (which would disconnect
                # from the controlling terminal and break interactive mode).
                stdin=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                preexec_fn=os.setpgrp,
            )

            # Tee stderr to the parent in real-time while accumulating for
            # error classification. Stdout flows directly through the PTY.
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
                self._terminate_process(process)

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

        return RunResult(
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            duration_seconds=duration,
            timed_out=timed_out,
            command=spec.command,
        )

    def _terminate_process(self, process: subprocess.Popen) -> None:
        """Terminate a process gracefully, then forcefully if needed.

        Args:
            process: The subprocess to terminate
        """
        import signal

        # Try SIGTERM first (graceful)
        try:
            # Kill the process group (includes any children)
            pgid = os.getpgid(process.pid)
            os.killpg(pgid, signal.SIGTERM)
        except (ProcessLookupError, OSError):
            # Process already gone
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
            # Best effort - process may be in uninterruptible state
            pass
