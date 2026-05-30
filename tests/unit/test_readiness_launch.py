"""Unit tests for the readiness-assessment launcher (producer side)."""

from pathlib import Path
from types import SimpleNamespace

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

    # Preference order is claude, codex, gemini, cursor → codex/cursor dropped.
    assert available_readiness_clis() == ["claude", "gemini"]


def test_ensure_readiness_skill_in_repo_copies_into_workspace(tmp_path) -> None:
    """The skill is copied inside the repo so sandboxed agents can read it."""
    dest = readiness_launch.ensure_readiness_skill_in_repo(tmp_path)

    assert dest == tmp_path / ".issue-orchestrator" / "readiness" / "SKILL.md"
    assert dest.is_file()
    # Content matches the packaged source.
    assert dest.read_text() == readiness_skill_path().read_text()
    # Idempotent — a second call does not raise.
    readiness_launch.ensure_readiness_skill_in_repo(tmp_path)


def test_run_readiness_assessment_invokes_runner_interactively() -> None:
    """The launcher copies the skill in, builds the argv, and returns the code."""
    calls: list[tuple] = []

    def fake_runner(command, cwd):
        calls.append((command, cwd))
        return SimpleNamespace(returncode=0)

    repo = Path("/work/myrepo")
    in_repo_skill = repo / ".issue-orchestrator" / "readiness" / "SKILL.md"
    code = run_readiness_assessment(
        "claude",
        repo,
        runner=fake_runner,
        ensure_skill=lambda _repo: in_repo_skill,  # avoid touching disk
    )

    assert code == 0
    assert len(calls) == 1
    command, cwd = calls[0]
    assert command[0] == "claude"
    # The prompt points at the in-repo copy, not the packaged path.
    assert str(in_repo_skill) in command[1]
    assert cwd == str(repo)
