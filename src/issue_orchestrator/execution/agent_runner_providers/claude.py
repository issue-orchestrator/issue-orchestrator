"""Claude Code provider implementation.

Builds command-line invocations for Anthropic's Claude Code CLI.

Previously in ``_vendor/agent_runner/providers/claude.py``.
"""

from .base import CLIProvider


class ClaudeCodeProvider(CLIProvider):
    """Provider for Anthropic's Claude Code CLI.

    Runs Claude Code as an interactive TUI session. The initial prompt
    is passed as a positional argument (not ``-p``), which starts the TUI
    and immediately begins working while still showing the full interactive
    output. Follow-up prompts can be delivered via PTY stdin.
    """

    # Model name mappings (short names to full IDs if needed)
    MODEL_ALIASES: dict[str, str] = {
        "haiku": "haiku",
        "sonnet": "sonnet",
        "opus": "opus",
    }
    EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")

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

        The prompt is passed as a positional argument (without ``-p``),
        which starts the interactive TUI and immediately begins working.

        Args:
            prompt: The task to perform (passed as positional arg)
            model: Model name (haiku, sonnet, opus, or full model ID). None for default.
            **kwargs: Additional options:
                - permission_mode: Permission handling mode (default: bypassPermissions)
                - effort: Claude effort level (low, medium, high, xhigh, max)
                - reasoning_effort: Alias for effort
                - system_prompt: Additional system prompt text
                - max_turns: Maximum conversation turns
        """
        cmd = [self.executable]

        # Model (optional - Claude will use default if not specified)
        if model:
            resolved_model = self.MODEL_ALIASES.get(model, model)
            cmd.extend(["--model", resolved_model])

        effort = self._resolve_effort(kwargs)
        if effort:
            cmd.extend(["--effort", effort])

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

        # Disable MCP servers — worktree .mcp.json can contain configs
        # (e.g. Playwright) that hang in automated/headless contexts.
        cmd.extend(["--mcp-config", '{"mcpServers":{}}', "--strict-mcp-config"])

        # Verbose mode (more detailed TUI output)
        verbose = kwargs.get("verbose")
        if verbose and str(verbose).lower() not in ("false", "0", "no", ""):
            cmd.append("--verbose")

        # Initial prompt as positional argument — starts TUI working immediately
        # without -p flag, so full interactive output is preserved.
        if prompt:
            cmd.append(prompt)

        return cmd

    @classmethod
    def _resolve_effort(cls, kwargs: dict[str, str]) -> str | None:
        effort = cls._normalize_effort(kwargs.get("effort"))
        reasoning_effort = cls._normalize_effort(kwargs.get("reasoning_effort"))
        if effort and reasoning_effort and effort != reasoning_effort:
            raise ValueError(
                "Claude effort and reasoning_effort must match when both are set"
            )
        normalized = effort or reasoning_effort
        if normalized is None:
            return None
        if normalized not in cls.EFFORT_LEVELS:
            allowed = ", ".join(cls.EFFORT_LEVELS)
            raise ValueError(
                f"Claude effort must be one of {allowed}; got {normalized!r}"
            )
        return normalized

    @staticmethod
    def _normalize_effort(value: str | None) -> str | None:
        if value is None:
            return None
        normalized = str(value).strip().lower()
        return normalized or None
