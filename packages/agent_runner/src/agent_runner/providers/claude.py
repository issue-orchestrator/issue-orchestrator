"""Claude Code provider implementation.

This module provides the ClaudeCodeProvider class for building
Claude Code CLI invocations.

Claude Code CLI flags:
- -p, --print: Non-interactive mode, prints output and exits
- --model: Model to use (sonnet, opus, haiku, or full model ID)
- --permission-mode: How to handle permission prompts
  - default: Ask for each permission
  - acceptEdits: Auto-accept file edits
  - bypassPermissions: Skip all permission prompts (for automation)
  - plan: Planning mode only
  - dontAsk: Never ask, fail if permission needed
- --append-system-prompt: Add to the system prompt
"""

from .base import CLIProvider


class ClaudeCodeProvider(CLIProvider):
    """Provider for Anthropic's Claude Code CLI.

    Builds command-line invocations for Claude Code with appropriate flags
    for non-interactive, automated execution.

    Example:
        provider = ClaudeCodeProvider()
        cmd = provider.build_command(
            prompt="Fix the bug in auth.py",
            model="sonnet",
            permission_mode="bypassPermissions",
            system_prompt="You are fixing a bug.",
        )
        # Returns: ["claude", "-p", "--model", "sonnet", "--permission-mode",
        #           "bypassPermissions", "--append-system-prompt", "...", "Fix..."]
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

    def build_command(
        self,
        prompt: str,
        model: str | None = None,
        **kwargs: str,
    ) -> list[str]:
        """Build a Claude Code CLI command.

        Args:
            prompt: The task to perform
            model: Model name (haiku, sonnet, opus, or full model ID). None for default.
            **kwargs: Additional options:
                - permission_mode: Permission handling mode (default: bypassPermissions)
                - system_prompt: Additional system prompt text
                - max_turns: Maximum conversation turns

        Returns:
            Command as argv list
        """
        cmd = [self.executable, "-p"]

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

        # The prompt itself (must be last for -p mode)
        cmd.append(prompt)

        return cmd
