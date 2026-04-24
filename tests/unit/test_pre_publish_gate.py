from pathlib import Path
from unittest.mock import Mock

from issue_orchestrator.control.pre_publish_gate import PrePublishGate
from issue_orchestrator.ports.command_runner import CommandResult


def test_pre_publish_gate_runs_worktree_pre_push_hook(tmp_path: Path) -> None:
    hook_path = tmp_path / ".git" / "hooks" / "pre-push"
    hook_path.parent.mkdir(parents=True)
    hook_path.write_text("#!/bin/bash\nexit 0\n")
    hook_path.chmod(0o755)

    runner = Mock()
    runner.run.side_effect = [
        CommandResult(returncode=0, stdout=str(hook_path), stderr=""),
        CommandResult(returncode=0, stdout="", stderr=""),
        CommandResult(returncode=0, stdout="deadbeef", stderr=""),
    ]

    gate = PrePublishGate(runner)
    result = gate.check(tmp_path)

    assert result.allowed is True
    assert result.ran is True
    assert result.command == str(hook_path)
    hook_call = runner.run.call_args_list[1]
    assert hook_call.args[0] == [str(hook_path), "origin", "origin"]
    assert hook_call.kwargs["cwd"] == tmp_path


def test_pre_publish_gate_skips_when_hook_missing(tmp_path: Path) -> None:
    runner = Mock()
    runner.run.side_effect = [
        CommandResult(returncode=1, stdout="", stderr="not a git repository"),
        CommandResult(returncode=1, stdout="", stderr="not a git repository"),
    ]

    gate = PrePublishGate(runner)
    result = gate.check(tmp_path)

    assert result.allowed is True
    assert result.ran is False
    assert "No pre-push hook" in result.reason


def test_pre_publish_gate_surfaces_hook_failure_summary(tmp_path: Path) -> None:
    hook_path = tmp_path / ".git" / "hooks" / "pre-push"
    hook_path.parent.mkdir(parents=True)
    hook_path.write_text("#!/bin/bash\nexit 1\n")
    hook_path.chmod(0o755)

    runner = Mock()
    runner.run.side_effect = [
        CommandResult(returncode=0, stdout=str(hook_path), stderr=""),
        CommandResult(
            returncode=1,
            stdout="\n╔══\n[orchestrator] Running project pre-push hook...\nERROR: Test-skipping patterns detected\n",
            stderr="",
        ),
        CommandResult(returncode=0, stdout="deadbeef", stderr=""),
    ]

    gate = PrePublishGate(runner)
    result = gate.check(tmp_path)

    assert result.allowed is False
    assert result.ran is True
    assert result.reason == "ERROR: Test-skipping patterns detected"
