"""Run the improver on Claude Code, non-interactively and read-only (#8001).

``claude -p --restricted`` with only the read tools:

* ``--restricted`` confines Claude's file tools to the working directory, the
  staged run directory, and ignores the user, project and local settings
  files, so no hook, permission rule or instruction file of the operator's
  reaches the agent (the tournament's arms had to deny ``~/.claude`` and the
  io checkouts one by one; this confines by construction);
* ``--tools Read,Grep,Glob`` is the whole built-in toolset: no shell, no
  writes, no web. An EMPOWERED run adds the read-only toolbox
  (:mod:`.improver_toolbox`) as its one MCP server, served by the
  orchestrator. ``--permission-mode dontAsk`` denies anything else instead of
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

import json
import os
from collections.abc import Mapping
from pathlib import Path

from ..contracts.improver_run import ImproverAgentChoice, ImproverProvider
from ..ports.command_runner import CommandRunner
from ..ports.improver import ImproverAgentResult, heat_file
from ..ports.improver_toolbox import TOOLBOX_SERVER_NAME, TOOLBOX_TOKEN_ENV, ToolboxEndpoint

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

    def argv(self, *, run_dir: Path, toolbox: ToolboxEndpoint | None, heat: int) -> list[str]:
        return [
            # The prompt file is the shell's first argument, so no path is
            # ever spliced into the script text.
            "/bin/sh", "-c", 'prompt="$1"; shift; exec "$@" <"$prompt"', "sh",
            str(run_dir / heat_file(PROMPT_FILE, heat)),
            "claude", "-p",
            "--restricted",
            "--tools", ",".join(READ_TOOLS),
            "--permission-mode", "dontAsk",
            "--strict-mcp-config",
            "--no-session-persistence",
            "--output-format", "text",
            *_toolbox_arguments(toolbox),
            "--model", self._choice.model,
        ]

    def run(self, *, prompt: str, run_dir: Path, toolbox: ToolboxEndpoint | None, heat: int) -> ImproverAgentResult:
        # Absolute: the launch runs IN the run dir, so a relative prompt path
        # would resolve beneath it.
        run_dir = run_dir.resolve()
        (run_dir / heat_file(PROMPT_FILE, heat)).write_text(prompt, encoding="utf-8")
        result = self._runner.run(
            self.argv(run_dir=run_dir, toolbox=toolbox, heat=heat),
            cwd=run_dir,
            env={
                **agent_environment(os.environ),
                "ISSUE_ORCHESTRATOR_RUN_DIR": str(run_dir),
                **({} if toolbox is None else {TOOLBOX_TOKEN_ENV: toolbox.token}),
            },
            timeout_seconds=self._timeout,
        )
        (run_dir / heat_file(AGENT_LOG, heat)).write_text(result.stdout + "\n--- stderr ---\n" + result.stderr, encoding="utf-8")
        if result.timed_out:
            return ImproverAgentResult(None, f"claude timed out after {self._timeout}s")
        if result.returncode:
            detail = (result.stderr.strip() or result.stdout.strip())[-500:]
            return ImproverAgentResult(None, f"claude exited {result.returncode}: {detail}")
        if not result.stdout.strip():
            return ImproverAgentResult(None, "claude finished without a final message")
        return ImproverAgentResult(result.stdout, "claude finished")


def _toolbox_arguments(toolbox: ToolboxEndpoint | None) -> list[str]:
    """The empowered toolbox as Claude's one MCP server. Its bearer token is
    expanded by Claude from its environment, so it is never on a command
    line; ``--allowedTools`` admits the server's tools under ``dontAsk``."""
    if toolbox is None:
        return []
    config = {
        "mcpServers": {
            TOOLBOX_SERVER_NAME: {
                "type": "http",
                "url": toolbox.url,
                "headers": {"Authorization": f"Bearer ${{{TOOLBOX_TOKEN_ENV}}}"},
            }
        }
    }
    return [
        "--mcp-config", json.dumps(config, sort_keys=True),
        "--allowedTools", f"mcp__{TOOLBOX_SERVER_NAME}",
    ]


__all__ = ["AGENT_LOG", "PROMPT_FILE", "READ_TOOLS", "ClaudeImproverAgent"]
