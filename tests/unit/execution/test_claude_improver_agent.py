"""The improver runs on Claude Code non-interactively and read-only (#8001)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from pydantic import ValidationError

from issue_orchestrator.contracts.improver_run import (
    DEFAULT_IMPROVER_AGENT,
    ImproverAgentChoice,
    ImproverProvider,
)
from issue_orchestrator.execution.claude_improver_agent import AGENT_LOG, PROMPT_FILE, ClaudeImproverAgent
from issue_orchestrator.execution.codex_improver_agent import CodexImproverAgent
from issue_orchestrator.execution.improver_agents import improver_agent
from issue_orchestrator.ports.command_runner import CommandResult


class FakeRunner:
    def __init__(self, result: CommandResult) -> None:
        self.result = result
        self.calls: list[dict] = []

    def run(self, command, *, cwd=None, env=None, timeout_seconds=None, shell=False, newlines=None):  # type: ignore[no-untyped-def]
        self.calls.append({"command": command, "cwd": cwd, "env": env, "timeout": timeout_seconds})
        return self.result


def _agent(runner: FakeRunner) -> ClaudeImproverAgent:
    return ClaudeImproverAgent(runner=runner, model="opus", timeout_seconds=600)


def test_claude_runs_restricted_to_the_read_tools_with_the_prompt_on_stdin(tmp_path: Path) -> None:
    runner = FakeRunner(CommandResult(0, '{"findings": []}\n', ""))

    result = _agent(runner).run(prompt="THE PROMPT", run_dir=tmp_path)

    [call] = runner.calls
    argv = call["command"]
    claude = argv[argv.index("claude"):]
    # File tools confined to the run dir, no operator settings, only reads.
    assert "--restricted" in claude
    assert claude[claude.index("--tools") + 1] == "Read,Grep,Glob"
    assert claude[claude.index("--permission-mode") + 1] == "dontAsk"
    assert "--strict-mcp-config" in claude and "--no-session-persistence" in claude
    assert claude[claude.index("--model") + 1] == "opus"
    assert claude[claude.index("--output-format") + 1] == "text"
    # The prompt is never positional: the variadic --tools would swallow it.
    assert "THE PROMPT" not in argv
    assert (tmp_path / PROMPT_FILE).read_text() == "THE PROMPT"
    assert call["cwd"] == tmp_path
    assert call["env"]["ISSUE_ORCHESTRATOR_RUN_DIR"] == str(tmp_path)
    assert call["timeout"] == 600
    assert result.final_message == '{"findings": []}\n'
    assert (tmp_path / AGENT_LOG).read_text().startswith('{"findings": []}')


def test_the_shell_feeds_the_prompt_file_to_claude_on_stdin(tmp_path: Path) -> None:
    """The launch's own shell: ``claude`` replaced by ``cat`` reads the prompt
    from stdin, with the run dir's path never spliced into the script."""
    agent = _agent(FakeRunner(CommandResult(0, "", "")))
    (tmp_path / PROMPT_FILE).write_text("prompt via stdin")
    argv = agent.argv(run_dir=tmp_path)
    shell = argv[: argv.index("claude")]

    done = subprocess.run([*shell, "cat"], capture_output=True, text=True, check=True)

    assert done.stdout == "prompt via stdin"
    assert str(tmp_path) not in shell[2]


@pytest.mark.parametrize(
    ("result", "detail"),
    [
        (CommandResult(-9, "", "", timed_out=True), "claude timed out after 600s"),
        (CommandResult(1, "", "Credit balance is too low"), "claude exited 1: Credit balance is too low"),
        (CommandResult(0, "  \n", ""), "without a final message"),
    ],
)
def test_no_output_says_why(tmp_path: Path, result: CommandResult, detail: str) -> None:
    answer = _agent(FakeRunner(result)).run(prompt="P", run_dir=tmp_path)

    assert answer.final_message is None
    assert detail in answer.detail


def test_no_repository_host_credential_reaches_the_agent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("GH_TOKEN", "GITHUB_TOKEN", "ISSUE_ORCH_GITHUB_TOKEN", "OPENAI_API_KEY", "CLAUDECODE"):
        monkeypatch.setenv(name, "secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "oauth")
    runner = FakeRunner(CommandResult(0, "{}", ""))

    _agent(runner).run(prompt="P", run_dir=tmp_path)

    env = runner.calls[0]["env"]
    assert "secret" not in env.values()
    assert env["ANTHROPIC_API_KEY"] == "anthropic" and env["CLAUDE_CODE_OAUTH_TOKEN"] == "oauth"
    assert "PATH" in env


@pytest.mark.parametrize(
    ("choice", "adapter"),
    [
        (ImproverAgentChoice(provider=ImproverProvider.CLAUDE, model="sonnet"), ClaudeImproverAgent),
        (ImproverAgentChoice(provider=ImproverProvider.CODEX, model="gpt-5.6-sol"), CodexImproverAgent),
    ],
)
def test_each_provider_has_its_adapter_and_reports_its_choice(choice: ImproverAgentChoice, adapter: type) -> None:
    agent = improver_agent(choice, runner=FakeRunner(CommandResult(0, "", "")), timeout_seconds=60)

    assert isinstance(agent, adapter)
    assert agent.choice == choice


def test_the_default_agent_is_the_tournament_winner_claude_opus() -> None:
    assert DEFAULT_IMPROVER_AGENT == ImproverAgentChoice(provider=ImproverProvider.CLAUDE, model="opus")
    assert ImproverAgentChoice.for_provider(ImproverProvider.CODEX).model == "gpt-5.6-sol"
    assert ImproverAgentChoice.for_provider(ImproverProvider.CLAUDE, "sonnet").model == "sonnet"


def test_an_empty_model_is_refused_not_defaulted() -> None:
    """r1 F2: ``--model ""`` must not silently run (and record) the default."""
    with pytest.raises(ValidationError):
        ImproverAgentChoice.for_provider(ImproverProvider.CLAUDE, "")


def test_a_relative_run_dir_still_feeds_the_prompt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """r1 F1: the launch runs IN the run dir, so a relative prompt path would
    resolve beneath it and Claude would never start. The adapter's real shell
    runs, with ``cat`` standing in for ``claude``."""
    (tmp_path / "out" / "run").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)

    class ShellRunner(FakeRunner):
        def run(self, command, *, cwd=None, env=None, timeout_seconds=None, shell=False, newlines=None):  # type: ignore[no-untyped-def]
            super().run(command, cwd=cwd, env=env, timeout_seconds=timeout_seconds)
            argv = [*command[: command.index("claude")], "cat"]
            done = subprocess.run(argv, cwd=cwd, capture_output=True, text=True)
            return CommandResult(done.returncode, done.stdout, done.stderr)

    runner = ShellRunner(CommandResult(0, "", ""))

    answer = _agent(runner).run(prompt="the prompt", run_dir=Path("out/run"))

    assert answer.final_message == "the prompt", answer.detail
    assert runner.calls[0]["env"]["ISSUE_ORCHESTRATOR_RUN_DIR"] == str(tmp_path.resolve() / "out" / "run")
