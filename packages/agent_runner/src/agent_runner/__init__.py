"""Agent Runner - Provider-agnostic AI agent execution.

This package provides a simple, single-shot execution model for AI coding agents.
It handles subprocess invocation, timeout management, output capture, and
environment isolation.

Key components:
- AgentRunner: Core executor that runs agents as subprocesses
- RunSpec: Specification for an agent run (command, timeout, env, etc.)
- RunResult: Result of an agent run (exit code, output, timing)
- AIProvider: Protocol for building agent-specific commands
- Providers: Claude, Codex implementations

Example usage:
    from agent_runner import AgentRunner, RunSpec
    from agent_runner.providers import ClaudeCodeProvider

    provider = ClaudeCodeProvider()
    command = provider.build_command(
        prompt="Fix the bug in auth.py",
        model="sonnet",
    )

    runner = AgentRunner()
    result = runner.run(RunSpec(
        command=command,
        working_dir=Path("/path/to/repo"),
        timeout_seconds=300,
        output_dir=Path("/path/to/output"),
    ))

    if result.timed_out:
        print("Agent timed out")
    elif result.exit_code == 0:
        print("Agent completed successfully")
"""

from .ports import AIProvider, RunSpec, RunResult
from .runner import AgentRunner

__all__ = [
    "AgentRunner",
    "AIProvider",
    "RunSpec",
    "RunResult",
]

__version__ = "0.1.0"
