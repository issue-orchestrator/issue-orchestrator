"""Integration tests for subprocess terminal backend."""

from __future__ import annotations

import shlex
import subprocess
import time
from pathlib import Path

import pytest

# Run PTY tests sequentially in one worker to avoid Python 3.14 forkpty warning
# (forkpty() in multi-threaded processes can deadlock)
pytestmark = pytest.mark.xdist_group("pty")

from issue_orchestrator.execution.terminal_subprocess import SubprocessPlugin
from issue_orchestrator.infra.env import ENV_PREFIX


def _wait_for_exit(plugin: SubprocessPlugin, session_name: str, timeout_s: float = 5.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if not plugin.session_exists(0, session_name):
            return
        time.sleep(0.05)
    raise AssertionError(f"Session {session_name} did not exit within {timeout_s}s")


def _wait_for_file(path: Path, timeout_s: float = 5.0) -> None:
    """Wait for a file to exist (atomic check, no content parsing)."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.02)  # Can poll aggressively - just a stat() call
    raise AssertionError(f"File {path} not created within {timeout_s}s")


def _wait_for_content(path: Path, marker: str, timeout_s: float = 5.0) -> None:
    """Wait for specific content to appear in a file."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if path.exists():
            try:
                content = path.read_text(errors="ignore")
                if marker in content:
                    return
            except Exception:
                pass
        time.sleep(0.05)
    raise AssertionError(f"Content '{marker}' not found in {path} within {timeout_s}s")


def _ensure_worktree_venv(worktree: Path) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    venv_path = repo_root / ".venv"
    if not venv_path.exists():
        pytest.skip("Repo .venv not found; subprocess integration tests require agent-done")
    target = worktree / ".venv"
    if not target.exists():
        target.symlink_to(venv_path)


def _init_git_repo_with_origin(worktree: Path, remote_repo: Path) -> None:
    """Initialize a real git repo with a local bare origin remote."""
    subprocess.run(["git", "init", "--bare", str(remote_repo)], capture_output=True, check=True)
    subprocess.run(["git", "init"], cwd=worktree, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=worktree, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=worktree, capture_output=True, check=True)
    (worktree / "README.md").write_text("# Test\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=worktree, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=worktree, capture_output=True, check=True)
    subprocess.run(["git", "remote", "add", "origin", str(remote_repo)], cwd=worktree, capture_output=True, check=True)


def test_subprocess_session_writes_completion_and_log(tmp_path, monkeypatch):
    repo_root = tmp_path / "repo"
    worktree = repo_root / "wt"
    worktree.mkdir(parents=True)
    (worktree / ".issue-orchestrator").mkdir()
    _init_git_repo_with_origin(worktree, tmp_path / "origin.git")
    _ensure_worktree_venv(worktree)

    monkeypatch.setenv(f"{ENV_PREFIX}REPO_ROOT", str(repo_root))
    completion_path = ".issue-orchestrator/sessions/issue-42/completion.json"
    command = (
        "echo 'hello-from-subprocess' && "
        f"export {ENV_PREFIX}COMPLETION_PATH='{completion_path}' && "
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

    log_path = worktree / ".issue-orchestrator" / "sessions" / "issue-42" / "ui-session.log"
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

    monkeypatch.setenv(f"{ENV_PREFIX}REPO_ROOT", str(repo_root))
    monkeypatch.setenv(f"{ENV_PREFIX}SUBPROCESS_ALLOW_STDIN", "1")

    # Use file-based synchronization: touch completes synchronously before
    # shell proceeds to read, so file existence guarantees shell is waiting.
    # pexpect captures all PTY output to session.log automatically.
    ready_file = worktree / ".ready"
    command = f"touch {shlex.quote(str(ready_file))}; read -r line; echo \"INPUT:$line\""

    plugin = SubprocessPlugin()
    created = plugin.create_session(
        session_id=7,
        command=command,
        working_dir=str(worktree),
        title="Subprocess input test",
        session_name="issue-7",
    )
    assert created is True

    # Wait for ready file - guarantees shell has executed touch and is now in read
    _wait_for_file(ready_file)

    assert plugin.send_to_session(7, "ping", "issue-7") is True

    # Wait for the expected output instead of polling session_exists.
    # This avoids a race condition in pexpect where the watcher thread and
    # isalive() can both call waitpid() on the same process.
    log_path = worktree / ".issue-orchestrator" / "sessions" / "issue-7" / "ui-session.log"
    _wait_for_content(log_path, "INPUT:ping")
