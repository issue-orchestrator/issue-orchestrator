"""Every provider launch carries a bounded instruction, never the prompt (#9012).

The command-building seam every launcher funnels through
(``AgentConfig.get_command_for_prompt``) is exercised for each provider adapter
and for a custom ``command`` template, with a prompt far past the OS argument
limit. The agent's prompt is the persisted file the instruction names, whole.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from issue_orchestrator.domain.launch_prompt_delivery import (
    MAX_LAUNCH_COMMAND_BYTES,
    LaunchCommandTooLong,
    LaunchPromptFile,
)
from issue_orchestrator.domain.models import AgentConfig
from issue_orchestrator.execution.session_output_adapter import FileSystemSessionOutput
from tests.launch_prompt_helpers import delivered_prompt

#: Past Linux's 128 KiB per-argument cap and macOS's 1 MiB ARG_MAX alike.
_RULING = "Standing ruling: runtime stamping replaces the walk checker. "
_LONG_PROMPT = (_RULING * 20_000) + "\n\nWork on issue #9012: don't drop 'quoted' text."


@pytest.fixture(autouse=True)
def isolated_codex_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    user_home = tmp_path / ".test-codex-user"
    user_home.mkdir()
    (user_home / "auth.json").write_text("{}\n", encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(user_home))
    monkeypatch.setenv("ISSUE_ORCHESTRATOR_CODEX_RUNTIME_ROOT", str(tmp_path / ".test-codex-runtime"))


def _agent(provider: str | None, **provider_args: str) -> AgentConfig:
    return AgentConfig(
        prompt_path=Path(".prompts/backend.md"),
        prompt_relative=".prompts/backend.md",
        provider=provider,
        provider_args=dict(provider_args),
    )


def _persisted(tmp_path: Path, text: str) -> LaunchPromptFile:
    return FileSystemSessionOutput().write_session_prompt(tmp_path / "run", text)


@pytest.mark.parametrize("provider", ["claude-code", "codex", "deepseek", None])
def test_a_prompt_past_the_argument_limit_is_delivered_whole_from_its_file(
    provider: str | None, tmp_path: Path
) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    assert len(_LONG_PROMPT.encode()) > 1024 * 1024

    command = _agent(provider).get_command_for_prompt(
        _persisted(tmp_path, _LONG_PROMPT), worktree=worktree, task_kind="tech-lead",
    )

    assert len(command.encode()) <= MAX_LAUNCH_COMMAND_BYTES
    assert _RULING not in command
    assert delivered_prompt(command) == _LONG_PROMPT


def test_the_persisted_prompt_is_recorded_as_the_runs_session_prompt(tmp_path: Path) -> None:
    session_output = FileSystemSessionOutput()

    prompt = session_output.write_session_prompt(tmp_path / "run", _LONG_PROMPT)

    assert prompt.path == tmp_path / "run" / "session-prompt.txt"
    assert (prompt.characters, prompt.lines) == (len(_LONG_PROMPT), len(_LONG_PROMPT.splitlines()))
    manifest = session_output.read_manifest(tmp_path / "run")
    assert manifest is not None and manifest["session_prompt_path"] == str(prompt.path)


_OVERSIZED = "s" * MAX_LAUNCH_COMMAND_BYTES


@pytest.mark.parametrize("agent", [
    _agent("claude-code", system_prompt=_OVERSIZED),
    AgentConfig(prompt_path=Path(".prompts/backend.md"), command=f"claude --append-system-prompt '{_OVERSIZED}' '{{initial_prompt}}'"),
], ids=["provider", "custom-command"])
def test_configuration_past_the_argument_limit_fails_loudly_and_is_never_cut(agent: AgentConfig, tmp_path: Path) -> None:
    """What still rides on the command line is configuration; past the bound the
    launch cannot start, so building it refuses rather than truncating."""
    with pytest.raises(LaunchCommandTooLong):
        agent.get_command_for_prompt(_persisted(tmp_path, "Work on issue #9012."), worktree=tmp_path)
