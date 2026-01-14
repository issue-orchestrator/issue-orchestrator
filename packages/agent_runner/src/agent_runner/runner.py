"""Core agent runner implementation.

This module provides the AgentRunner class that executes AI agents as subprocesses.
It handles:
- Subprocess invocation with proper isolation
- Timeout management
- Output capture to files
- Clean process termination
"""

import logging
import subprocess
import time
from pathlib import Path

from .env_filter import build_filtered_env
from .ports import RunResult, RunSpec

logger = logging.getLogger(__name__)


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
            "Starting agent: %s in %s (timeout: %ds)",
            spec.command[0],
            spec.working_dir,
            spec.timeout_seconds,
        )

        start_time = time.monotonic()
        timed_out = False
        exit_code: int | None = None

        try:
            with (
                open(stdout_path, "w") as stdout_file,
                open(stderr_path, "w") as stderr_file,
            ):
                process = subprocess.Popen(
                    spec.command,
                    cwd=spec.working_dir,
                    env=env,
                    stdout=stdout_file,
                    stderr=stderr_file,
                    # Start in new session to allow clean termination
                    start_new_session=True,
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

        # Read output files
        stdout = stdout_path.read_text() if stdout_path.exists() else ""
        stderr = stderr_path.read_text() if stderr_path.exists() else ""

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
