"""OpenAI Codex provider implementation.

Builds command-line invocations for OpenAI's Codex CLI.

Previously in ``_vendor/agent_runner/providers/codex.py``.
"""

from .base import CLIProvider


class CodexProvider(CLIProvider):
    """Provider for OpenAI's Codex CLI.

    Builds command-line invocations for Codex with appropriate flags
    for non-interactive, automated execution.
    """

    @property
    def name(self) -> str:
        return "codex"

    @property
    def executable(self) -> str:
        return "codex"

    @property
    def description(self) -> str:
        return "OpenAI Codex CLI"

    def build_command(
        self,
        prompt: str,
        model: str | None = None,
        **kwargs: str,
    ) -> list[str]:
        """Build a Codex CLI command.

        Args:
            prompt: The task to perform
            model: Model name (e.g., o3). If None, uses Codex's default.
            **kwargs: Additional options:
                - approval_mode: "full-auto" (default), "yolo", or "default"
                - sandbox: Sandbox policy (read-only, workspace-write, danger-full-access)
                - json_output: Whether to emit JSON events (default: True)
        """
        # Use exec subcommand for non-interactive execution
        cmd = [self.executable, "exec"]

        # Approval mode
        approval_mode = kwargs.get("approval_mode", "full-auto")
        if approval_mode == "yolo":
            cmd.append("--dangerously-bypass-approvals-and-sandbox")
        elif approval_mode == "full-auto":
            cmd.append("--full-auto")
        # else: default mode, no flag needed

        # Model (optional - Codex will use default if not specified)
        if model:
            cmd.extend(["--model", model])

        # Sandbox policy (only if not using yolo which disables sandbox)
        sandbox = kwargs.get("sandbox")
        if sandbox and approval_mode != "yolo":
            cmd.extend(["--sandbox", sandbox])

        # JSON output for structured events (default: True for automation)
        json_output = kwargs.get("json_output", "true").lower() == "true"
        if json_output:
            cmd.append("--json")

        # The prompt itself
        cmd.append(prompt)

        return cmd
