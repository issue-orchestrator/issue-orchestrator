from __future__ import annotations

from pathlib import Path
import base64
import json

from issue_orchestrator.domain.models import AgentConfig
from issue_orchestrator.events import EventName
from issue_orchestrator.execution.review_exchange_local_loop import _run_exchange_rounds
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
        emit=lambda name, payload: emitted.append((name, payload)),
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
    assert "Fix provider log" in content
    assert "Applied fix" in content
    assert "Thinking" not in content
    assert "Recentactivity" not in content
    assert any(name == EventName.REVIEW_EXCHANGE_ROUND_COMPLETED for name, _ in emitted)
