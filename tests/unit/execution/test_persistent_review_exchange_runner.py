"""Unit-level coverage for ``PersistentReviewExchangeRunner``.

Pins the reviewer-worktree lifecycle the adapter owns:

- ``create_reviewer_worktree`` is called at exchange start
- ``run_persistent_session_exchange`` is called once with the runner's
  ``session_output`` and the reviewer worktree path
- the ``before_reviewer_round`` callback fast-forwards only on rounds 2+
- ``remove_reviewer_worktree`` is always called on exit, including
  when the inner runner raises

End-to-end behaviour against the real PTY runner is covered in
``tests/integration/test_persistent_review_exchange_integration.py``;
this file focuses on the adapter's policy on top of those helpers.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from issue_orchestrator.domain.models import AgentConfig
from issue_orchestrator.domain.review_exchange import ReviewExchangeOutcome
from issue_orchestrator.execution import persistent_review_exchange_runner as prer


@pytest.fixture
def stub_lifecycle(monkeypatch, tmp_path):
    """Replace the reviewer-worktree helpers with no-op stubs that record calls."""
    calls: dict[str, list[Any]] = {
        "create": [],
        "fast_forward": [],
        "remove": [],
        "resolve_branch": [],
    }

    def _resolve_branch(wt: Path) -> str:
        calls["resolve_branch"].append(wt)
        return "feature/test"

    def _create(*, coder_worktree, coder_branch, timestamp):
        calls["create"].append({
            "coder_worktree": coder_worktree,
            "coder_branch": coder_branch,
            "timestamp": timestamp,
        })
        return SimpleNamespace(
            path=tmp_path / "reviewer-wt",
            coder_branch=coder_branch,
        )

    def _fast_forward(reviewer_wt):
        calls["fast_forward"].append(reviewer_wt)
        return "deadbeef"

    def _remove(reviewer_wt, *, force=False):
        calls["remove"].append({"reviewer_wt": reviewer_wt, "force": force})

    monkeypatch.setattr(prer, "resolve_current_branch", _resolve_branch)
    monkeypatch.setattr(prer, "create_reviewer_worktree", _create)
    monkeypatch.setattr(prer, "fast_forward_reviewer_worktree", _fast_forward)
    monkeypatch.setattr(prer, "remove_reviewer_worktree", _remove)
    return calls


def _make_agent(tmp_path: Path) -> AgentConfig:
    prompt = tmp_path / "prompt.md"
    prompt.write_text("hello")
    return AgentConfig(
        prompt_path=prompt,
        ai_system="claude-code",
        command="echo",
    )


def _canned_outcome() -> ReviewExchangeOutcome:
    return ReviewExchangeOutcome(
        status="ok",
        rounds=2,
        reason="reviewer_ok",
        reviewer_response=None,
        exchange_dir=None,
        summary={"status": "ok", "completed_rounds": 2},
    )


def _run(runner, tmp_path):
    return runner.run(
        coder_worktree=tmp_path / "coder",
        issue_number=42,
        issue_title="t",
        coder_label="agent:coder",
        reviewer_label="agent:reviewer",
        coder_agent=_make_agent(tmp_path),
        reviewer_agent=_make_agent(tmp_path),
        max_rounds=3,
        max_no_progress=3,
        require_validation=False,
    )


def test_run_creates_reviewer_worktree_and_passes_path_through(
    monkeypatch, tmp_path: Path, stub_lifecycle
):
    """Reviewer worktree is created at exchange start and threaded into
    the inner runner as ``reviewer_worktree_path``."""
    captured: dict[str, Any] = {}

    def _fake_inner(**kwargs):
        captured.update(kwargs)
        return _canned_outcome()

    monkeypatch.setattr(prer, "run_persistent_session_exchange", _fake_inner)
    runner = prer.PersistentReviewExchangeRunner(MagicMock(name="session_output"))

    outcome = _run(runner, tmp_path)

    assert outcome.status == "ok"
    assert len(stub_lifecycle["create"]) == 1
    assert captured["reviewer_worktree_path"] == tmp_path / "reviewer-wt"
    assert captured["coder_worktree_path"] == tmp_path / "coder"
    assert captured["session_output"] is runner._session_output


def test_before_reviewer_round_fast_forwards_only_after_round_one(
    monkeypatch, tmp_path: Path, stub_lifecycle
):
    """Round 1's reviewer worktree is already at the coder tip from
    creation; rounds 2+ must fast-forward to pick up the coder's
    new commits."""
    captured: dict[str, Any] = {}

    def _fake_inner(**kwargs):
        captured.update(kwargs)
        return _canned_outcome()

    monkeypatch.setattr(prer, "run_persistent_session_exchange", _fake_inner)
    runner = prer.PersistentReviewExchangeRunner(MagicMock(name="session_output"))

    _run(runner, tmp_path)
    before = captured["before_reviewer_round"]

    before(1)
    assert stub_lifecycle["fast_forward"] == []

    before(2)
    before(3)
    assert len(stub_lifecycle["fast_forward"]) == 2


def test_run_always_removes_reviewer_worktree_on_success(
    monkeypatch, tmp_path: Path, stub_lifecycle
):
    """Happy path still reclaims the sibling worktree."""
    monkeypatch.setattr(
        prer, "run_persistent_session_exchange",
        lambda **_: _canned_outcome(),
    )
    runner = prer.PersistentReviewExchangeRunner(MagicMock(name="session_output"))

    _run(runner, tmp_path)

    assert len(stub_lifecycle["remove"]) == 1
    assert stub_lifecycle["remove"][0]["force"] is True


def test_run_removes_reviewer_worktree_when_inner_runner_raises(
    monkeypatch, tmp_path: Path, stub_lifecycle
):
    """If the persistent runner blows up mid-exchange, the sibling
    worktree must still be reclaimed (otherwise a kill -9 or stuck
    checkout would strand it)."""

    def _explode(**_):
        raise RuntimeError("simulated runner failure")

    monkeypatch.setattr(prer, "run_persistent_session_exchange", _explode)
    runner = prer.PersistentReviewExchangeRunner(MagicMock(name="session_output"))

    with pytest.raises(RuntimeError, match="simulated runner failure"):
        _run(runner, tmp_path)

    assert len(stub_lifecycle["remove"]) == 1
    assert stub_lifecycle["remove"][0]["force"] is True
