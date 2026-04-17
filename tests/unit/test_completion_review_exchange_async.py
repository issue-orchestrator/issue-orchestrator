"""Tests for the async/deferred review-exchange path.

The main tick must never block on the review-exchange subprocess. These
tests exercise the seam where :class:`CompletionReviewExchange` either
submits the exchange to a background runner (returning ``deferred=True``)
or short-circuits onto a cached on-disk summary (returning the outcome).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from issue_orchestrator.control.completion_review_exchange import (
    CompletionReviewExchange,
)
from issue_orchestrator.control.review_exchange_loop import (
    ReviewExchangeOutcome,
    ReviewExchangeResponse,
)
from issue_orchestrator.domain.models import (
    CompletionOutcome,
    CompletionRecord,
    RequestedAction,
)
from issue_orchestrator.infra.config import Config
from issue_orchestrator.domain.models import AgentConfig
from issue_orchestrator.ports.background_job import CompletedJob
from issue_orchestrator.ports.session_output import SessionOutput, SessionRun


class _FakeJobRunner:
    """Records submitted jobs and lets tests control whether they are 'running'."""

    def __init__(self) -> None:
        self.submitted: list[tuple[str, Callable[[], None]]] = []
        self._running: set[str] = set()
        self._completed: list[CompletedJob] = []

    def submit(self, job_id: str, fn: Callable[[], None]) -> bool:
        if job_id in self._running:
            return False
        self._running.add(job_id)
        self.submitted.append((job_id, fn))
        return True

    def is_running(self, job_id: str) -> bool:
        return job_id in self._running

    def drain_completed(self) -> list[CompletedJob]:
        done = self._completed
        self._completed = []
        return done

    def finish(self, job_id: str, error: BaseException | None = None) -> None:
        self._running.discard(job_id)
        self._completed.append(CompletedJob(job_id=job_id, error=error))


@dataclass
class _CapturedSummary:
    summary: dict[str, Any]
    validation_record_path: Path | None


class _FakeSessionOutput:
    """Minimal SessionOutput stand-in exposing only what the async path touches."""

    def __init__(self, worktree: Path) -> None:
        self._worktree = worktree
        self._summary: _CapturedSummary | None = None
        self._run_dir = worktree / ".sessions" / "exchange-run"
        (self._run_dir / "review-exchange").mkdir(parents=True, exist_ok=True)

    def find_run_dir(self, worktree: Path, session_name: str) -> Path | None:
        return self._run_dir

    def store_review_exchange_summary(
        self,
        worktree: Path,
        review_session_name: str,
        summary: dict[str, Any],
        *,
        validation_record_path: Path | None = None,
    ) -> None:
        self._summary = _CapturedSummary(
            summary=dict(summary),
            validation_record_path=validation_record_path,
        )

    def load_review_exchange_summary(self, worktree: Path, session_name: str):
        if self._summary is None:
            return None

        @dataclass
        class _Cached:
            summary: dict[str, Any]
            validation_record_path: Path | None
            exchange_dir: Path | None

        return _Cached(
            summary=self._summary.summary,
            validation_record_path=self._summary.validation_record_path,
            exchange_dir=self._run_dir / "review-exchange",
        )


def _make_config(tmp_path: Path) -> Config:
    cfg = Config(repo_root=tmp_path)
    cfg.review_exchange_mode = "via-local-loop"
    cfg.review_exchange_require_validation = False
    cfg.code_review_agent = "agent:reviewer"
    cfg.agents = {
        "agent:backend": AgentConfig(
            prompt_path=tmp_path / "backend.md",
            command="claude --print",
            reviewer="agent:reviewer",
        ),
        "agent:reviewer": AgentConfig(
            prompt_path=tmp_path / "reviewer.md",
            command="claude --print",
        ),
    }
    return cfg


def _make_record(pr: bool = True) -> CompletionRecord:
    actions = [RequestedAction.CREATE_PR, RequestedAction.PUSH_BRANCH] if pr else []
    return CompletionRecord(
        session_id="coding-1",
        timestamp="2026-04-17T00:00:00Z",
        outcome=CompletionOutcome.COMPLETED,
        summary="done",
        implementation="",
        problems="",
        requested_actions=actions,
    )


def _build(
    tmp_path: Path,
    job_runner: _FakeJobRunner,
    started_events: list[dict[str, Any]],
    outcome_events: list[dict[str, Any]],
) -> tuple[CompletionReviewExchange, _FakeSessionOutput]:
    session_output = _FakeSessionOutput(tmp_path)

    def _on_started(**kwargs: Any) -> None:
        started_events.append(kwargs)

    def _on_outcome(**kwargs: Any) -> None:
        outcome_events.append(kwargs)

    review = CompletionReviewExchange(
        config=_make_config(tmp_path),
        session_output=session_output,  # type: ignore[arg-type]
        emit_review_started=_on_started,
        emit_review_outcome=_on_outcome,
        job_runner=job_runner,
    )
    return review, session_output


def test_first_pass_submits_background_job_and_returns_deferred(tmp_path: Path) -> None:
    job_runner = _FakeJobRunner()
    started: list[dict[str, Any]] = []
    outcomes: list[dict[str, Any]] = []
    review, _ = _build(tmp_path, job_runner, started, outcomes)
    called = []

    def fake_loop(**kwargs: Any) -> ReviewExchangeOutcome:
        called.append(kwargs)
        raise AssertionError("loop must not run synchronously when deferred")

    (
        plan,
        mode,
        outcome,
        completed,
        halt,
        deferred,
    ) = review.prepare_review_exchange(
        requested_actions=(RequestedAction.CREATE_PR, RequestedAction.PUSH_BRANCH),
        worktree=tmp_path,
        issue_number=230,
        issue_title="Example",
        session_name="coding-1",
        agent_label="agent:backend",
        record=_make_record(),
        errors=[],
        actions_taken=[],
        run_review_exchange_loop=fake_loop,
    )

    assert deferred is True
    assert halt is False
    assert completed is False
    assert outcome is None
    assert mode == "via-local-loop"
    # The job was submitted but the caller's loop did NOT run on this thread.
    assert len(job_runner.submitted) == 1
    assert called == []
    # Job id is stable for the same (issue, session_name).
    assert job_runner.submitted[0][0] == "review-exchange:230:coding-1"


def test_second_pass_while_running_keeps_deferring(tmp_path: Path) -> None:
    job_runner = _FakeJobRunner()
    review, _ = _build(tmp_path, job_runner, [], [])

    def fake_loop(**_: Any) -> ReviewExchangeOutcome:
        raise AssertionError("must not run")

    # Tick N: submits.
    review.prepare_review_exchange(
        requested_actions=(RequestedAction.CREATE_PR,),
        worktree=tmp_path,
        issue_number=230,
        issue_title="Example",
        session_name="coding-1",
        agent_label="agent:backend",
        record=_make_record(),
        errors=[],
        actions_taken=[],
        run_review_exchange_loop=fake_loop,
    )

    # Tick N+1: still running.
    (_, _, _, _, _, deferred) = review.prepare_review_exchange(
        requested_actions=(RequestedAction.CREATE_PR,),
        worktree=tmp_path,
        issue_number=230,
        issue_title="Example",
        session_name="coding-1",
        agent_label="agent:backend",
        record=_make_record(),
        errors=[],
        actions_taken=[],
        run_review_exchange_loop=fake_loop,
    )
    assert deferred is True
    # Exactly one submission — second tick reused the in-flight job.
    assert len(job_runner.submitted) == 1


def test_tick_after_completion_resolves_cached_outcome(tmp_path: Path) -> None:
    job_runner = _FakeJobRunner()
    started: list[dict[str, Any]] = []
    outcomes: list[dict[str, Any]] = []
    review, session_output = _build(tmp_path, job_runner, started, outcomes)

    recorded_on_started: list[Path] = []

    def fake_loop(**kwargs: Any) -> ReviewExchangeOutcome:
        kwargs["on_started"](session_output._run_dir)  # type: ignore[attr-defined]
        recorded_on_started.append(session_output._run_dir)  # type: ignore[attr-defined]
        return ReviewExchangeOutcome(
            status="ok",
            rounds=1,
            reason="Looks good.",
            reviewer_response=ReviewExchangeResponse(
                response_type="ok", getting_closer=True, response_text="Looks good."
            ),
            exchange_dir=session_output._run_dir / "review-exchange",  # type: ignore[attr-defined]
            summary={
                "status": "ok",
                "completed_rounds": 1,
                "response_text": "Looks good.",
            },
        )

    # Tick N — submit.
    review.prepare_review_exchange(
        requested_actions=(RequestedAction.CREATE_PR,),
        worktree=tmp_path,
        issue_number=230,
        issue_title="Example",
        session_name="coding-1",
        agent_label="agent:backend",
        record=_make_record(),
        errors=[],
        actions_taken=[],
        run_review_exchange_loop=fake_loop,
    )
    # Execute the submitted callable inline to simulate the background thread
    # finishing and writing summary.json.
    job_id, submitted_fn = job_runner.submitted[0]
    submitted_fn()
    job_runner.finish(job_id)

    # Tick N+k — cached summary present, exchange resolves.
    actions_taken: list[str] = []
    (_, mode, outcome, completed, halt, deferred) = review.prepare_review_exchange(
        requested_actions=(RequestedAction.CREATE_PR,),
        worktree=tmp_path,
        issue_number=230,
        issue_title="Example",
        session_name="coding-1",
        agent_label="agent:backend",
        record=_make_record(),
        errors=[],
        actions_taken=actions_taken,
        run_review_exchange_loop=fake_loop,
    )

    assert deferred is False
    assert halt is False
    assert completed is True
    assert outcome is not None and outcome.status == "ok"
    assert "Review exchange passed (cached)" in actions_taken
    assert recorded_on_started, "emit_review_started must fire during the job"


def test_no_job_runner_falls_back_to_inline_execution(tmp_path: Path) -> None:
    # Construct without a job runner — the NullBackgroundJobRunner default must
    # short-circuit to inline execution so tests without async wiring still pass.
    started: list[dict[str, Any]] = []
    outcomes: list[dict[str, Any]] = []
    session_output = _FakeSessionOutput(tmp_path)

    review = CompletionReviewExchange(
        config=_make_config(tmp_path),
        session_output=session_output,  # type: ignore[arg-type]
        emit_review_started=lambda **kw: started.append(kw),
        emit_review_outcome=lambda **kw: outcomes.append(kw),
    )

    def fake_loop(**kwargs: Any) -> ReviewExchangeOutcome:
        kwargs["on_started"](session_output._run_dir)  # type: ignore[attr-defined]
        return ReviewExchangeOutcome(
            status="ok",
            rounds=1,
            reason="Looks good.",
            reviewer_response=ReviewExchangeResponse(
                response_type="ok", getting_closer=True, response_text="Looks good."
            ),
            exchange_dir=session_output._run_dir / "review-exchange",  # type: ignore[attr-defined]
            summary={
                "status": "ok",
                "completed_rounds": 1,
                "response_text": "Looks good.",
            },
        )

    (_, _, outcome, completed, halt, deferred) = review.prepare_review_exchange(
        requested_actions=(RequestedAction.CREATE_PR,),
        worktree=tmp_path,
        issue_number=230,
        issue_title="Example",
        session_name="coding-1",
        agent_label="agent:backend",
        record=_make_record(),
        errors=[],
        actions_taken=[],
        run_review_exchange_loop=fake_loop,
    )

    assert deferred is False
    assert halt is False
    assert completed is True
    assert outcome is not None
