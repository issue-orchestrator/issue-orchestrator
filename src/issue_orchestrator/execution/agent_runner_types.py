"""Shared types for all agent runners.

This module is the single source of truth for:
- RetryPolicy: retry configuration for transient failures
- AgentSpec: what to run (command, env, timeouts)
- AgentResult: what happened (exit code, timing, error classification)
- _format_command_for_log: log-friendly command rendering

Both PtyAgentRunner (pexpect) and SubprocessAgentRunner (Popen) import
from here.  No runner implementation lives in this file.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass, field
from pathlib import Path

from issue_orchestrator.execution.agent_runner_errors import ProviderErrorType

__all__ = ["AgentResult", "AgentSpec", "RetryPolicy", "_format_command_for_log"]


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
        log_path: Path for the canonical raw terminal recording.
                  Optional — SubprocessAgentRunner does not use it.
        output_dir: Directory for artifacts (completion.json, etc.).
        env_overrides: Environment variables to set (highest priority).
        env_scrub: Variables to remove from the environment (security).
        env_passthrough: Allowlist mode — only these vars pass through.
        retry_policy: Optional retry policy for transient provider errors.
    """

    command: list[str]
    working_dir: Path
    timeout_seconds: int
    output_dir: Path
    log_path: Path | None = None
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

    The ``stderr`` field captures launch-level errors or subprocess PIPE
    stderr, depending on the runner. For the pexpect runner agent output
    flows through the PTY into the run-scoped terminal recording.
    """

    exit_code: int | None
    timed_out: bool
    duration_seconds: float
    stderr: str
    command: list[str]
    stdout: str = ""
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


def _format_command_for_log(command: list[str], max_arg_length: int = 160) -> str:
    """Render argv for logs while keeping long prompt args bounded."""
    rendered: list[str] = []
    for arg in command:
        text = str(arg)
        if len(text) > max_arg_length:
            text = text[:max_arg_length] + "..."
        rendered.append(shlex.quote(text))
    return " ".join(rendered)
