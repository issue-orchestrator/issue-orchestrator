"""Unit tests for doctor guardrail checks."""

from __future__ import annotations

import sys
from pathlib import Path

from issue_orchestrator.infra.doctor.checks import guardrails as guardrail_checks


class _UnusedRunner:
    def run(self, *args, **kwargs):  # pragma: no cover - should never be called
        raise AssertionError("runner should not be invoked in this test")


def test_check_guardrails_prepends_active_python_bin(monkeypatch, tmp_path: Path) -> None:
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
    active_python_bin = Path(sys.executable).resolve().parent
    expected_prefix = f"{wrapper_dir}:{active_python_bin}:"
    assert captured_paths
    assert all(path_value.startswith(expected_prefix) for path_value in captured_paths)
