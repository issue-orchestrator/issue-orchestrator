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

    # Wait for session to complete. session_exists() returns False once the
    # process exits AND the copier thread finishes draining output.
    for _ in range(100):  # Up to 2 seconds
        if not plugin.session_exists(123, "issue-123"):
            break
        time.sleep(0.02)
    else:
        raise AssertionError("Session did not complete within timeout")

    log_path = worktree / ".issue-orchestrator" / "sessions" / "issue-123" / "session.log"
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
        log_path=str(repo_root / "wt" / ".issue-orchestrator" / "sessions" / "issue-9" / "session.log"),
        tab_name="Issue 9",
        is_review=False,
    )

    state_dir = repo_root / ".issue-orchestrator" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    index_path = state_dir / "subprocess_sessions.json"
    index_path.write_text(json.dumps({record.session_name: vars(record)}))

    recovered = _SubprocessRegistry(repo_root).load()
    assert "issue-9" in recovered


def test_process_alive_handles_waitpid_race(tmp_path, monkeypatch):
    repo_root = tmp_path / "repo"
    worktree = repo_root / "wt"
    worktree.mkdir(parents=True)
    monkeypatch.setenv(f"{ENV_PREFIX}REPO_ROOT", str(repo_root))

    class _FlakyChild:
        pid = 4242

        def isalive(self) -> bool:
            raise ChildProcessError("waitpid race")

    plugin = SubprocessPlugin()
    plugin._children["issue-1"] = _FlakyChild()

    assert plugin._process_alive(4242, "issue-1") is False
