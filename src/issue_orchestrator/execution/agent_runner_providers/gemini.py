"""Google Gemini CLI provider implementation."""

from .base import CLIProvider


class GeminiProvider(CLIProvider):
    """Provider for Google's Gemini CLI.

    Gemini is launched in non-interactive prompt mode so normal orchestrator
    retries, timeout handling, and provider circuit reporting can wrap it.
    """

    @property
    def name(self) -> str:
        return "gemini"

    @property
    def executable(self) -> str:
        return "gemini"

    @property
    def description(self) -> str:
        return "Google Gemini CLI"

    def build_command(
        self,
        prompt: str,
        model: str | None = None,
        **kwargs: str,
    ) -> list[str]:
        """Build a Gemini CLI command.

        Args:
            prompt: The task to perform.
            model: Gemini model name. If omitted, Gemini CLI selects its default.
            **kwargs: Additional options:
                - approval_mode: Gemini approval mode. Defaults to ``yolo`` for
                  unattended orchestrator sessions.
        """
        cmd = [self.executable]

        if model:
            cmd.extend(["--model", model])

        approval_mode = kwargs.get("approval_mode", "yolo")
        if approval_mode and approval_mode != "default":
            cmd.extend(["--approval-mode", approval_mode])

        cmd.extend(["--prompt", prompt])
        return cmd
