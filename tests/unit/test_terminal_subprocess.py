from __future__ import annotations

import time
from pathlib import Path

import json

from issue_orchestrator.execution.terminal_subprocess import SubprocessPlugin, _SessionRecord, _SubprocessRegistry


def test_subprocess_session_writes_log(tmp_path, monkeypatch):
    repo_root = tmp_path / "repo"
    worktree = repo_root / "wt"
    worktree.mkdir(parents=True)
    monkeypatch.setenv("ORCHESTRATOR_REPO_ROOT", str(repo_root))

    plugin = SubprocessPlugin()
    created = plugin.create_session(
        session_id=123,
        command="printf 'hello from subprocess\\n'",
        working_dir=str(worktree),
        title="Test session",
        session_name="issue-123",
    )
    assert created is True

    # Allow the subprocess to exit and flush logs.
    for _ in range(50):
        if not plugin.session_exists(123, "issue-123"):
            break
        time.sleep(0.02)

    log_path = worktree / ".issue-orchestrator" / "sessions" / "issue-123" / "session.log"
    assert log_path.exists()
    assert "hello from subprocess" in log_path.read_text()


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
