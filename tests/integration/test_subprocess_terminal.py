"""Integration tests for subprocess terminal backend."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from issue_orchestrator.execution.terminal_subprocess import SubprocessPlugin


def _wait_for_exit(plugin: SubprocessPlugin, session_name: str, timeout_s: float = 5.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if not plugin.session_exists(0, session_name):
            return
        time.sleep(0.05)
    raise AssertionError(f"Session {session_name} did not exit within {timeout_s}s")


def _ensure_worktree_venv(worktree: Path) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    venv_path = repo_root / ".venv"
    if not venv_path.exists():
        pytest.skip("Repo .venv not found; subprocess integration tests require agent-done")
    target = worktree / ".venv"
    if not target.exists():
        target.symlink_to(venv_path)


def test_subprocess_session_writes_completion_and_log(tmp_path, monkeypatch):
    repo_root = tmp_path / "repo"
    worktree = repo_root / "wt"
    worktree.mkdir(parents=True)
    (worktree / ".git").mkdir()
    (worktree / ".issue-orchestrator").mkdir()
    _ensure_worktree_venv(worktree)

    monkeypatch.setenv("ORCHESTRATOR_REPO_ROOT", str(repo_root))
    completion_path = ".issue-orchestrator/completion.json"
    command = (
        "echo 'hello-from-subprocess' && "
        f"export ORCHESTRATOR_COMPLETION_PATH='{completion_path}' && "
        "agent-done completed --implementation 'subprocess test' --problems 'none'"
    )

    plugin = SubprocessPlugin()
    created = plugin.create_session(
        session_id=42,
        command=command,
        working_dir=str(worktree),
        title="Subprocess integration test",
        session_name="issue-42",
    )
    assert created is True

    _wait_for_exit(plugin, "issue-42")

    log_path = worktree / ".issue-orchestrator" / "session.log"
    assert log_path.exists()
    assert "hello-from-subprocess" in log_path.read_text(errors="ignore")

    completion_file = worktree / completion_path
    assert completion_file.exists()


def test_subprocess_send_input_writes_to_log(tmp_path, monkeypatch):
    repo_root = tmp_path / "repo"
    worktree = repo_root / "wt"
    worktree.mkdir(parents=True)
    (worktree / ".git").mkdir()
    (worktree / ".issue-orchestrator").mkdir()
    _ensure_worktree_venv(worktree)

    monkeypatch.setenv("ORCHESTRATOR_REPO_ROOT", str(repo_root))
    command = "read -r line; echo \"INPUT:$line\" >> .issue-orchestrator/session.log"

    plugin = SubprocessPlugin()
    created = plugin.create_session(
        session_id=7,
        command=command,
        working_dir=str(worktree),
        title="Subprocess input test",
        session_name="issue-7",
    )
    assert created is True

    # Give the process a moment to start and wait for input.
    time.sleep(0.1)
    assert plugin.send_to_session(7, "ping", "issue-7") is True

    _wait_for_exit(plugin, "issue-7")

    log_path = worktree / ".issue-orchestrator" / "session.log"
    assert log_path.exists()
    assert "INPUT:ping" in log_path.read_text(errors="ignore")
