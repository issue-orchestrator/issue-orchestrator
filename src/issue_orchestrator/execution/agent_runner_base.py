"""Base class for agent runners — shared retry, backoff, and kill logic.

Both PtyAgentRunner (pexpect) and SubprocessAgentRunner (Popen) extend
this class.  Subclasses implement ``_execute_once()`` with their specific
process-launch mechanism.
"""

from __future__ import annotations

import logging
import os
import random
import signal
import time
from abc import ABC, abstractmethod

from issue_orchestrator.execution.agent_runner_errors import (
    ProviderErrorType,
    classify_provider_error,
)
from issue_orchestrator.execution.agent_runner_types import (
    AgentResult,
    AgentSpec,
    _format_command_for_log,
)
from issue_orchestrator.infra.shutdown_signals import unblock_shutdown_signals_in_child

logger = logging.getLogger(__name__)

__all__ = ["BaseAgentRunner", "_agent_preexec", "_pty_preexec"]

_GRACEFUL_KILL_TIMEOUT = 5


def _agent_preexec() -> None:
    """Pre-exec setup for subprocess-launched agent child processes.

    - setpgrp: creates a new process group so killpg can cleanly terminate the
      agent and all its children without hitting the parent.
    - SIG_IGN for SIGTTIN/SIGTTOU: the child is in a background process group
      relative to the terminal. If the agent (e.g. Claude CLI) opens /dev/tty
      directly, the kernel would send SIGTTIN/SIGTTOU and stop the entire
      process group. Ignoring these signals causes the read/write to return EIO
      instead of stopping the process — the agent handles this gracefully.

    NOTE: Do NOT use this with pexpect/ptyprocess — use ``_pty_preexec``
    instead.  ``os.setpgrp()`` makes the child a process group leader, which
    causes ptyprocess's ``os.setsid()`` to fail with EPERM.

    Also resets the shutdown-signal mask (see
    ``unblock_shutdown_signals_in_child``): the orchestrator blocks SIGTERM/SIGINT
    process-wide for sender attribution, and the agent — which is later stopped
    via ``killpg(pgid, SIGTERM)`` — must not inherit that block.
    """
    os.setpgrp()
    signal.signal(signal.SIGTTIN, signal.SIG_IGN)
    signal.signal(signal.SIGTTOU, signal.SIG_IGN)
    unblock_shutdown_signals_in_child()


def _pty_preexec() -> None:
    """Pre-exec setup for pexpect/ptyprocess-launched agent child processes.

    Only ignores SIGTTIN/SIGTTOU — does NOT call ``os.setpgrp()`` because
    ptyprocess already calls ``os.setsid()`` which creates a new session and
    process group.  Calling ``setpgrp()`` before ``setsid()`` would make the
    child a process group leader, causing ``setsid()`` to fail with EPERM.

    Also resets the shutdown-signal mask (see
    ``unblock_shutdown_signals_in_child``) so the agent does not inherit the
    orchestrator's process-wide SIGTERM/SIGINT block.
    """
    signal.signal(signal.SIGTTIN, signal.SIG_IGN)
    signal.signal(signal.SIGTTOU, signal.SIG_IGN)
    unblock_shutdown_signals_in_child()


class BaseAgentRunner(ABC):
    """Abstract base for agent runners.

    Provides the retry loop (``run``) and shared utilities.
    Subclasses implement ``_execute_once()`` for the actual subprocess launch.
    """

    def run(self, spec: AgentSpec) -> AgentResult:
        """Start an agent, wait for it, and optionally retry on transient errors.

        Template method: calls ``_execute_once`` in a retry loop with
        exponential backoff when ``spec.retry_policy`` is set.
        """
        attempts = 0
        last_result: AgentResult | None = None
        last_error_type: ProviderErrorType | None = None

        max_attempts = spec.retry_policy.max_attempts if spec.retry_policy else 1

        while True:
            attempts += 1
            logger.info("Agent attempt %d/%d", attempts, max_attempts)

            last_result = self._execute_once(spec, attempt=attempts)
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
        return AgentResult(
            exit_code=last_result.exit_code,
            timed_out=last_result.timed_out,
            duration_seconds=last_result.duration_seconds,
            stderr=last_result.stderr,
            command=last_result.command,
            stdout=last_result.stdout,
            provider_error_type=last_error_type,
            attempts=attempts,
        )

    @abstractmethod
    def _execute_once(self, spec: AgentSpec, *, attempt: int) -> AgentResult:
        """Execute a single attempt.  Subclasses implement this."""

    @staticmethod
    def _compute_backoff(spec: AgentSpec, attempts: int) -> float:
        """Compute exponential backoff with optional jitter."""
        if spec.retry_policy is None:
            return 0.0
        base = spec.retry_policy.initial_backoff_seconds * (2 ** max(0, attempts - 1))
        delay = min(base, spec.retry_policy.max_backoff_seconds)
        if spec.retry_policy.jitter:
            return random.uniform(0, delay)
        return delay

    @staticmethod
    def _terminate_process_group(pid: int) -> None:
        """Terminate a process group: SIGTERM → grace period → SIGKILL."""
        try:
            pgid = os.getpgid(pid)
            os.killpg(pgid, signal.SIGTERM)
        except (ProcessLookupError, OSError):
            return

        deadline = time.monotonic() + _GRACEFUL_KILL_TIMEOUT
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)  # probe
            except (ProcessLookupError, OSError):
                return
            time.sleep(0.1)

        logger.warning("Agent did not terminate gracefully, using SIGKILL")
        try:
            pgid = os.getpgid(pid)
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, OSError):
            pass

    @staticmethod
    def _log_start(spec: AgentSpec, attempt: int, max_attempts: int) -> None:
        """Log agent start — shared by both runners."""
        logger.info(
            "Starting agent: %s in %s (timeout: %ds) attempt=%d/%d",
            spec.command[0],
            spec.working_dir,
            spec.timeout_seconds,
            attempt,
            max_attempts,
        )
        logger.info("Agent argv: %s", _format_command_for_log(spec.command))
