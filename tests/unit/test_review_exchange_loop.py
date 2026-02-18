"""Tests for review exchange loop behaviors."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from issue_orchestrator.control.review_exchange_loop import (
    _refresh_run_validation_record,
    _run_agent_round,
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


def test_refresh_run_validation_record_requires_validation_command(tmp_path: Path, monkeypatch) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    run_dir = worktree / ".issue-orchestrator" / "sessions" / "run-1"
    run_dir.mkdir(parents=True)

    monkeypatch.setattr(
        "issue_orchestrator.control.review_exchange_loop.load_validation_config",
        lambda _worktree: {},
    )

    with pytest.raises(RuntimeError, match="validation\\.cmd is not configured"):
        _refresh_run_validation_record(worktree_path=worktree, run_dir=run_dir)


def test_refresh_run_validation_record_enforces_run_scoped_record(tmp_path: Path, monkeypatch) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    run_dir = worktree / ".issue-orchestrator" / "sessions" / "run-1"
    run_dir.mkdir(parents=True)

    monkeypatch.setattr(
        "issue_orchestrator.control.review_exchange_loop.load_validation_config",
        lambda _worktree: {"cmd": "make validate", "timeout_seconds": 90},
    )

    class DummyGate:
        def __init__(self, _worktree, **_kwargs) -> None:
            pass

        def run(self, *, session_output_dir: Path):
            (session_output_dir / "validation-record.json").write_text('{"passed": true}', encoding="utf-8")
            return None

    monkeypatch.setattr("issue_orchestrator.control.review_exchange_loop.AgentGate", DummyGate)

    _refresh_run_validation_record(worktree_path=worktree, run_dir=run_dir)
    assert (run_dir / "validation-record.json").exists()
