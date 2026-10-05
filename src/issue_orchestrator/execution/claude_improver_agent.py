"""Run the improver on Claude Code, non-interactively and read-only (#8001).

``claude -p --restricted`` with only the read tools:

* ``--restricted`` confines Claude's file tools to the working directory, the
  staged run directory, and ignores the user, project and local settings
  files, so no hook, permission rule or instruction file of the operator's
  reaches the agent (the tournament's arms had to deny ``~/.claude`` and the
  io checkouts one by one; this confines by construction);
* ``--tools Read,Grep,Glob`` is the whole toolset: no shell, no writes, no
  web. ``--permission-mode dontAsk`` denies anything else instead of
  prompting a terminal nobody watches, and ``--strict-mcp-config`` loads no
  MCP server;
* the PROMPT GOES ON STDIN. ``--tools`` (like ``--disallowedTools``) is
  variadic and would swallow a positional prompt as one more tool name;
* ``--no-session-persistence`` keeps the run out of Claude's resumable
  history, and ``--output-format text`` makes stdout the final message.

The environment is allowlisted as for Codex: what a process needs to run and
Claude's own configuration and authentication, never a repository-host
credential (a token in a finding's free text would reach a filed issue).
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from ..contracts.improver_run import ImproverAgentChoice, ImproverProvider
from ..ports.command_runner import CommandRunner
from ..ports.improver import ImproverAgentResult

#: The prompt, inside the run directory, that the launch feeds on stdin.
PROMPT_FILE = "improver-prompt.txt"
#: Claude's stdout (its final message) and stderr.
AGENT_LOG = "improver-agent.log"
#: The agent's whole toolset.
READ_TOOLS = ("Read", "Grep", "Glob")

_PASSED_NAMES = frozenset(
    {"PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "TMPDIR", "TERM", "TZ", "CLAUDE_CONFIG_DIR"}
)
_PASSED_PREFIXES = ("LC_", "ANTHROPIC_", "CLAUDE_CODE_")


def agent_environment(environ: Mapping[str, str]) -> dict[str, str]:
    """What Claude needs to run and authenticate; nothing else passes."""
    return {
        name: value
        for name, value in environ.items()
        if name in _PASSED_NAMES or name.startswith(_PASSED_PREFIXES)
    }


class ClaudeImproverAgent:
    def __init__(self, *, runner: CommandRunner, model: str, timeout_seconds: int) -> None:
        if timeout_seconds <= 0:
            raise ValueError("the improver agent needs a positive timeout")
        self._runner = runner
        self._choice = ImproverAgentChoice(provider=ImproverProvider.CLAUDE, model=model)
        self._timeout = timeout_seconds

    @property
    def choice(self) -> ImproverAgentChoice:
        return self._choice

    def argv(self, *, run_dir: Path) -> list[str]:
        return [
            # The prompt file is the shell's first argument, so no path is
            # ever spliced into the script text.
            "/bin/sh", "-c", 'prompt="$1"; shift; exec "$@" <"$prompt"', "sh",
            str(run_dir / PROMPT_FILE),
            "claude", "-p",
            "--restricted",
            "--tools", ",".join(READ_TOOLS),
            "--permission-mode", "dontAsk",
            "--strict-mcp-config",
            "--no-session-persistence",
            "--output-format", "text",
            "--model", self._choice.model,
        ]

    def run(self, *, prompt: str, run_dir: Path) -> ImproverAgentResult:
        # Absolute: the launch runs IN the run dir, so a relative prompt path
        # would resolve beneath it.
        run_dir = run_dir.resolve()
        (run_dir / PROMPT_FILE).write_text(prompt, encoding="utf-8")
        result = self._runner.run(
            self.argv(run_dir=run_dir),
            cwd=run_dir,
            env={**agent_environment(os.environ), "ISSUE_ORCHESTRATOR_RUN_DIR": str(run_dir)},
            timeout_seconds=self._timeout,
        )
        (run_dir / AGENT_LOG).write_text(result.stdout + "\n--- stderr ---\n" + result.stderr, encoding="utf-8")
        if result.timed_out:
            return ImproverAgentResult(None, f"claude timed out after {self._timeout}s")
        if result.returncode:
            detail = (result.stderr.strip() or result.stdout.strip())[-500:]
            return ImproverAgentResult(None, f"claude exited {result.returncode}: {detail}")
        if not result.stdout.strip():
            return ImproverAgentResult(None, "claude finished without a final message")
        return ImproverAgentResult(result.stdout, "claude finished")


__all__ = ["AGENT_LOG", "PROMPT_FILE", "READ_TOOLS", "ClaudeImproverAgent"]
