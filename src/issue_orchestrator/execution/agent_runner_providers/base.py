"""Base class for CLI-based AI providers.

This module provides a base class that implements common functionality
for AI providers that are invoked via command-line interface.

Previously in ``_vendor/agent_runner/providers/base.py``.
"""

import shutil
import subprocess
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from issue_orchestrator.domain.sandbox_scope import SandboxScope


class CLIProvider(ABC):
    """Base class for CLI-based AI agent providers.

    Subclasses must implement:
    - name: Provider identifier
    - executable: The CLI executable name
    - build_command: Build the full command argv

    Subclasses may override:
    - is_authenticated: Check if CLI is authenticated (default: True if available)
    - description: Human-readable description
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Provider identifier (e.g., 'claude-code', 'codex')."""
        ...

    @property
    @abstractmethod
    def executable(self) -> str:
        """The CLI executable name (e.g., 'claude', 'codex')."""
        ...

    @property
    def description(self) -> str:
        """Human-readable description of this provider."""
        return f"{self.name} CLI"

    @property
    def interactive(self) -> bool:
        """Whether this provider runs as an interactive TUI session.

        Interactive providers:
        - Are NOT wrapped in provider_runner (no retry/circuit wrapper)
        - May seed the initial prompt through argv and accept follow-up prompts via PTY
        - Stay alive for follow-up prompts (review feedback, rework)
        """
        return False

    def runs_interactively(self, **kwargs: object) -> bool:
        """Whether this invocation should be treated as an interactive session.

        Most providers have a single execution mode and can rely on
        :attr:`interactive`. Providers with both TUI and one-shot modes can
        inspect provider args and choose per invocation.
        """
        return self.interactive

    def needs_fresh_prompt_process(self, **kwargs: object) -> bool:
        """Whether a completed prompt turn should respawn before follow-up input.

        Interactive providers normally keep one process alive across review
        exchange turns. Providers that expose an interactive UI but cannot
        reliably accept a second orchestrator-driven prompt should override
        this capability.
        """
        return False

    @abstractmethod
    def build_command(
        self,
        prompt: str,
        model: str | None = None,
        *,
        sandbox_scope: "SandboxScope | None" = None,
        **kwargs: str,
    ) -> list[str]:
        """Build the command-line invocation for this provider.

        Args:
            prompt: The task/prompt to send to the agent
            model: Model identifier (provider-specific), None for default
            sandbox_scope: When set, the bounded sandbox the orchestrator
                computed for this session. ``None`` (the default) preserves the
                provider's existing unsandboxed command exactly.
            **kwargs: Provider-specific options (provider_args from YAML)

        Returns:
            Command as argv list
        """
        ...

    def apply_scope(self, scope: "SandboxScope") -> list[str]:
        """Translate a :class:`SandboxScope` into this provider's CLI argv fragment.

        Default: not supported. Providers that can enforce a sandbox override
        this; a provider that cannot yet raises :class:`NotImplementedError`
        rather than silently launching unsandboxed.
        """
        raise NotImplementedError(
            f"{self.name} does not support sandbox-scope translation"
        )

    def is_available(self) -> bool:
        """Check if the CLI executable is installed and in PATH."""
        return shutil.which(self.executable) is not None

    def is_authenticated(self) -> bool:
        """Check if the CLI is authenticated and ready to use.

        Default implementation just checks availability.
        Subclasses can override to perform actual auth checks.
        """
        return self.is_available()

    def check_version(self) -> str | None:
        """Get the CLI version string, if available."""
        if not self.is_available():
            return None
        try:
            result = subprocess.run(
                [self.executable, "--version"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0:
                return result.stdout.strip() or result.stderr.strip()
        except (subprocess.TimeoutExpired, OSError):
            pass
        return None

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(name={self.name!r})"
