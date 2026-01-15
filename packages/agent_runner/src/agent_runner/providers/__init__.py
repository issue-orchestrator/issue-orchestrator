"""AI provider implementations for agent-runner.

This module provides command builders for different AI coding agents:
- ClaudeCodeProvider: Anthropic's Claude Code CLI
- CodexProvider: OpenAI's Codex CLI

Each provider knows how to build the correct command-line invocation
for its respective AI agent.

Example:
    from agent_runner.providers import get_provider, list_providers

    # List available providers
    for name in list_providers():
        provider = get_provider(name)
        print(f"{name}: {provider.description} (available: {provider.is_available()})")

    # Get a specific provider
    codex = get_provider("codex")
    cmd = codex.build_command(
        prompt="Fix the bug in auth.py",
        model="o3",
    )
"""

from .base import CLIProvider
from .claude import ClaudeCodeProvider
from .codex import CodexProvider

# Provider registry - maps name to provider class
_PROVIDERS: dict[str, type[CLIProvider]] = {
    "claude-code": ClaudeCodeProvider,
    "codex": CodexProvider,
}


def list_providers() -> list[str]:
    """List available provider names.

    Returns:
        List of provider names (e.g., ["claude-code", "codex"])
    """
    return list(_PROVIDERS.keys())


def get_provider(name: str) -> CLIProvider:
    """Get a provider instance by name.

    Args:
        name: Provider name (e.g., "claude-code", "codex")

    Returns:
        Provider instance

    Raises:
        ValueError: If provider name is not recognized
    """
    if name not in _PROVIDERS:
        available = ", ".join(list_providers())
        raise ValueError(f"Unknown provider: {name!r}. Available: {available}")
    return _PROVIDERS[name]()


def is_valid_provider(name: str) -> bool:
    """Check if a provider name is valid.

    Args:
        name: Provider name to check

    Returns:
        True if provider exists in registry
    """
    return name in _PROVIDERS


__all__ = [
    "CLIProvider",
    "ClaudeCodeProvider",
    "CodexProvider",
    # Registry functions
    "list_providers",
    "get_provider",
    "is_valid_provider",
]
