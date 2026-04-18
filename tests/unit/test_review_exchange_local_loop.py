from __future__ import annotations

from pathlib import Path
import base64
import json

from issue_orchestrator.control.isolation import GRADLE_USER_HOME_ENV, get_gradle_user_home
from issue_orchestrator.domain.models import AgentConfig
from issue_orchestrator.events import EventName
from issue_orchestrator.execution.review_exchange_local_loop import (
    _build_session_env,
    _run_exchange_rounds,
    _run_phase,
)
from issue_orchestrator.execution.session_output_adapter import FileSystemSessionOutput


class _FakeSession:
    def __init__(self, role: str, completion_path: Path) -> None:
        self.role = role
        self.completion_path = completion_path

    def terminate(self, timeout: float = 30.0) -> None:
        return None


def test_local_loop_writes_clean_ui_session_log(monkeypatch, tmp_path: Path) -> None:
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("Prompt", encoding="utf-8")
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    session_output = FileSystemSessionOutput()
    run = session_output.start_run(worktree, "review-exchange-1", issue_number=4057)
    run_dir = run.run_dir
    exchange_dir = run_dir / "review-exchange"
    exchange_dir.mkdir(parents=True, exist_ok=True)

    coder_agent = AgentConfig(prompt_path=prompt_path, ai_system="claude-code")
    reviewer_agent = AgentConfig(prompt_path=prompt_path, ai_system="claude-code")
    phase_calls: list[str] = []

    def _fake_run_phase(**kwargs):
        role = kwargs["role"]
        phase_calls.append(role)
        completion_path = run_dir / f"completion-{role}.json"
        completion_path.write_text("{}", encoding="utf-8")
        if role == "reviewer":
            data = {
                "outcome": "changes_requested",
                "review_issues": "Fix provider log\n✶ Thinking…\nRecentactivity",
            }
        else:
            data = {
                "outcome": "completed",
                "implementation": "Applied fix\n✶ Thinking…\nRecentactivity",
            }
        return _FakeSession(role, completion_path), data

    monkeypatch.setattr(
        "issue_orchestrator.execution.review_exchange_local_loop._run_phase",
        _fake_run_phase,
    )
    monkeypatch.setattr(
        "issue_orchestrator.execution.review_exchange_local_loop._kill_existing_claude_sessions",
        lambda *args, **kwargs: None,
    )

    emitted: list[tuple[EventName, dict[str, object]]] = []
    transcript_snapshots: dict[str, str] = {}

    def _emit(name: EventName, payload: dict[str, object]) -> None:
        emitted.append((name, payload))
        transcript = run_dir / "review-exchange" / "transcript.log"
        if name == EventName.REVIEW_EXCHANGE_STARTED:
            transcript_snapshots["exchange_started"] = (
                transcript.read_text(encoding="utf-8") if transcript.exists() else ""
            )
        if name == EventName.REVIEW_EXCHANGE_ROUND_STARTED:
            transcript_snapshots["round_started"] = (
                transcript.read_text(encoding="utf-8") if transcript.exists() else ""
            )

    outcome = _run_exchange_rounds(
        worktree_path=worktree,
        run_dir=run_dir,
        exchange_dir=exchange_dir,
        issue_number=4057,
        issue_title="Test",
        session_name="review-exchange-1",
        coder_label="agent:backend",
        reviewer_label="agent:reviewer",
        coder_agent=coder_agent,
        reviewer_agent=reviewer_agent,
        max_rounds=1,
        max_no_progress=2,
        require_validation=False,
        web_port=None,
        emit=_emit,
        session_output=session_output,
    )

    assert phase_calls == ["reviewer", "coder"]
    assert outcome.status == "stopped"
    events = [
        json.loads(line)
        for line in (run_dir / "terminal-recording.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    content = "".join(
        base64.b64decode(event["data_b64"]).decode("utf-8", errors="ignore")
        for event in events
        if event.get("event_type") == "output" and event.get("data_b64")
    )
    transcript = (run_dir / "review-exchange" / "transcript.log").read_text(encoding="utf-8")
    assert content == ""
    assert "Fix provider log" in transcript
    assert "Applied fix" in transcript
    assert "Thinking" not in transcript
    assert "Recentactivity" not in transcript
    assert "round_started" in transcript_snapshots
    assert "role=reviewer section=prompt" in transcript_snapshots["round_started"]
    assert any(name == EventName.REVIEW_EXCHANGE_ROUND_COMPLETED for name, _ in emitted)


def test_run_phase_uses_round_scoped_phase_directory(monkeypatch, tmp_path: Path) -> None:
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("Prompt", encoding="utf-8")
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    run_dir = worktree / ".issue-orchestrator" / "sessions" / "run-1"
    run_dir.mkdir(parents=True)
    exchange_dir = run_dir / "review-exchange"
    exchange_dir.mkdir()

    captured: dict[str, object] = {}

    def _fake_start_pty_session(**kwargs):
        captured["phase_dir"] = kwargs["phase_dir"]
        return _FakeSession("reviewer", run_dir / "completion-reviewer.json")

    monkeypatch.setattr(
        "issue_orchestrator.execution.review_exchange_local_loop._start_pty_session",
        _fake_start_pty_session,
    )
    monkeypatch.setattr(
        "issue_orchestrator.execution.review_exchange_local_loop._wait_for_completion",
        lambda session, timeout_seconds: {"outcome": "approved"},
    )

    _run_phase(
        round_index=2,
        role="reviewer",
        agent=AgentConfig(prompt_path=prompt_path, ai_system="claude-code"),
        worktree_path=worktree,
        run_dir=run_dir,
        exchange_dir=exchange_dir,
        issue_number=4057,
        issue_title="Test",
        session_name="review-exchange-1",
        agent_label="agent:reviewer",
        web_port=None,
        prompt_file_path=prompt_path,
    )

    assert captured["phase_dir"] == exchange_dir / "round-002" / "reviewer"


def test_local_loop_session_env_includes_per_worktree_gradle_home(tmp_path: Path) -> None:
    """Persistent review exchange sessions should use the worktree-local Gradle registry."""
    worktree = tmp_path / "worktree"
    run_dir = worktree / ".issue-orchestrator" / "sessions" / "run-1"

    env = _build_session_env(
        worktree_path=worktree,
        run_dir=run_dir,
        role="reviewer",
        agent_label="agent:reviewer",
        issue_number=4057,
        session_name="review-exchange-1",
        web_port=None,
    )

    assert env[GRADLE_USER_HOME_ENV] == str(get_gradle_user_home(worktree))
