"""Run the improver on Codex, non-interactively and sandboxed (#7490).

``codex exec`` under the orchestrator's own Codex permission profile
(:func:`~.agent_runner_providers.sandbox.build_codex_sandbox_argv`, the one
owner of how an agent's reads and writes are bounded): the staged run
directory is READ-only, the agent's commands may write only an empty scratch
workspace inside it, the network is off, and the credential stores every
agent is denied (``~/.config/gh``, ``~/.ssh``, ``~/.codex``, ...) are denied
here too, so no token can be read and carried into a finding and from there
into a filed issue. Codex's legacy ``--sandbox read-only`` cannot express
those denies (and disables the profile), so it is not used. The agent's
environment is also allowlisted. Its final message IS its output;
Codex writes it to a file (``--output-last-message``) and the orchestrator
takes it from there. ``--ephemeral`` keeps the session out of Codex's own
history, and the prompt goes as the last argument, as every Codex launch in
the orchestrator passes it.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path

from ..domain.sandbox_scope import (
    DEFAULT_SANDBOX_DENY_ENV,
    DEFAULT_SANDBOX_DENY_READ_FILES,
    SandboxScope,
)
from ..contracts.improver_run import ImproverAgentChoice, ImproverProvider
from ..ports.command_runner import CommandRunner
from ..ports.improver import ImproverAgentResult, heat_file
from ..ports.improver_toolbox import TOOLBOX_SERVER_NAME, TOOLBOX_TOKEN_ENV, ToolboxEndpoint
from .agent_runner_providers.sandbox import build_codex_sandbox_argv

#: Where Codex leaves the agent's last message, inside the run directory.
FINAL_MESSAGE_FILE = "improver-final-message.txt"
#: The only place the agent's commands may write: an empty Git repository
#: inside the run directory (the permission profile pins a Git worktree).
AGENT_WORKSPACE = "agent-workspace"


#: What the agent's environment may carry: what a process needs to run, and
#: Codex's own configuration and authentication. Nothing else passes, so no
#: repository-host credential (GH_TOKEN, a configured token variable, ...)
#: can reach the agent and, through a finding's free text, a filed issue.
_PASSED_NAMES = frozenset(
    {"PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "TMPDIR", "TERM", "TZ", "OPENAI_API_KEY"}
)
_PASSED_PREFIXES = ("LC_", "CODEX_")


def _beyond_the_run_dir() -> tuple[str, ...]:
    """Denies on top of the read boundary (``reads_confined``, #8001 r3/r4).

    The boundary denies everything outside the run dir except the platform's
    runtime files. The shared profile still grants the temp directories, and
    the platform set reaches the world-writable ``/var/tmp``, where another
    process's file could sit: those are denied explicitly. The run dir keeps
    its own, more specific, grant even when it lies beneath one of them.
    """
    temp_roots = {
        "/tmp", "/var/tmp",
        *(os.path.realpath(p) for p in ("/tmp", "/var/tmp", tempfile.gettempdir())),
    }
    return ("~", *sorted(temp_roots))


def agent_environment(environ: Mapping[str, str]) -> dict[str, str]:
    return {
        name: value
        for name, value in environ.items()
        if name in _PASSED_NAMES or name.startswith(_PASSED_PREFIXES)
    }


class CodexImproverAgent:
    def __init__(self, *, runner: CommandRunner, model: str, timeout_seconds: int) -> None:
        if timeout_seconds <= 0:
            raise ValueError("the improver agent needs a positive timeout")
        self._runner = runner
        self._choice = ImproverAgentChoice(provider=ImproverProvider.CODEX, model=model)
        self._timeout = timeout_seconds

    @property
    def choice(self) -> ImproverAgentChoice:
        return self._choice

    @staticmethod
    def scope(run_dir: Path, heat: int) -> SandboxScope:
        workspace = run_dir / heat_file(AGENT_WORKSPACE, heat)
        return SandboxScope(
            working_directory=workspace,
            read_roots=(workspace, run_dir),
            write_roots=(workspace,),
            egress="model-only",
            deny_env=DEFAULT_SANDBOX_DENY_ENV,
            deny_read_files=(*DEFAULT_SANDBOX_DENY_READ_FILES, *_beyond_the_run_dir()),
            reads_confined=True,
        )

    def argv(self, *, prompt: str, run_dir: Path, toolbox: ToolboxEndpoint | None, heat: int) -> list[str]:
        # stdin is /dev/null: ``codex exec`` appends a PIPED stdin to the
        # prompt, and would wait on one inherited from a runner that never
        # closes it.
        return [
            "/bin/sh", "-c", 'exec "$@" </dev/null', "sh",
            "codex", *build_codex_sandbox_argv(self.scope(run_dir, heat)), *_toolbox_overrides(toolbox), "exec",
            # The operator's config.toml is not the agent's: its MCP servers,
            # profiles and tools would reach past the run's boundary (#8001
            # r2 F1). Authentication still comes from CODEX_HOME.
            "--ignore-user-config",
            "--skip-git-repo-check",
            "--ephemeral",
            "--color", "never",
            "--model", self._choice.model,
            "--output-last-message", str(run_dir / heat_file(FINAL_MESSAGE_FILE, heat)),
            prompt,
        ]

    def run(self, *, prompt: str, run_dir: Path, toolbox: ToolboxEndpoint | None, heat: int) -> ImproverAgentResult:
        # Absolute: Codex runs IN the run dir, so a relative final-message
        # path or sandbox root would resolve beneath it.
        run_dir = run_dir.resolve()
        workspace = run_dir / heat_file(AGENT_WORKSPACE, heat)
        workspace.mkdir()
        initialized = self._runner.run(["git", "init", "-q", str(workspace)], timeout_seconds=60)
        if initialized.returncode:
            raise RuntimeError(f"cannot prepare the agent workspace: {initialized.stderr.strip()}")
        result = self._runner.run(
            self.argv(prompt=prompt, run_dir=run_dir, toolbox=toolbox, heat=heat),
            cwd=run_dir,
            env={
                **agent_environment(os.environ),
                "ISSUE_ORCHESTRATOR_RUN_DIR": str(run_dir),
                **({} if toolbox is None else {TOOLBOX_TOKEN_ENV: toolbox.token}),
            },
            timeout_seconds=self._timeout,
        )
        (run_dir / heat_file("improver-agent.log", heat)).write_text(
            result.stdout + "\n--- stderr ---\n" + result.stderr, encoding="utf-8"
        )
        if result.timed_out:
            return ImproverAgentResult(None, f"codex timed out after {self._timeout}s")
        if result.returncode:
            return ImproverAgentResult(None, f"codex exited {result.returncode}: {result.stderr.strip()[-500:]}")
        message = run_dir / heat_file(FINAL_MESSAGE_FILE, heat)
        text = message.read_text(encoding="utf-8") if message.is_file() else ""
        if not text.strip():
            return ImproverAgentResult(None, "codex finished without a final message")
        return ImproverAgentResult(text, "codex finished")


def _toolbox_overrides(toolbox: ToolboxEndpoint | None) -> list[str]:
    """The empowered toolbox as Codex's one MCP server (the operator's own
    servers are never loaded: ``--ignore-user-config``); Codex reads its
    bearer token from the named environment variable, never the argv."""
    if toolbox is None:
        return []
    server = f"mcp_servers.{TOOLBOX_SERVER_NAME}"
    return [
        "-c", f"{server}.url={json.dumps(toolbox.url)}",
        "-c", f"{server}.bearer_token_env_var={json.dumps(TOOLBOX_TOKEN_ENV)}",
        # Under ``-a never`` an unapproved MCP call is refused outright; the
        # toolbox offers only reads, each checked by its own policy.
        "-c", f"{server}.default_tools_approval_mode={json.dumps('approve')}",
    ]


__all__ = ["CodexImproverAgent", "FINAL_MESSAGE_FILE"]
