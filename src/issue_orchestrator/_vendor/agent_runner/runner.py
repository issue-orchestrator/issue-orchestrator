"""Core agent runner implementation.

This module provides the AgentRunner class that executes AI agents as subprocesses.
It handles:
- Subprocess invocation with proper isolation
- Timeout management
- Output capture to files
- Clean process termination
"""

import logging
import random
import shlex
import subprocess
import time

from .env_filter import build_filtered_env
from .errors import ProviderErrorType, classify_provider_error
from .ports import RunResult, RunSpec
from .stream_capture import capture_process_output

logger = logging.getLogger(__name__)


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
            stdout=last_result.stdout,
            stderr=last_result.stderr,
            stdout_path=last_result.stdout_path,
            stderr_path=last_result.stderr_path,
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

        stdout_path = spec.output_dir / "stdout.log"
        stderr_path = spec.output_dir / "stderr.log"

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
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                # Start in new session to allow clean termination
                start_new_session=True,
            )

            capture = capture_process_output(
                process,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
                timeout_seconds=spec.timeout_seconds,
                on_timeout=lambda: self._terminate_process(process),
            )
            exit_code = capture.exit_code
            timed_out = capture.timed_out
            stdout = capture.stdout
            stderr = capture.stderr
            if timed_out:
                logger.warning(
                    "Agent timed out after %ds, terminating",
                    spec.timeout_seconds,
                )

        except FileNotFoundError:
            logger.error("Command not found: %s", spec.command[0])
            # Write error to stderr file
            stderr_path.write_text(f"Command not found: {spec.command[0]}\n")
            exit_code = 127  # Standard "command not found" exit code

        except PermissionError:
            logger.error("Permission denied executing: %s", spec.command[0])
            stderr_path.write_text(f"Permission denied: {spec.command[0]}\n")
            exit_code = 126  # Standard "permission denied" exit code

        except OSError as e:
            logger.error("OS error running agent: %s", e)
            stderr_path.write_text(f"OS error: {e}\n")
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
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            duration_seconds=duration,
            timed_out=timed_out,
            command=spec.command,
        )

    def _terminate_process(self, process: subprocess.Popen) -> None:
        """Terminate a process gracefully, then forcefully if needed.

        Args:
            process: The subprocess to terminate
        """
        import os
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
