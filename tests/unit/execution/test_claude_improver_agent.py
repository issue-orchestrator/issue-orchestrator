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
from issue_orchestrator.ports.improver import HeatSpace

def _space(run_dir: Path) -> HeatSpace:
    """Heat 1's own workdir under ``run_dir``, reading only the staged inputs."""
    workdir = run_dir / "heats" / "h1"
    workdir.mkdir(parents=True, exist_ok=True)
    return HeatSpace(heat=1, run_dir=run_dir, workdir=workdir, evidence=(run_dir / "improver-data",))


def _wd(run_dir: Path) -> Path:
    return run_dir / "heats" / "h1"



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

    result = _agent(runner).run(prompt="THE PROMPT", space=_space(tmp_path), toolbox=None)

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
    assert (_wd(tmp_path) / PROMPT_FILE).read_text() == "THE PROMPT"
    assert call["cwd"] == _wd(tmp_path)
    assert call["env"]["ISSUE_ORCHESTRATOR_RUN_DIR"] == str(tmp_path)
    assert call["timeout"] == 600
    assert result.final_message == '{"findings": []}\n'
    assert (_wd(tmp_path) / AGENT_LOG).read_text().startswith('{"findings": []}')


def test_the_shell_feeds_the_prompt_file_to_claude_on_stdin(tmp_path: Path) -> None:
    """The launch's own shell: ``claude`` replaced by ``cat`` reads the prompt
    from stdin, with the run dir's path never spliced into the script."""
    agent = _agent(FakeRunner(CommandResult(0, "", "")))
    space = _space(tmp_path)
    (space.workdir / PROMPT_FILE).write_text("prompt via stdin")
    argv = agent.argv(space=space, toolbox=None)
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
    answer = _agent(FakeRunner(result)).run(prompt="P", space=_space(tmp_path), toolbox=None)

    assert answer.final_message is None
    assert detail in answer.detail


def test_no_repository_host_credential_reaches_the_agent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("GH_TOKEN", "GITHUB_TOKEN", "ISSUE_ORCH_GITHUB_TOKEN", "OPENAI_API_KEY", "CLAUDECODE"):
        monkeypatch.setenv(name, "secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "oauth")
    runner = FakeRunner(CommandResult(0, "{}", ""))

    _agent(runner).run(prompt="P", space=_space(tmp_path), toolbox=None)

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


def test_an_empowered_run_gets_the_toolbox_as_its_one_mcp_server_never_its_token_on_argv(tmp_path: Path) -> None:
    import json

    from issue_orchestrator.ports.improver_toolbox import TOOLBOX_TOKEN_ENV, ToolboxEndpoint

    runner = FakeRunner(CommandResult(0, "{}", ""))
    endpoint = ToolboxEndpoint(url="http://127.0.0.1:5555/mcp", token="run-token-xyz")

    _agent(runner).run(prompt="P", space=_space(tmp_path), toolbox=endpoint)

    [call] = runner.calls
    argv = call["command"]
    claude = argv[argv.index("claude"):]
    assert not any("run-token-xyz" in a for a in argv)
    config = json.loads(claude[claude.index("--mcp-config") + 1])
    server = config["mcpServers"]["improver_toolbox"]
    assert server == {
        "type": "http",
        "url": "http://127.0.0.1:5555/mcp",
        "headers": {"Authorization": "Bearer ${" + TOOLBOX_TOKEN_ENV + "}"},
    }
    assert claude[claude.index("--allowedTools") + 1] == "mcp__improver_toolbox"
    # Still no shell and no write tool, still confined to the run dir.
    assert claude[claude.index("--tools") + 1] == "Read,Grep,Glob" and "--restricted" in claude
    assert "--strict-mcp-config" in claude
    assert call["env"][TOOLBOX_TOKEN_ENV] == "run-token-xyz"


def test_a_scripted_run_has_no_toolbox(tmp_path: Path) -> None:
    runner = FakeRunner(CommandResult(0, "{}", ""))

    _agent(runner).run(prompt="P", space=_space(tmp_path), toolbox=None)

    argv = runner.calls[0]["command"]
    assert "--mcp-config" not in argv and "--allowedTools" not in argv
    assert "IO_IMPROVER_TOOLBOX_TOKEN" not in runner.calls[0]["env"]


def test_a_heat_runs_in_its_own_workdir_and_reads_only_the_shared_evidence(tmp_path: Path) -> None:
    """r3 F1: a heat that could read another's answer is no independent
    support. Its cwd is its own workdir; --add-dir grants the evidence only."""
    runner = FakeRunner(CommandResult(0, "{}", ""))
    space = HeatSpace(
        heat=2, run_dir=tmp_path, workdir=tmp_path / "heats" / "h2",
        evidence=(tmp_path / "improver-data", tmp_path / "toolbox"),
    )
    space.workdir.mkdir(parents=True)

    _agent(runner).run(prompt="P", space=space, toolbox=None)

    [call] = runner.calls
    claude = call["command"][call["command"].index("claude"):]
    assert call["cwd"] == tmp_path / "heats" / "h2"
    at = claude.index("--add-dir")
    assert claude[at + 1:at + 3] == [str(tmp_path / "improver-data"), str(tmp_path / "toolbox")]
    assert claude[at + 3].startswith("--")
    assert str(tmp_path) not in claude[:at] and str(tmp_path / "heats") not in " ".join(claude[at:])
    assert (space.workdir / "improver-prompt.txt").read_text() == "P"


@pytest.mark.parametrize(
    ("workdir", "evidence"),
    [
        (Path("heats/h1"), (Path("/r/improver-data"),)),             # relative
        (Path("/r/improver-data/h1"), (Path("/r/improver-data"),)),  # inside the evidence
        (Path("/r"), (Path("/r/improver-data"),)),                   # around the evidence
    ],
)
def test_a_heat_space_is_absolute_and_apart_from_its_evidence(workdir: Path, evidence: tuple[Path, ...]) -> None:
    with pytest.raises(ValueError):
        HeatSpace(heat=1, run_dir=Path("/r"), workdir=workdir, evidence=evidence)
