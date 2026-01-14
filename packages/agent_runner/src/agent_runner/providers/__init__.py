"""AI provider implementations for agent-runner.

This module provides command builders for different AI coding agents:
- ClaudeCodeProvider: Anthropic's Claude Code CLI
- CodexProvider: OpenAI's Codex CLI

Each provider knows how to build the correct command-line invocation
for its respective AI agent.

Example:
    from agent_runner.providers import ClaudeCodeProvider, CodexProvider

    # Claude Code
    claude = ClaudeCodeProvider()
    cmd = claude.build_command(
        prompt="Fix the bug in auth.py",
        model="sonnet",
        permission_mode="bypassPermissions",
    )

    # Codex
    codex = CodexProvider()
    cmd = codex.build_command(
        prompt="Fix the bug in auth.py",
        model="gpt-5-codex",
    )
"""

from .base import CLIProvider
from .claude import ClaudeCodeProvider
from .codex import CodexProvider

__all__ = [
    "CLIProvider",
    "ClaudeCodeProvider",
    "CodexProvider",
]
