"""Unit tests for the readiness-assessment launcher (producer side)."""

import shutil
from pathlib import Path

import issue_orchestrator.entrypoints.cli_tools.readiness_launch as readiness_launch
from issue_orchestrator.entrypoints.cli_tools.readiness_launch import (
    available_readiness_clis,
    build_readiness_command,
    build_readiness_prompt,
    readiness_skill_path,
    run_readiness_assessment,
)


def test_readiness_skill_path_resolves_to_packaged_file() -> None:
    """The skill must resolve to a real shipped file (under templates/)."""
    path = readiness_skill_path()
    assert path.is_file(), f"readiness skill not found at {path}"
    assert path.name == "SKILL.md"
    assert path.parent.parts[-3:] == ("templates", "skills", "readiness")


def test_build_readiness_prompt_points_at_skill_and_repo() -> None:
    """The seed prompt names the skill path, the repo, and the safety guard."""
    skill = Path("/pkg/templates/skills/readiness/SKILL.md")
    repo = Path("/work/myrepo")

    prompt = build_readiness_prompt(skill, repo)

    assert str(skill) in prompt
    assert str(repo) in prompt
    assert "installs, probes, or remote writes" in prompt


def test_build_readiness_command_is_positional_prompt() -> None:
    """Interactive launch is `<exe> <prompt>` — no automation flags."""
    assert build_readiness_command("codex", "do the thing") == ["codex", "do the thing"]


def test_available_readiness_clis_filters_and_orders(monkeypatch) -> None:
    """Only CLIs on PATH are returned, in the module's preference order."""
    present = {"claude", "gemini"}
    monkeypatch.setattr(
        readiness_launch.shutil,
        "which",
        lambda exe: f"/usr/bin/{exe}" if exe in present else None,
    )

    # Preference order is claude, codex, gemini → codex dropped (not present).
    assert available_readiness_clis() == ["claude", "gemini"]


def test_copy_skill_to_workspace_creates_temp_copy_inside_repo(tmp_path) -> None:
    """The skill is copied into a temp dir inside the repo (workspace sandbox)."""
    workspace = readiness_launch.copy_skill_to_workspace(tmp_path)

    assert workspace.parent == tmp_path  # temp dir lives inside the repo
    assert workspace.name.startswith(".io-readiness-")
    skill = workspace / "SKILL.md"
    assert skill.is_file()
    assert skill.read_text() == readiness_skill_path().read_text()

    shutil.rmtree(workspace)  # run_readiness_assessment owns cleanup in practice


def test_run_readiness_assessment_returns_exit_code_and_cleans_up(tmp_path) -> None:
    """Returns the runner's int exit code directly and removes the temp copy."""
    repo = tmp_path
    workspace = repo / ".io-readiness-fixed"

    def prepare(_repo):
        workspace.mkdir()
        (workspace / "SKILL.md").write_text("rubric")
        return workspace

    seen = {}

    def fake_runner(command, cwd):
        seen["command"] = command
        seen["cwd"] = cwd
        # The skill copy must exist *during* the session.
        seen["skill_present"] = (workspace / "SKILL.md").is_file()
        return 7  # an int, matching run_interactive's documented contract

    code = run_readiness_assessment(
        "claude", repo, runner=fake_runner, prepare_workspace=prepare
    )

    assert code == 7  # returned directly — no `.returncode` access (was the bug)
    assert seen["cwd"] == str(repo)
    assert seen["command"][0] == "claude"
    assert str(workspace / "SKILL.md") in seen["command"][1]
    assert seen["skill_present"] is True
    assert not workspace.exists()  # temp copy cleaned up when the session ends
