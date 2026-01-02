"""CommandRunner port for executing local shell commands.

Execution-only: control layer requests command execution; adapters implement it.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class CommandResult:
    """Result of a command execution."""

    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False


class CommandRunner(Protocol):
    """Protocol for running local commands."""

    def run(
        self,
        command: str | list[str],
        *,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        timeout_seconds: int | None = None,
        shell: bool = False,
    ) -> CommandResult:
        """Run a command and return the result."""
        ...


class NullCommandRunner:
    """CommandRunner that always fails (for tests and defaults)."""

    def run(
        self,
        command: str | list[str],
        *,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        timeout_seconds: int | None = None,
        shell: bool = False,
    ) -> CommandResult:
        return CommandResult(
            returncode=1,
            stdout="",
            stderr="NullCommandRunner: command execution not available",
            timed_out=False,
        )
