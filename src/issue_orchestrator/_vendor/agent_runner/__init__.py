"""Agent Runner - Provider-agnostic AI agent execution.

This package provides a simple, single-shot execution model for AI coding agents.
It handles subprocess invocation, timeout management, output capture, and
environment isolation.

Key components:
- AgentRunner: Core executor that runs agents as subprocesses
- RunSpec: Specification for an agent run (command, timeout, env, etc.)
- RunResult: Result of an agent run (exit code, output, timing)
- Provider registry: list_providers(), get_provider(), is_valid_provider()

Example usage:
    from agent_runner import AgentRunner, RunSpec, get_provider, list_providers

    # List available providers
    print(f"Available: {list_providers()}")

    # Get provider and build command
    provider = get_provider("codex")
    command = provider.build_command(
        prompt="Fix the bug in auth.py",
        model="o3",
    )

    # Run the agent
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

from .ports import AIProvider, RunSpec, RunResult, RetryPolicy
from .errors import ProviderErrorType, classify_provider_error
from .providers import get_provider, is_valid_provider, list_providers
from .runner import AgentRunner

__all__ = [
    # Core
    "AgentRunner",
    "AIProvider",
    "RunSpec",
    "RunResult",
    "RetryPolicy",
    "ProviderErrorType",
    "classify_provider_error",
    # Provider registry
    "list_providers",
    "get_provider",
    "is_valid_provider",
]

__version__ = "0.1.0"
