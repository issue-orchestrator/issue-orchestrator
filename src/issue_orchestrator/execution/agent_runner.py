"""Unified agent runner — the single abstraction for all agent execution.

Every agent subprocess in the orchestrator MUST go through this module.
It creates a pexpect PTY, routes output through CleaningLogWriter,
applies environment filtering, and enforces timeouts.

Architecture:
    AgentRunner.start(spec)  →  AgentSession (handle)
    AgentRunner.run(spec)    →  AgentResult  (start + wait + retry)

All callers — coding sessions, review exchange rounds, simulated tests —
use the same code path. No subprocess.run, no raw pexpect.spawn elsewhere.
"""

from __future__ import annotations

import logging
import os
import random
import shlex
import signal
import time
from dataclasses import dataclass, field
from pathlib import Path

import pexpect

from issue_orchestrator._vendor.agent_runner.env_filter import build_filtered_env
from issue_orchestrator._vendor.agent_runner.errors import (
    ProviderErrorType,
    classify_provider_error,
)
from issue_orchestrator.infra.terminal_cleaning import CleaningLogWriter

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class RetryPolicy:
    """Retry policy for transient provider failures."""

    max_attempts: int = 4
    initial_backoff_seconds: int = 5
    max_backoff_seconds: int = 60
    jitter: bool = True


@dataclass
class AgentSpec:
    """What to run.

    Attributes:
        command: Agent command as argv list (e.g. ["claude", "-p", "prompt"]).
                 Passed to ``bash -c`` via :func:`shlex.join`.
        working_dir: Directory to run the agent in (typically a git worktree).
        timeout_seconds: Maximum time to wait for the agent to complete.
        log_path: Path for the cleaned session log (ui-session.log).
        output_dir: Directory for artifacts (completion.json, etc.).
        env_overrides: Environment variables to set (highest priority).
        env_scrub: Variables to remove from the environment (security).
        env_passthrough: Allowlist mode — only these vars pass through.
        retry_policy: Optional retry policy for transient provider errors.
    """

    command: list[str]
    working_dir: Path
    timeout_seconds: int
    log_path: Path
    output_dir: Path
    env_overrides: dict[str, str] = field(default_factory=dict)
    env_passthrough: list[str] = field(default_factory=list)
    env_scrub: list[str] = field(default_factory=list)
    retry_policy: RetryPolicy | None = None

    def __post_init__(self) -> None:
        if not self.command:
            raise ValueError("command cannot be empty")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")


@dataclass
class AgentResult:
    """What happened.

    The ``stderr`` field only contains launch-level errors (command not found,
    permission denied).  Agent output flows through the PTY to
    CleaningLogWriter → ui-session.log.
    """

    exit_code: int | None
    timed_out: bool
    duration_seconds: float
    stderr: str
    command: list[str]
    provider_error_type: ProviderErrorType | None = None
    attempts: int = 1

    @property
    def succeeded(self) -> bool:
        """True if the agent exited with code 0 and didn't time out."""
        return self.exit_code == 0 and not self.timed_out

    @property
    def failed(self) -> bool:
        """True if the agent exited with a non-zero code."""
        return self.exit_code is not None and self.exit_code != 0


# ---------------------------------------------------------------------------
# AgentSession — handle to a running agent
# ---------------------------------------------------------------------------

_GRACEFUL_KILL_TIMEOUT = 5


class AgentSession:
    """Handle to a running agent process.

    Returned by :meth:`AgentRunner.start`.  Callers either poll
    :meth:`is_alive` across ticks or block with :meth:`wait`.
    """

    def __init__(
        self,
        child: pexpect.spawn,
        log_writer: CleaningLogWriter,
        spec: AgentSpec,
        start_time: float,
    ) -> None:
        self._child = child
        self._log_writer = log_writer
        self._spec = spec
        self._start_time = start_time
        self._closed = False

    @property
    def pid(self) -> int | None:
        return self._child.pid

    def is_alive(self) -> bool:
        """Check whether the agent process is still running."""
        if self._closed:
            return False
        try:
            return self._child.isalive()
        except (ChildProcessError, OSError):
            return False

    def wait(self, timeout: float | None = None) -> AgentResult:
        """Block until the agent finishes or *timeout* seconds elapse.

        On timeout the process group is killed (SIGTERM → grace → SIGKILL).
        Always closes the PTY and flushes the log writer before returning.
        """
        timed_out = False
        try:
            self._child.expect(pexpect.EOF, timeout=timeout)
        except pexpect.TIMEOUT:
            timed_out = True
            logger.warning(
                "Agent timed out after %ss, terminating",
                timeout,
            )
            self.kill()
        except pexpect.ExceptionPexpect:
            # Covers unexpected pexpect errors (e.g. child already closed)
            pass

        return self._close(timed_out=timed_out)

    def kill(self) -> None:
        """Terminate the agent's process group (SIGTERM → grace → SIGKILL)."""
        if self._closed:
            return
        pid = self._child.pid
        if pid is None:
            return
        try:
            pgid = os.getpgid(pid)
            os.killpg(pgid, signal.SIGTERM)
        except (ProcessLookupError, OSError):
            return

        # Wait briefly for graceful termination
        deadline = time.monotonic() + _GRACEFUL_KILL_TIMEOUT
        while time.monotonic() < deadline:
            if not self.is_alive():
                return
            time.sleep(0.1)

        # Force kill
        logger.warning("Agent did not terminate gracefully, using SIGKILL")
        try:
            pgid = os.getpgid(pid)
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, OSError):
            pass

    def _close(self, *, timed_out: bool) -> AgentResult:
        """Close the PTY, flush the log, return the result."""
        if self._closed:
            return AgentResult(
                exit_code=None,
                timed_out=timed_out,
                duration_seconds=time.monotonic() - self._start_time,
                stderr="session already closed",
                command=self._spec.command,
            )

        self._closed = True
        duration = time.monotonic() - self._start_time

        # Close pexpect child to collect exit status
        try:
            self._child.close(force=True)
        except Exception:  # noqa: BLE001
            pass

        # Flush remaining log output
        try:
            self._log_writer.close()
        except Exception:  # noqa: BLE001
            pass

        exit_code = self._child.exitstatus
        stderr = ""
        # If bash couldn't find the command, exit code is 127
        if exit_code == 127:
            stderr = f"Command not found: {self._spec.command[0]}"
        elif exit_code == 126:
            stderr = f"Permission denied: {self._spec.command[0]}"

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
            command=self._spec.command,
        )


# ---------------------------------------------------------------------------
# AgentRunner — the single entry point
# ---------------------------------------------------------------------------


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
    """The single abstraction for running agent commands.

    Every agent subprocess — coding sessions, review exchange rounds,
    simulated scenario tests — MUST go through this class.

    Two usage modes (same underlying mechanism):

    **Async** — for long-running sessions::

        session = runner.start(spec)
        while session.is_alive():
            # ... do other work ...
        result = session.wait()

    **Sync** — for single-shot execution with optional retry::

        result = runner.run(spec)  # blocks until done
    """

    def start(self, spec: AgentSpec) -> AgentSession:
        """Start an agent in a PTY. Returns a session handle.

        The agent runs in a pexpect PTY with:
        - Cleaned output via CleaningLogWriter → spec.log_path
        - Filtered environment (credentials scrubbed, overrides applied)
        - Process group isolation (for clean termination)

        The caller is responsible for calling :meth:`AgentSession.wait`
        or :meth:`AgentSession.kill` when done.
        """
        spec.output_dir.mkdir(parents=True, exist_ok=True)
        spec.log_path.parent.mkdir(parents=True, exist_ok=True)

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
        logger.info("Agent argv: %s", _format_command_for_log(spec.command))

        log_writer = CleaningLogWriter(spec.log_path)

        shell_cmd = shlex.join(spec.command)
        child = pexpect.spawn(
            "/bin/bash",
            ["-c", shell_cmd],
            cwd=str(spec.working_dir),
            env=env,  # type: ignore[arg-type]  # pexpect accepts dict[str, str]
            logfile=log_writer,
            timeout=None,
        )

        return AgentSession(child, log_writer, spec, time.monotonic())

    def run(self, spec: AgentSpec) -> AgentResult:
        """Start an agent, wait for it, and optionally retry on transient errors.

        This is the sync convenience method.  Equivalent to::

            session = self.start(spec)
            result = session.wait(timeout=spec.timeout_seconds)

        With retry logic when ``spec.retry_policy`` is set.
        """
        attempts = 0
        last_result: AgentResult | None = None
        last_error_type: ProviderErrorType | None = None

        max_attempts = spec.retry_policy.max_attempts if spec.retry_policy else 1

        while True:
            attempts += 1
            logger.info(
                "Agent attempt %d/%d",
                attempts,
                max_attempts,
            )

            session = self.start(spec)
            last_result = session.wait(timeout=spec.timeout_seconds)
            last_error_type = classify_provider_error(
                stdout="",  # stdout flows through PTY, not captured
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
        return AgentResult(
            exit_code=last_result.exit_code,
            timed_out=last_result.timed_out,
            duration_seconds=last_result.duration_seconds,
            stderr=last_result.stderr,
            command=last_result.command,
            provider_error_type=last_error_type,
            attempts=attempts,
        )

    @staticmethod
    def _compute_backoff(spec: AgentSpec, attempts: int) -> float:
        if spec.retry_policy is None:
            return 0.0
        base = spec.retry_policy.initial_backoff_seconds * (2 ** max(0, attempts - 1))
        delay = min(base, spec.retry_policy.max_backoff_seconds)
        if spec.retry_policy.jitter:
            return random.uniform(0, delay)
        return delay
