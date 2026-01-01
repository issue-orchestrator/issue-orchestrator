"""Hook verification port for startup safety checks."""

from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class HookVerificationResult:
    """Result of hook verification."""
    success: bool
    message: str
    unsupported_agents: list[tuple[str, str]] = field(default_factory=list)  # [(agent_type, reason)]


class HookVerifier(Protocol):
    """Protocol for verifying meta-agent hooks."""

    async def verify(self) -> HookVerificationResult:
        """Verify hooks for all configured meta-agents."""
        ...

    def raise_on_failure(self, result: HookVerificationResult) -> None:
        """Raise an error if verification fails."""
        ...
