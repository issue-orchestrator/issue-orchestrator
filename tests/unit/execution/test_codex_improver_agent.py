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

    result = _agent(runner).run(prompt="PROMPT", run_dir=tmp_path)

    [call] = runner.calls
    argv = call["command"]
    codex = argv[argv.index("codex"):]
    exec_at = codex.index("exec")
    profile_args = codex[:exec_at]
    # The orchestrator's profile, never the legacy flag that disables it.
    assert "--sandbox" not in codex
    assert profile_args[profile_args.index("-C") + 1] == str(tmp_path / AGENT_WORKSPACE)
    profile = " ".join(profile_args)
    assert '"~/.config/gh" = "deny"' in profile and '"~/.codex" = "deny"' in profile
    assert f'"{tmp_path}" = "read"' in profile
    assert "network = { enabled = false }" in profile
    assert "--add-dir" not in profile_args
    assert codex[codex.index("--model") + 1] == "gpt-5.6-sol"
    assert codex[codex.index("--output-last-message") + 1] == str(tmp_path / FINAL_MESSAGE_FILE)
    assert "--ephemeral" in codex and codex[-1] == "PROMPT"
    assert argv[:3] == ["/bin/sh", "-c", 'exec "$@" </dev/null']
    assert (tmp_path / AGENT_WORKSPACE / ".git").is_dir()
    assert call["env"]["ISSUE_ORCHESTRATOR_RUN_DIR"] == str(tmp_path)
    assert call["timeout"] == 600
    assert result.final_message == '{"findings": []}'
    assert (tmp_path / "improver-agent.log").read_text().startswith("events")


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
    answer = _agent(FakeRunner(result, message)).run(prompt="P", run_dir=tmp_path)

    assert answer.final_message is None
    assert detail in answer.detail


def test_no_repository_host_credential_reaches_the_agent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A token could otherwise end up in a finding's free text, and so in a
    filed issue (r1 F3). Codex's own configuration still passes."""
    for name in ("GH_TOKEN", "GITHUB_TOKEN", "ISSUE_ORCH_GITHUB_TOKEN", "PORCHPIN_BOT_TOKEN"):
        monkeypatch.setenv(name, "secret")
    monkeypatch.setenv("CODEX_HOME", "/codex")
    runner = FakeRunner(CommandResult(0, "", ""), message="{}")

    _agent(runner).run(prompt="P", run_dir=tmp_path)

    env = runner.calls[0]["env"]
    assert "secret" not in env.values()
    assert env["CODEX_HOME"] == "/codex" and "PATH" in env
