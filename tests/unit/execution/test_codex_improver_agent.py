"""The improver runs on Codex non-interactively and read-only (#7490)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from issue_orchestrator.execution.codex_improver_agent import (
    AGENT_WORKSPACE,
    FINAL_MESSAGE_FILE,
    CodexImproverAgent,
)
from issue_orchestrator.ports.command_runner import CommandResult
from issue_orchestrator.ports.improver import HeatSpace

def _space(run_dir: Path) -> HeatSpace:
    """Heat 1's own workdir under ``run_dir``, reading only the staged inputs."""
    workdir = run_dir / "heats" / "h1"
    workdir.mkdir(parents=True, exist_ok=True)
    return HeatSpace(heat=1, run_dir=run_dir, workdir=workdir, evidence=(run_dir / "improver-data",))


def _wd(run_dir: Path) -> Path:
    return run_dir / "heats" / "h1"



class FakeRunner:
    """Runs ``git`` for real (the agent workspace), fakes ``codex``."""

    def __init__(self, result: CommandResult, message: str | None = None) -> None:
        self.result = result
        self.message = message
        self.calls: list[dict] = []

    def run(self, command, *, cwd=None, env=None, timeout_seconds=None, shell=False, newlines=None):  # type: ignore[no-untyped-def]
        if command[0] == "git":
            done = subprocess.run(command, capture_output=True, text=True)
            return CommandResult(done.returncode, done.stdout, done.stderr)
        self.calls.append({"command": command, "cwd": cwd, "env": env, "timeout": timeout_seconds})
        if self.message is not None:
            (Path(cwd) / FINAL_MESSAGE_FILE).write_text(self.message)
        return self.result


def _agent(runner: FakeRunner) -> CodexImproverAgent:
    return CodexImproverAgent(runner=runner, model="gpt-5.6-sol", timeout_seconds=600)


def test_codex_runs_under_the_permission_profile_and_its_last_message_is_the_output(tmp_path: Path) -> None:
    runner = FakeRunner(CommandResult(0, "events", ""), message='{"findings": []}')

    result = _agent(runner).run(prompt="PROMPT", space=_space(tmp_path), toolbox=None)

    [call] = runner.calls
    argv = call["command"]
    codex = argv[argv.index("codex"):]
    exec_at = codex.index("exec")
    profile_args = codex[:exec_at]
    # The orchestrator's profile, never the legacy flag that disables it.
    assert "--sandbox" not in codex
    assert profile_args[profile_args.index("-C") + 1] == str(_wd(tmp_path) / AGENT_WORKSPACE)
    profile = " ".join(profile_args)
    assert '"~/.config/gh" = "deny"' in profile and '"~/.codex" = "deny"' in profile
    assert f'"{tmp_path / "improver-data"}" = "read"' in profile
    # Only its own workspace and the shared evidence: not the run dir itself.
    assert f'"{tmp_path}" = "read"' not in profile
    assert "network = { enabled = false }" in profile
    assert "--add-dir" not in profile_args
    assert codex[codex.index("--model") + 1] == "gpt-5.6-sol"
    assert codex[codex.index("--output-last-message") + 1] == str(_wd(tmp_path) / FINAL_MESSAGE_FILE)
    assert "--ephemeral" in codex and codex[-1] == "PROMPT"
    assert argv[:3] == ["/bin/sh", "-c", 'exec "$@" </dev/null']
    assert (_wd(tmp_path) / AGENT_WORKSPACE / ".git").is_dir()
    assert call["env"]["ISSUE_ORCHESTRATOR_RUN_DIR"] == str(tmp_path)
    assert call["timeout"] == 600
    assert result.final_message == '{"findings": []}'
    assert (_wd(tmp_path) / "improver-agent.log").read_text().startswith("events")


@pytest.mark.parametrize(
    ("result", "message", "detail"),
    [
        (CommandResult(-9, "", "", timed_out=True), None, "timed out"),
        (CommandResult(2, "", "quota exhausted"), None, "exited 2: quota exhausted"),
        (CommandResult(0, "", ""), None, "without a final message"),
        # An empty final message is no output, not a rejected file (r1 F6).
        (CommandResult(0, "", ""), "  \n", "without a final message"),
    ],
)
def test_no_output_says_why(tmp_path: Path, result: CommandResult, message: str | None, detail: str) -> None:
    answer = _agent(FakeRunner(result, message)).run(prompt="P", space=_space(tmp_path), toolbox=None)

    assert answer.final_message is None
    assert detail in answer.detail


def test_no_repository_host_credential_reaches_the_agent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A token could otherwise end up in a finding's free text, and so in a
    filed issue (r1 F3). Codex's own configuration still passes."""
    for name in ("GH_TOKEN", "GITHUB_TOKEN", "ISSUE_ORCH_GITHUB_TOKEN", "PORCHPIN_BOT_TOKEN"):
        monkeypatch.setenv(name, "secret")
    monkeypatch.setenv("CODEX_HOME", "/codex")
    runner = FakeRunner(CommandResult(0, "", ""), message="{}")

    _agent(runner).run(prompt="P", space=_space(tmp_path), toolbox=None)

    env = runner.calls[0]["env"]
    assert "secret" not in env.values()
    assert env["CODEX_HOME"] == "/codex" and "PATH" in env


def test_an_empowered_run_gets_the_toolbox_by_url_and_its_token_by_env(tmp_path: Path) -> None:
    from issue_orchestrator.ports.improver_toolbox import TOOLBOX_TOKEN_ENV, ToolboxEndpoint

    runner = FakeRunner(CommandResult(0, "", ""), message="{}")

    _agent(runner).run(
        prompt="P", space=_space(tmp_path), toolbox=ToolboxEndpoint(url="http://127.0.0.1:5555/mcp", token="run-token-xyz"),
    )

    [call] = runner.calls
    argv = call["command"]
    codex = argv[argv.index("codex"):argv.index("exec")]
    assert 'mcp_servers.improver_toolbox.url="http://127.0.0.1:5555/mcp"' in codex
    assert f'mcp_servers.improver_toolbox.bearer_token_env_var="{TOOLBOX_TOKEN_ENV}"' in codex
    # Without it, ``-a never`` refuses every toolbox call (found by the live escape test).
    assert 'mcp_servers.improver_toolbox.default_tools_approval_mode="approve"' in codex
    assert not any("run-token-xyz" in a for a in argv)
    assert call["env"][TOOLBOX_TOKEN_ENV] == "run-token-xyz"


@pytest.mark.parametrize("empowered", [False, True])
def test_the_operators_codex_config_never_reaches_the_agent(tmp_path: Path, empowered: bool) -> None:
    """r2 F1: an MCP server (or profile) in the operator's config.toml would
    hand the agent tools beyond its boundary, scripted or empowered."""
    from issue_orchestrator.ports.improver_toolbox import ToolboxEndpoint

    runner = FakeRunner(CommandResult(0, "", ""), message="{}")
    toolbox = ToolboxEndpoint(url="http://127.0.0.1:5555/mcp", token="t") if empowered else None

    _agent(runner).run(prompt="P", space=_space(tmp_path), toolbox=toolbox)

    argv = runner.calls[0]["command"]
    assert "--ignore-user-config" in argv[argv.index("exec"):]


def test_the_shell_reads_only_its_run_dir(tmp_path: Path) -> None:
    """r3 F1 / r4 F1: the shared profile reads the whole disk, and a denylist
    cannot bound a shell. The improver's scope is a read BOUNDARY (the disk
    denied, the run dir and the platform's runtime files granted), plus
    explicit denies for the temp areas the shared profile grants. Verified
    live with the run dir under the temp root and under home: the run dir
    reads; a sibling, /var/tmp, /Users/Shared, ~/.claude, /Library and
    /private/var/log do not."""
    import os
    import tempfile

    scope = CodexImproverAgent.scope(_space(tmp_path))

    assert scope.reads_confined is True
    assert "~" in scope.deny_read_files and "/var/tmp" in scope.deny_read_files
    assert os.path.realpath(tempfile.gettempdir()) in scope.deny_read_files
    assert "/tmp" in scope.deny_read_files and os.path.realpath("/tmp") in scope.deny_read_files
    assert tmp_path / "improver-data" in scope.read_roots and tmp_path not in scope.read_roots
    runner = FakeRunner(CommandResult(0, "", ""), message="{}")
    _agent(runner).run(prompt="P", space=_space(tmp_path), toolbox=None)
    profile = " ".join(runner.calls[0]["command"])
    assert '"/" = "deny"' in profile and '":minimal" = "read"' in profile
    assert '"~" = "deny"' in profile and f'"{tmp_path / "improver-data"}" = "read"' in profile
