"""Registry for validated review-exchange pairs."""

from __future__ import annotations

from typing import FrozenSet, Tuple

# Ordered pairs: (coder_system, reviewer_system)
# Update this list as combinations are validated in practice.
SUPPORTED_MCP_PAIRS: FrozenSet[Tuple[str, str]] = frozenset(
    {
        ("claude-code", "codex"),
        ("codex", "claude-code"),
        ("claude-code", "claude-code"),
        ("codex", "codex"),
    }
)


def supports_mcp_pair(coder_system: str, reviewer_system: str) -> bool:
    """Return True if the pair is validated for MCP exchange."""
    return (coder_system, reviewer_system) in SUPPORTED_MCP_PAIRS
