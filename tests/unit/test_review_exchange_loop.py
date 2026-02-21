"""Tests for review exchange loop behaviors."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from issue_orchestrator.control.review_exchange_loop import (
    _run_agent_round,
    _resolve_provider,
    run_review_exchange_loop,
)
from issue_orchestrator.domain.models import AgentConfig


def test_agent_round_returns_error_on_nonzero_exit(tmp_path: Path) -> None:
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("Prompt")

    worktree = tmp_path / "worktree"
    worktree.mkdir()
    run_dir = worktree / ".issue-orchestrator" / "sessions" / "run-1"
    run_dir.mkdir(parents=True)
    exchange_dir = run_dir / "review-exchange"
    exchange_dir.mkdir()

    agent = AgentConfig(prompt_path=prompt_path, ai_system="claude-code")

    class DummyRunner:
        def run(self, _spec):
            return SimpleNamespace(
                succeeded=False,
                exit_code=2,
                timed_out=False,
                stdout="",
                stderr="boom",
            )

    response = _run_agent_round(
        runner=DummyRunner(),
        worktree_path=worktree,
        run_dir=run_dir,
        exchange_dir=exchange_dir,
        round_index=1,
        issue_number=1,
        issue_title="Test",
        session_name="review-exchange-1",
        agent=agent,
        role="reviewer",
        agent_label="agent:reviewer",
        prompt_text="Review prompt",
        web_port=None,
    )

    assert response.response_type == "error"
    assert "exit_code=2" in response.response_text


def test_resolve_provider_uses_ai_system_when_provider_missing(tmp_path: Path) -> None:
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("Prompt")
    agent = AgentConfig(prompt_path=prompt_path, ai_system="claude-code")
    assert _resolve_provider(agent) == "claude-code"


def test_run_agent_round_prefers_provider_mode_when_ai_system_is_provider(tmp_path: Path) -> None:
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("Prompt")

    worktree = tmp_path / "worktree"
    worktree.mkdir()
    run_dir = worktree / ".issue-orchestrator" / "sessions" / "run-1"
    run_dir.mkdir(parents=True)
    exchange_dir = run_dir / "review-exchange"
    exchange_dir.mkdir()

    agent = AgentConfig(prompt_path=prompt_path, ai_system="claude-code")
    seen_command: list[str] = []

    class DummyRunner:
        def run(self, spec):
            nonlocal seen_command
            seen_command = spec.command
            return SimpleNamespace(
                succeeded=True,
                exit_code=0,
                timed_out=False,
                stdout='{"response_type":"ok","response_text":"looks good"}\n',
                stderr="",
            )

    response = _run_agent_round(
        runner=DummyRunner(),
        worktree_path=worktree,
        run_dir=run_dir,
        exchange_dir=exchange_dir,
        round_index=1,
        issue_number=1,
        issue_title="Test",
        session_name="review-exchange-1",
        agent=agent,
        role="reviewer",
        agent_label="agent:reviewer",
        prompt_text="Review prompt",
        web_port=None,
    )

    assert response.response_type == "ok"
    assert "-p" in seen_command


def test_review_exchange_run_manifest_includes_claude_log_dir(tmp_path: Path, monkeypatch) -> None:
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("Prompt")
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    run_dir = worktree / ".issue-orchestrator" / "sessions" / "run-exchange"
    run_dir.mkdir(parents=True)

    session_output = MagicMock()
    session_output.start_run.return_value = SimpleNamespace(run_dir=run_dir, run_id="run-exchange")

    class DummyRunner:
        def run(self, _spec):
            return SimpleNamespace(
                succeeded=True,
                exit_code=0,
                timed_out=False,
                stdout='{"response_type":"ok","response_text":"looks good"}\n',
                stderr="",
            )

    monkeypatch.setattr("issue_orchestrator.control.review_exchange_loop.AgentRunner", DummyRunner)

    coder_agent = AgentConfig(prompt_path=prompt_path, ai_system="claude-code")
    reviewer_agent = AgentConfig(prompt_path=prompt_path, ai_system="claude-code")

    outcome = run_review_exchange_loop(
        session_output=session_output,
        worktree_path=worktree,
        issue_number=4057,
        issue_title="Test",
        coder_label="agent:backend",
        reviewer_label="agent:reviewer",
        coder_agent=coder_agent,
        reviewer_agent=reviewer_agent,
        max_rounds=1,
        max_no_progress=1,
        require_validation=False,
        web_port=None,
    )

    assert outcome.status == "ok"
    kwargs = session_output.start_run.call_args.kwargs
    assert kwargs["claude_log_dir"].endswith("-worktree")
    assert kwargs["orchestrator_log"].endswith(".issue-orchestrator/state/logs/orchestrator.log")


def test_review_exchange_retries_coder_when_completion_artifact_missing(tmp_path: Path, monkeypatch) -> None:
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("Prompt")
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    run_dir = worktree / ".issue-orchestrator" / "sessions" / "run-exchange"
    run_dir.mkdir(parents=True)

    session_output = MagicMock()
    session_output.start_run.return_value = SimpleNamespace(run_dir=run_dir, run_id="run-exchange")

    class DummyRunner:
        def __init__(self) -> None:
            self.coder_calls = 0

        def run(self, spec):
            role = Path(spec.output_dir).name
            if role == "reviewer":
                return SimpleNamespace(
                    succeeded=True,
                    exit_code=0,
                    timed_out=False,
                    stdout='{"response_type":"changes_requested","getting_closer":true,"response_text":"fix"}\n',
                    stderr="",
                )
            self.coder_calls += 1
            if self.coder_calls == 2:
                (run_dir / "completion-coder.json").write_text("{}", encoding="utf-8")
            return SimpleNamespace(
                succeeded=True,
                exit_code=0,
                timed_out=False,
                stdout='{"response_type":"ok","response_text":"updated"}\n',
                stderr="",
            )

    runner = DummyRunner()
    monkeypatch.setattr(
        "issue_orchestrator.control.review_exchange_loop.AgentRunner", lambda: runner
    )

    coder_agent = AgentConfig(prompt_path=prompt_path, ai_system="claude-code")
    reviewer_agent = AgentConfig(prompt_path=prompt_path, ai_system="claude-code")

    outcome = run_review_exchange_loop(
        session_output=session_output,
        worktree_path=worktree,
        issue_number=4057,
        issue_title="Test",
        coder_label="agent:backend",
        reviewer_label="agent:reviewer",
        coder_agent=coder_agent,
        reviewer_agent=reviewer_agent,
        max_rounds=1,
        max_no_progress=1,
        require_validation=False,
        web_port=None,
    )

    assert runner.coder_calls == 2
    assert outcome.status == "stopped"
    assert outcome.reason == "max_rounds_exceeded"


def test_review_exchange_fails_after_protocol_retries_exhausted(tmp_path: Path, monkeypatch) -> None:
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("Prompt")
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    run_dir = worktree / ".issue-orchestrator" / "sessions" / "run-exchange"
    run_dir.mkdir(parents=True)

    session_output = MagicMock()
    session_output.start_run.return_value = SimpleNamespace(run_dir=run_dir, run_id="run-exchange")

    class DummyRunner:
        def __init__(self) -> None:
            self.coder_calls = 0

        def run(self, spec):
            role = Path(spec.output_dir).name
            if role == "reviewer":
                return SimpleNamespace(
                    succeeded=True,
                    exit_code=0,
                    timed_out=False,
                    stdout='{"response_type":"changes_requested","getting_closer":true,"response_text":"fix"}\n',
                    stderr="",
                )
            self.coder_calls += 1
            return SimpleNamespace(
                succeeded=True,
                exit_code=0,
                timed_out=False,
                stdout='{"response_type":"ok","response_text":"updated"}\n',
                stderr="",
            )

    runner = DummyRunner()
    monkeypatch.setattr(
        "issue_orchestrator.control.review_exchange_loop.AgentRunner", lambda: runner
    )

    coder_agent = AgentConfig(prompt_path=prompt_path, ai_system="claude-code")
    reviewer_agent = AgentConfig(prompt_path=prompt_path, ai_system="claude-code")

    outcome = run_review_exchange_loop(
        session_output=session_output,
        worktree_path=worktree,
        issue_number=4057,
        issue_title="Test",
        coder_label="agent:backend",
        reviewer_label="agent:reviewer",
        coder_agent=coder_agent,
        reviewer_agent=reviewer_agent,
        max_rounds=1,
        max_no_progress=1,
        require_validation=False,
        web_port=None,
    )

    assert runner.coder_calls == 3
    assert outcome.status == "error"
    assert outcome.reason == "coder_protocol_violation"


def test_review_exchange_retries_when_validation_artifact_missing(tmp_path: Path, monkeypatch) -> None:
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("Prompt")
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    run_dir = worktree / ".issue-orchestrator" / "sessions" / "run-exchange"
    run_dir.mkdir(parents=True)

    session_output = MagicMock()
    session_output.start_run.return_value = SimpleNamespace(run_dir=run_dir, run_id="run-exchange")

    class DummyRunner:
        def __init__(self) -> None:
            self.coder_calls = 0

        def run(self, spec):
            role = Path(spec.output_dir).name
            if role == "reviewer":
                return SimpleNamespace(
                    succeeded=True,
                    exit_code=0,
                    timed_out=False,
                    stdout='{"response_type":"changes_requested","getting_closer":true,"response_text":"fix"}\n',
                    stderr="",
                )
            self.coder_calls += 1
            # First coder pass writes completion only; second pass writes validation too.
            (run_dir / "completion-coder.json").write_text("{}", encoding="utf-8")
            if self.coder_calls == 2:
                (run_dir / "validation-record.json").write_text(
                    '{"passed": true}',
                    encoding="utf-8",
                )
            return SimpleNamespace(
                succeeded=True,
                exit_code=0,
                timed_out=False,
                stdout='{"response_type":"ok","response_text":"updated"}\n',
                stderr="",
            )

    runner = DummyRunner()
    monkeypatch.setattr(
        "issue_orchestrator.control.review_exchange_loop.AgentRunner", lambda: runner
    )

    coder_agent = AgentConfig(prompt_path=prompt_path, ai_system="claude-code")
    reviewer_agent = AgentConfig(prompt_path=prompt_path, ai_system="claude-code")

    outcome = run_review_exchange_loop(
        session_output=session_output,
        worktree_path=worktree,
        issue_number=4057,
        issue_title="Test",
        coder_label="agent:backend",
        reviewer_label="agent:reviewer",
        coder_agent=coder_agent,
        reviewer_agent=reviewer_agent,
        max_rounds=1,
        max_no_progress=1,
        require_validation=True,
        web_port=None,
    )

    assert runner.coder_calls == 2
    assert outcome.status == "stopped"
    assert outcome.reason == "max_rounds_exceeded"


def test_review_exchange_calls_on_started_before_agent_rounds(tmp_path: Path, monkeypatch) -> None:
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("Prompt")
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    run_dir = worktree / ".issue-orchestrator" / "sessions" / "run-exchange"
    run_dir.mkdir(parents=True)

    session_output = MagicMock()
    session_output.start_run.return_value = SimpleNamespace(run_dir=run_dir, run_id="run-exchange")

    callback_state = {"started": False, "run_dir": None}

    def _on_started(path: Path) -> None:
        callback_state["started"] = True
        callback_state["run_dir"] = path

    class DummyRunner:
        def run(self, spec):
            assert callback_state["started"] is True
            assert callback_state["run_dir"] == run_dir
            role = Path(spec.output_dir).name
            if role == "reviewer":
                return SimpleNamespace(
                    succeeded=True,
                    exit_code=0,
                    timed_out=False,
                    stdout='{"response_type":"ok","getting_closer":true,"response_text":"approved"}\n',
                    stderr="",
                )
            return SimpleNamespace(
                succeeded=True,
                exit_code=0,
                timed_out=False,
                stdout='{"response_type":"ok","response_text":"updated"}\n',
                stderr="",
            )

    monkeypatch.setattr("issue_orchestrator.control.review_exchange_loop.AgentRunner", DummyRunner)

    coder_agent = AgentConfig(prompt_path=prompt_path, ai_system="claude-code")
    reviewer_agent = AgentConfig(prompt_path=prompt_path, ai_system="claude-code")

    outcome = run_review_exchange_loop(
        session_output=session_output,
        worktree_path=worktree,
        issue_number=4057,
        issue_title="Test",
        coder_label="agent:backend",
        reviewer_label="agent:reviewer",
        coder_agent=coder_agent,
        reviewer_agent=reviewer_agent,
        max_rounds=1,
        max_no_progress=1,
        require_validation=False,
        web_port=None,
        on_started=_on_started,
    )

    assert callback_state["started"] is True
    assert callback_state["run_dir"] == run_dir
    assert outcome.status == "ok"
