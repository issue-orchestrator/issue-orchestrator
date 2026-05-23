"""AI provider implementations for agent-runner.

This module provides command builders for different AI coding agents:
- ClaudeCodeProvider: Anthropic's Claude Code CLI
- CodexProvider: OpenAI's Codex CLI
- GeminiProvider: Google's Gemini CLI

Each provider knows how to build the correct command-line invocation
for its respective AI agent.

Previously in ``_vendor/agent_runner/providers/``.
"""

from .base import CLIProvider
from .claude import ClaudeCodeProvider
from .codex import CodexProvider
from .gemini import GeminiProvider

# Provider registry - maps name to provider class
_PROVIDERS: dict[str, type[CLIProvider]] = {
    "claude-code": ClaudeCodeProvider,
    "codex": CodexProvider,
    "gemini": GeminiProvider,
}


def list_providers() -> list[str]:
    """List available provider names."""
    return list(_PROVIDERS.keys())


def get_provider(name: str) -> CLIProvider:
    """Get a provider instance by name.

    Raises:
        ValueError: If provider name is not recognized
    """
    if name not in _PROVIDERS:
        available = ", ".join(list_providers())
        raise ValueError(f"Unknown provider: {name!r}. Available: {available}")
    return _PROVIDERS[name]()


def is_valid_provider(name: str) -> bool:
    """Check if a provider name is valid."""
    return name in _PROVIDERS


__all__ = [
    "CLIProvider",
    "ClaudeCodeProvider",
    "CodexProvider",
    "GeminiProvider",
    "list_providers",
    "get_provider",
    "is_valid_provider",
]
