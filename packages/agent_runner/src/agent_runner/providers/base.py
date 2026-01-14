"""Base class for CLI-based AI providers.

This module provides a base class that implements common functionality
for AI providers that are invoked via command-line interface.
"""

from abc import ABC, abstractmethod


class CLIProvider(ABC):
    """Base class for CLI-based AI agent providers.

    Subclasses must implement:
    - name: Provider identifier
    - executable: The CLI executable name
    - build_command: Build the full command argv

    Example subclass:
        class MyProvider(CLIProvider):
            @property
            def name(self) -> str:
                return "my-provider"

            @property
            def executable(self) -> str:
                return "my-cli"

            def build_command(self, prompt, model, **kwargs):
                return [self.executable, "--model", model, prompt]
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

    @abstractmethod
    def build_command(
        self,
        prompt: str,
        model: str,
        **kwargs: str,
    ) -> list[str]:
        """Build the command-line invocation for this provider.

        Args:
            prompt: The task/prompt to send to the agent
            model: Model identifier (provider-specific)
            **kwargs: Provider-specific options

        Returns:
            Command as argv list
        """
        ...

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(name={self.name!r})"
