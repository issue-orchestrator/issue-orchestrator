"""Unit tests for doctor guardrail checks."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from issue_orchestrator.infra.doctor.checks import guardrails as guardrail_checks


class _UnusedRunner:
    def run(self, *args, **kwargs):  # pragma: no cover - should never be called
        raise AssertionError("runner should not be invoked in this test")


def test_check_guardrails_prepends_active_venv_bin(monkeypatch, tmp_path: Path) -> None:
    captured_paths: list[str] = []

    def _capture_env(
        worktree_path: Path,
        runner: _UnusedRunner,
        env: dict[str, str],
    ) -> list[object]:
        del worktree_path, runner
        captured_paths.append(env["PATH"])
        return []

    monkeypatch.setattr(guardrail_checks, "_check_git_guards", _capture_env)
    monkeypatch.setattr(guardrail_checks, "_check_gh_guards", _capture_env)
    monkeypatch.setattr(
        guardrail_checks,
        "_check_completion_commands_available",
        _capture_env,
    )
    monkeypatch.setattr(
        guardrail_checks,
        "_check_bypass_tests",
        lambda worktree_path, runner: [],
    )

    guardrail_checks.check_guardrails_in_worktree_impl(tmp_path, _UnusedRunner())

    wrapper_dir = Path(guardrail_checks.__file__).resolve().parents[3] / "scripts"
    active_python_bin = Path(sys.executable).parent
    expected_prefix_entries = [str(wrapper_dir), str(active_python_bin)]
    resolved_python_bin = Path(sys.executable).resolve().parent
    if resolved_python_bin != active_python_bin:
        expected_prefix_entries.append(str(resolved_python_bin))
    expected_prefix = ":".join(expected_prefix_entries) + ":"
    assert captured_paths
    assert all(path_value.startswith(expected_prefix) for path_value in captured_paths)


@pytest.mark.parametrize("command_name", ["coding-done", "reviewer-done"])
@pytest.mark.parametrize("env_mode", ["cc_repo_root", "pyvenv_launcher"])
def test_completion_wrapper_resolves_venv_from_snapshot_context(
    tmp_path: Path,
    command_name: str,
    env_mode: str,
) -> None:
    snapshot_scripts = tmp_path / "launch" / "src" / "issue_orchestrator" / "scripts"
    snapshot_scripts.mkdir(parents=True)
    source_scripts = Path(__file__).resolve().parents[2] / "src" / "issue_orchestrator" / "scripts"
    source_wrapper = source_scripts / command_name
    wrapper = snapshot_scripts / command_name
    shutil.copy2(source_wrapper, wrapper)
    shutil.copy2(source_scripts / "completion-wrapper-lib.sh", snapshot_scripts / "completion-wrapper-lib.sh")

    repo_root = tmp_path / "issue-orchestrator"
    venv_bin = repo_root / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    target = venv_bin / command_name
    target.write_text(
        "#!/bin/sh\n"
        f"printf 'resolved-{command_name}:%s\\n' \"$1\"\n",
        encoding="utf-8",
    )
    target.chmod(0o755)

    env = {
        "PATH": "/usr/bin:/bin",
    }
    if env_mode == "cc_repo_root":
        env["ISSUE_ORCHESTRATOR_CC_REPO_ROOT"] = str(repo_root)
    else:
        env["__PYVENV_LAUNCHER__"] = str(venv_bin / "python")

    result = subprocess.run(
        [str(wrapper), "sentinel"],
        env=env,
        text=True,
        capture_output=True,
        timeout=5,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == f"resolved-{command_name}:sentinel\n"
