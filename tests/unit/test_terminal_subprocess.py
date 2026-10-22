from __future__ import annotations

import json
import time
import base64
from pathlib import Path

import pytest

from issue_orchestrator.domain.launch_prompt_delivery import MAX_EXEC_ARGUMENT_BYTES, LaunchCommand, LaunchCommandTooLong
from issue_orchestrator.execution.agent_runner import AgentRunner, AgentSpec
from issue_orchestrator.execution.terminal_subprocess import SubprocessPlugin, _SessionRecord, _SubprocessRegistry
from issue_orchestrator.infra.env import ENV_PREFIX


def _read_recording_output(path):
    output_chunks: list[str] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        if not raw_line.strip():
            continue
        event = json.loads(raw_line)
        if event.get("event_type") != "output":
            continue
        data_b64 = event.get("data_b64")
        if isinstance(data_b64, str) and data_b64:
            output_chunks.append(base64.b64decode(data_b64).decode("utf-8", errors="ignore"))
    return "".join(output_chunks)


def _command_with_run_dir(worktree: Path, session_name: str, command: str) -> tuple[str, Path]:
    run_dir = worktree / ".issue-orchestrator" / "sessions" / session_name
    return f"export {ENV_PREFIX}RUN_DIR='{run_dir}' && {command}", run_dir


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
    command, run_dir = _command_with_run_dir(
        worktree,
        "issue-123",
        "printf 'hello from subprocess\\n'",
    )
    created = plugin.create_session(
        session_id=123,
        prepared=plugin.prepare_session(LaunchCommand(command), worktree),
        title="Test session",
        session_name="issue-123",
    )
    assert created is True

    # Bounded poll for process exit.  Subprocess is a real external system,
    # so bounded waits with GIL-yielding pauses are acceptable per test policy.
    deadline = time.monotonic() + 30.0
    while plugin.session_exists(123, "issue-123"):
        assert time.monotonic() < deadline, "subprocess did not exit within 30s"
        time.sleep(0.05)  # yield GIL so watcher thread can drain PTY output

    log_path = run_dir / "terminal-recording.jsonl"
    assert log_path.exists(), f"Log file not created at {log_path}"
    content = log_path.read_text()
    events = [json.loads(line) for line in content.splitlines() if line.strip()]
    assert events[0]["event_type"] == "resize"
    event = next(event for event in events if event.get("event_type") == "output")
    payload = base64.b64decode(event["data_b64"]).decode("utf-8", errors="replace")
    assert "hello from subprocess" in payload, f"Expected decoded output not in recording payload. Content: {content!r}"


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

    # Force the dead-session path deterministically instead of depending on the
    # host PID table, which can legitimately contain the chosen test PID on CI.
    monkeypatch.setattr(plugin, "_process_alive", lambda pid, session_name=None: False)  # noqa: ARG005, SLF001

    assert plugin.session_exists(1, "issue-1") is False
    assert "issue-1" not in plugin._registry.load()  # noqa: SLF001


def test_discover_running_sessions_includes_canonical_session_name(tmp_path, monkeypatch):
    """Registry discovery exposes the persisted terminal id to callers."""
    repo_root = tmp_path / "repo"
    worktree = repo_root / "wt"
    worktree.mkdir(parents=True)
    monkeypatch.setenv(f"{ENV_PREFIX}REPO_ROOT", str(repo_root))

    plugin = SubprocessPlugin()
    run_dir = (
        worktree
        / ".issue-orchestrator"
        / "sessions"
        / "20260221-000000Z__review-456"
    )
    record = _SessionRecord(
        session_name="review-456",
        issue_number=100,
        worktree_path=str(worktree),
        pid=4242,
        started_at="2026-01-01T00:00:00",
        log_path=str(run_dir / "terminal-recording.jsonl"),
        tab_name="Review PR #456",
        is_review=True,
    )
    plugin._registry.upsert(record)  # noqa: SLF001
    monkeypatch.setattr(plugin, "_process_alive", lambda pid, session_name=None: True)  # noqa: ARG005, SLF001

    assert plugin.discover_running_sessions() == [
        {
            "issue_number": 100,
            "tab_name": "Review PR #456",
            "is_review": True,
            "session_name": "review-456",
            "run_dir": str(run_dir),
        }
    ]


def test_session_log_path_uses_issue_orchestrator_run_dir_when_present(tmp_path, monkeypatch):
    repo_root = tmp_path / "repo"
    worktree = repo_root / "wt"
    worktree.mkdir(parents=True)
    monkeypatch.setenv(f"{ENV_PREFIX}REPO_ROOT", str(repo_root))

    plugin = SubprocessPlugin()
    run_dir = worktree / ".issue-orchestrator" / "sessions" / "20260221-000000Z__coding-1"
    command = f"export ISSUE_ORCHESTRATOR_RUN_DIR='{run_dir}' && echo test"

    log_path = plugin._session_log_path(worktree, "issue-123", command)  # noqa: SLF001

    assert log_path == run_dir / "terminal-recording.jsonl"


def test_subprocess_session_auto_accepts_claude_trust_prompt(tmp_path, monkeypatch):
    """Built-in interaction rules can unblock wrapped Claude trust prompts."""
    repo_root = tmp_path / "repo"
    worktree = repo_root / "wt"
    worktree.mkdir(parents=True)
    fake_claude = worktree / ".venv" / "bin" / "claude"
    fake_claude.parent.mkdir(parents=True)
    fake_claude.write_text(
        "#!/bin/sh\n"
        "printf 'Quick safety check: Is this a project you created or one you trust?\\n'\n"
        "printf '❯ 1. Yes, I trust this folder\\n'\n"
        "printf '2. No, exit\\n'\n"
        "read -r response\n"
        "printf 'AUTO-RESPONSE:%s\\n' \"$response\"\n",
        encoding="utf-8",
    )
    fake_claude.chmod(0o755)
    monkeypatch.setenv(f"{ENV_PREFIX}REPO_ROOT", str(repo_root))

    plugin = SubprocessPlugin(session_interactions_enabled=True, worktree_base=repo_root)
    command, run_dir = _command_with_run_dir(
        worktree,
        "issue-123",
        "ISSUE_ORCHESTRATOR_TEST=1 && claude",
    )
    created = plugin.create_session(
        session_id=123,
        prepared=plugin.prepare_session(LaunchCommand(command), worktree),
        title="Trust prompt test",
        session_name="issue-123",
    )
    assert created is True

    log_path = run_dir / "terminal-recording.jsonl"

    deadline = time.monotonic() + 30.0
    while plugin.session_exists(123, "issue-123"):
        assert time.monotonic() < deadline, "subprocess did not exit within 30s"
        time.sleep(0.05)

    assert "AUTO-RESPONSE:" in _read_recording_output(log_path)


def test_subprocess_session_interactions_require_worktree_under_base(tmp_path, monkeypatch):
    repo_root = tmp_path / "repo"
    allowed_base = repo_root / "allowed"
    allowed_base.mkdir(parents=True)
    outside_worktree = repo_root / "outside"
    outside_worktree.mkdir(parents=True)
    monkeypatch.setenv(f"{ENV_PREFIX}REPO_ROOT", str(repo_root))

    plugin = SubprocessPlugin(session_interactions_enabled=True, worktree_base=allowed_base)

    handler = plugin._interaction_handler("claude", "issue-7", outside_worktree)  # noqa: SLF001

    assert handler is None


def test_subprocess_session_interactions_require_configured_worktree_base(tmp_path, monkeypatch, caplog):
    repo_root = tmp_path / "repo"
    worktree = repo_root / "wt"
    worktree.mkdir(parents=True)
    monkeypatch.setenv(f"{ENV_PREFIX}REPO_ROOT", str(repo_root))

    plugin = SubprocessPlugin(session_interactions_enabled=True, worktree_base=None)

    handler = plugin._interaction_handler("claude", "issue-7", worktree)  # noqa: SLF001

    assert handler is None
    assert "worktree_base is not configured" in caplog.text


def test_the_runner_never_execs_an_argument_past_the_os_limit(tmp_path):
    """#9012 review r1: the command reaches the OS as one quoted ``bash -c``
    argument; past the limit the runner refuses before it creates anything."""
    log_path = tmp_path / "run" / "terminal-recording.jsonl"
    spec = AgentSpec(
        command=["/bin/bash", "-lc", "printf " + "x" * MAX_EXEC_ARGUMENT_BYTES],
        working_dir=tmp_path,
        timeout_seconds=10,
        output_dir=log_path.parent,
        log_path=log_path,
    )

    with pytest.raises(LaunchCommandTooLong):
        AgentRunner().start(spec)

    assert not log_path.parent.exists()


def _plugin_over(tmp_path, monkeypatch) -> tuple[SubprocessPlugin, Path]:
    repo_root = tmp_path / "repo"
    worktree = repo_root / "wt"
    worktree.mkdir(parents=True)
    monkeypatch.setenv(f"{ENV_PREFIX}REPO_ROOT", str(repo_root))
    return SubprocessPlugin(), worktree


def _wait_for_exit(plugin: SubprocessPlugin, session_name: str) -> None:
    # A real subprocess is an external system: a bounded wait is acceptable.
    deadline = time.monotonic() + 30.0
    while plugin.session_exists(123, session_name):
        assert time.monotonic() < deadline, "subprocess did not exit within 30s"
        time.sleep(0.05)


def test_the_terminal_refuses_to_prepare_a_command_the_os_cannot_take(tmp_path, monkeypatch):
    """#9012 review r1 F1: the terminal holds the argument it actually execs to
    the limit, after its own cd/PATH/isolation prefix and quoting. Under the
    limit as handed over, this command is past it once its quotes are quoted."""
    plugin, worktree = _plugin_over(tmp_path, monkeypatch)
    command, run_dir = _command_with_run_dir(worktree, "issue-123", "printf %s " + "''" * 20_000)
    assert len(command.encode()) < MAX_EXEC_ARGUMENT_BYTES // 2

    with pytest.raises(LaunchCommandTooLong):
        plugin.prepare_session(LaunchCommand(command), worktree)

    assert plugin.discover_running_sessions() == []
    assert not run_dir.exists()


def test_the_terminals_own_path_growth_is_inside_the_limit(tmp_path, monkeypatch):
    """#9012 review r2 F2: the terminal copies the inherited ``PATH`` into the
    command it execs. A short command under a long, quote-heavy ``PATH`` is past
    the OS limit, and preparing it says so - no fixed reserve stands in for it."""
    plugin, worktree = _plugin_over(tmp_path, monkeypatch)
    command, _run_dir = _command_with_run_dir(worktree, "issue-123", "printf ok")
    monkeypatch.setenv("PATH", "/usr/bin:" + "/opt/it's" * 20_000)
    assert len(command.encode()) < 1024

    with pytest.raises(LaunchCommandTooLong):
        plugin.prepare_session(LaunchCommand(command), worktree)


def test_a_session_runs_exactly_what_was_prepared(tmp_path, monkeypatch):
    """#9012 review r2 F2: the start consumes the prepared command; it does not
    read the terminal's ``PATH`` again, so what was checked is what runs."""
    plugin, worktree = _plugin_over(tmp_path, monkeypatch)
    command, run_dir = _command_with_run_dir(worktree, "issue-123", 'printf "PATH=%s\\n" "$PATH"')
    monkeypatch.setenv("PATH", "/prepared-marker:/usr/bin:/bin")
    prepared = plugin.prepare_session(LaunchCommand(command), worktree)
    monkeypatch.setenv("PATH", "/spawn-marker:/usr/bin:/bin")

    assert plugin.create_session(session_id=123, prepared=prepared, title="t", session_name="issue-123") is True
    _wait_for_exit(plugin, "issue-123")

    output = _read_recording_output(run_dir / "terminal-recording.jsonl")
    assert "/prepared-marker" in output and "/spawn-marker" not in output
