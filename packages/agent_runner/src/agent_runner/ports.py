"""Core interfaces and data structures for agent-runner.

This module defines the contracts between agent-runner and its consumers:
- RunSpec: What to run
- RunResult: What happened
- AIProvider: How to build commands for different AI agents
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


@dataclass
class RunSpec:
    """Specification for running an AI agent.

    This is the input to AgentRunner.run(). It describes everything needed
    to invoke an agent subprocess.

    Attributes:
        command: Full command as argv list (e.g., ["claude", "-p", "prompt"])
        working_dir: Directory to run the agent in (typically a git worktree)
        timeout_seconds: Maximum time to wait for the agent to complete
        output_dir: Directory to write stdout/stderr files to
        env_overrides: Environment variables to set (overrides inherited env)
        env_passthrough: List of env var names to pass through from parent process
        env_scrub: List of env var names to explicitly remove (security)
    """

    command: list[str]
    working_dir: Path
    timeout_seconds: int
    output_dir: Path
    env_overrides: dict[str, str] = field(default_factory=dict)
    env_passthrough: list[str] = field(default_factory=list)
    env_scrub: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        """Validate the spec."""
        if not self.command:
            raise ValueError("command cannot be empty")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")


@dataclass
class RunResult:
    """Result of running an AI agent.

    Stdout flows through the parent's PTY to pexpect/CleaningLogWriter for
    ui-session.log. Stderr is captured via PIPE for provider error classification
    (retry logic) and also contains launch failure messages (command not found, etc.).

    Attributes:
        exit_code: Process exit code, or None if timed out
        stderr: Error message from launch failure, or empty string
        duration_seconds: How long the agent ran
        timed_out: True if the agent was killed due to timeout
        command: The command that was executed (for debugging)
    """

    exit_code: int | None
    stderr: str
    duration_seconds: float
    timed_out: bool
    command: list[str]

    @property
    def succeeded(self) -> bool:
        """True if the agent exited with code 0 and didn't time out."""
        return self.exit_code == 0 and not self.timed_out

    @property
    def failed(self) -> bool:
        """True if the agent exited with non-zero code."""
        return self.exit_code is not None and self.exit_code != 0


class AIProvider(Protocol):
    """Protocol for AI agent providers (Claude, Codex, etc.).

    Providers know how to build command-line invocations for their respective
    AI agents. They handle CLI flag differences between agents.

    Example:
        provider = ClaudeCodeProvider()
        command = provider.build_command(
            prompt="Fix the bug",
            model="sonnet",
            permission_mode="bypassPermissions",
        )
        # Returns: ["claude", "-p", "--model", "sonnet", "--permission-mode", ...]
    """

    @property
    def name(self) -> str:
        """Provider identifier (e.g., 'claude-code', 'codex')."""
        ...

    def build_command(
        self,
        prompt: str,
        model: str,
        **kwargs: str,
    ) -> list[str]:
        """Build the command-line invocation for this provider.

        Args:
            prompt: The task/prompt to send to the agent
            model: Model identifier (provider-specific, e.g., 'sonnet', 'gpt-5-codex')
            **kwargs: Provider-specific options (e.g., permission_mode for Claude)

        Returns:
            Command as argv list, ready for subprocess execution
        """
        ...
