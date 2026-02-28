from __future__ import annotations

import json
import time

from issue_orchestrator.execution.terminal_subprocess import SubprocessPlugin, _SessionRecord, _SubprocessRegistry
from issue_orchestrator.infra.env import ENV_PREFIX


def test_subprocess_session_writes_log(tmp_path, monkeypatch):
    """Test that subprocess output is captured to the session log file.

    This test verifies that fast-exiting processes (like printf) have their
    output fully captured. The drain logic must be patient enough to wait
    for data to arrive in the PTY buffer after the process exits.
    """
    repo_root = tmp_path / "repo"
    worktree = repo_root / "wt"
    worktree.mkdir(parents=True)
    monkeypatch.setenv(f"{ENV_PREFIX}REPO_ROOT", str(repo_root))

    plugin = SubprocessPlugin()
    created = plugin.create_session(
        session_id=123,
        command="printf 'hello from subprocess\\n'",
        working_dir=str(worktree),
        title="Test session",
        session_name="issue-123",
    )
    assert created is True

    # Bounded poll for process exit.  Subprocess is a real external system,
    # so bounded waits with GIL-yielding pauses are acceptable per test policy.
    deadline = time.monotonic() + 5.0
    while plugin.session_exists(123, "issue-123"):
        assert time.monotonic() < deadline, "subprocess did not exit within 5s"
        time.sleep(0.05)  # yield GIL so watcher thread can drain PTY output

    log_path = worktree / ".issue-orchestrator" / "sessions" / "issue-123" / "ui-session.log"
    assert log_path.exists(), f"Log file not created at {log_path}"
    content = log_path.read_text()
    assert "hello from subprocess" in content, f"Expected output not in log. Content: {content!r}"


def test_subprocess_registry_migrates_legacy_index(tmp_path):
    repo_root = tmp_path / "repo"
    record = _SessionRecord(
        session_name="issue-9",
        issue_number=9,
        worktree_path=str(repo_root / "wt"),
        pid=1234,
        started_at="2026-01-01T00:00:00",
        log_path=str(repo_root / "wt" / ".issue-orchestrator" / "sessions" / "issue-9" / "ui-session.log"),
        tab_name="Issue 9",
        is_review=False,
    )

    state_dir = repo_root / ".issue-orchestrator" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    index_path = state_dir / "subprocess_sessions.json"
    index_path.write_text(json.dumps({record.session_name: vars(record)}))

    recovered = _SubprocessRegistry(repo_root).load()
    assert "issue-9" in recovered


def test_session_exists_returns_false_when_session_not_alive(tmp_path, monkeypatch):
    """session_exists returns False and cleans up when the AgentSession reports dead.

    Replaces the old waitpid race tests: race handling is now encapsulated inside
    AgentSession.is_alive(), so we test the observable behaviour — a dead session
    is removed from the registry and reported as non-existent.
    """
    repo_root = tmp_path / "repo"
    worktree = repo_root / "wt"
    worktree.mkdir(parents=True)
    monkeypatch.setenv(f"{ENV_PREFIX}REPO_ROOT", str(repo_root))

    plugin = SubprocessPlugin()

    # Register a session in the registry so session_exists finds it
    record = _SessionRecord(
        session_name="issue-1",
        issue_number=1,
        worktree_path=str(worktree),
        pid=4242,
        started_at="2026-01-01T00:00:00",
        log_path=str(worktree / "ui-session.log"),
        tab_name="Issue 1",
        is_review=False,
    )
    plugin._registry.upsert(record)  # noqa: SLF001

    # session_exists should report False because the PID doesn't exist
    # (falls through to kill(pid, 0) which raises OSError for nonexistent PID)
    assert plugin.session_exists(1, "issue-1") is False


def test_session_log_path_uses_issue_orchestrator_run_dir_when_present(tmp_path, monkeypatch):
    repo_root = tmp_path / "repo"
    worktree = repo_root / "wt"
    worktree.mkdir(parents=True)
    monkeypatch.setenv(f"{ENV_PREFIX}REPO_ROOT", str(repo_root))

    plugin = SubprocessPlugin()
    run_dir = worktree / ".issue-orchestrator" / "sessions" / "20260221-000000Z__coding-1"
    command = f"export ISSUE_ORCHESTRATOR_RUN_DIR='{run_dir}' && echo test"

    log_path = plugin._session_log_path(worktree, "issue-123", command)  # noqa: SLF001

    assert log_path == run_dir / "ui-session.log"
