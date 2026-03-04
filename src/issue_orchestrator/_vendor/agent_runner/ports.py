"""AI provider protocol — the only remaining vendored type.

RunSpec, RunResult, RetryPolicy, and ProviderErrorType have been moved to
``execution/agent_runner_types.py`` and ``execution/agent_runner_errors.py``.
"""

from typing import Protocol


class AIProvider(Protocol):
    """Protocol for AI agent providers (Claude, Codex, etc.).

    Providers know how to build command-line invocations for their respective
    AI agents. They handle CLI flag differences between agents.
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
        """Build the command-line invocation for this provider."""
        ...
