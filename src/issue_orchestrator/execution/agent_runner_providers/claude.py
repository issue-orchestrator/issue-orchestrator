"""Claude Code provider implementation.

Builds command-line invocations for Anthropic's Claude Code CLI.

Previously in ``_vendor/agent_runner/providers/claude.py``.
"""

from .base import CLIProvider


class ClaudeCodeProvider(CLIProvider):
    """Provider for Anthropic's Claude Code CLI.

    Runs Claude Code as an interactive TUI session. The initial prompt
    (and any follow-up prompts) are delivered via PTY stdin after the
    session starts, so the prompt is NOT included in the command argv.
    """

    # Model name mappings (short names to full IDs if needed)
    MODEL_ALIASES: dict[str, str] = {
        "haiku": "haiku",
        "sonnet": "sonnet",
        "opus": "opus",
    }

    @property
    def name(self) -> str:
        return "claude-code"

    @property
    def executable(self) -> str:
        return "claude"

    @property
    def description(self) -> str:
        return "Anthropic Claude Code CLI"

    @property
    def interactive(self) -> bool:
        return True

    def build_command(
        self,
        prompt: str,
        model: str | None = None,
        **kwargs: str,
    ) -> list[str]:
        """Build a Claude Code CLI command for interactive mode.

        The prompt is NOT included in the command argv — it will be sent
        via PTY stdin after the TUI initializes.

        Args:
            prompt: The task to perform (stored but not added to argv)
            model: Model name (haiku, sonnet, opus, or full model ID). None for default.
            **kwargs: Additional options:
                - permission_mode: Permission handling mode (default: bypassPermissions)
                - system_prompt: Additional system prompt text
                - max_turns: Maximum conversation turns
        """
        cmd = [self.executable]

        # Model (optional - Claude will use default if not specified)
        if model:
            resolved_model = self.MODEL_ALIASES.get(model, model)
            cmd.extend(["--model", resolved_model])

        # Permission mode (default to bypassPermissions for automation)
        permission_mode = kwargs.get("permission_mode", "bypassPermissions")
        cmd.extend(["--permission-mode", permission_mode])

        # Optional system prompt
        system_prompt = kwargs.get("system_prompt")
        if system_prompt:
            cmd.extend(["--append-system-prompt", system_prompt])

        # Optional max turns
        max_turns = kwargs.get("max_turns")
        if max_turns:
            cmd.extend(["--max-turns", str(max_turns)])

        # Prompt is NOT added to argv — it will be sent via PTY stdin
        # after the interactive TUI initializes.

        return cmd
