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
            model: Model name (e.g., gpt-5.3-codex). If None, uses Codex's default.
            **kwargs: Additional options:
                - approval_mode: "full-auto" (default), "yolo", or "default"
                - sandbox: Sandbox policy (read-only, workspace-write, danger-full-access)
                - reasoning_effort: Codex reasoning effort (low, medium, high, xhigh)
                - model_reasoning_effort: Alias for reasoning_effort
                - json_output: Emit ``--json`` (codex's structured event stream)
                  instead of the default terminal UI. Defaults to **False**.

                  Most production paths (persistent-session review-exchange,
                  one-shot agent runs) hand off via a response file or HTTP
                  callback — nothing in this codebase parses codex stdout
                  for protocol data. The PTY-backed terminal-recording
                  pipeline is the consumer that matters, and it captures
                  the agent's terminal UI for replay in the timeline
                  viewer. With ``--json`` set, the recording becomes a raw
                  JSONL stream that the terminal renderer concatenates as
                  unstyled text — exactly what the user saw on tixmeup
                  #362's reviewer log. Defaulting off makes the recording
                  match what a human running ``codex exec`` interactively
                  would see. Automation that genuinely wants the JSON
                  event stream can opt in per-agent via
                  ``provider_args: { json_output: "true" }``.
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

        # Reasoning effort is configured through Codex config overrides.
        reasoning_effort = kwargs.get("reasoning_effort")
        if reasoning_effort is None:
            reasoning_effort = kwargs.get("model_reasoning_effort")
        if reasoning_effort:
            cmd.extend(["-c", f'model_reasoning_effort="{reasoning_effort}"'])

        # Sandbox policy (only if not using yolo which disables sandbox)
        sandbox = kwargs.get("sandbox")
        if sandbox and approval_mode != "yolo":
            cmd.extend(["--sandbox", sandbox])

        # JSON-event-stream mode is opt-in. The default leaves codex in
        # terminal-UI mode so the PTY recording captures what a human
        # would see at the terminal — the timeline viewer's terminal
        # renderer can then play it back faithfully. ``--json`` flips
        # codex to a structured JSONL stream on stdout that the
        # terminal renderer cannot render without a structured-event
        # parser. See the docstring above for the full rationale.
        json_output = kwargs.get("json_output", "false").lower() == "true"
        if json_output:
            cmd.append("--json")

        # The prompt itself
        cmd.append(prompt)

        return cmd
