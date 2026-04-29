"""Tests for the async/deferred review-exchange path.

The main tick must never block on the review-exchange subprocess. These
tests exercise the seam where :class:`CompletionReviewExchange` either
submits the exchange to a background runner (returning ``deferred=True``)
or short-circuits onto a cached on-disk summary (returning the outcome).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

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
from issue_orchestrator.ports.session_output import SessionOutput


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

    @property
    def run_dir(self) -> Path:
        return self._run_dir

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

    def load_review_exchange_summary(
        self,
        worktree: Path,
        session_name: str,
        *,
        not_before_started_at: str | None = None,
    ):
        if self._summary is None:
            return None
        if not_before_started_at == "future-boundary":
            return None

        @dataclass
        class _Cached:
            summary: dict[str, Any]
            validation_record_path: Path | None
            exchange_dir: Path | None
            summary_path: Path

        return _Cached(
            summary=self._summary.summary,
            validation_record_path=self._summary.validation_record_path,
            exchange_dir=self._run_dir / "review-exchange",
            summary_path=self._run_dir / "review-exchange" / "summary.json",
        )


def _make_config(tmp_path: Path, *, require_validation: bool = False) -> Config:
    cfg = Config(repo_root=tmp_path)
    cfg.review_exchange_mode = "via-local-loop"
    cfg.review_exchange_require_validation = require_validation
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


def _make_record(
    pr: bool = True,
    validation_record_path: Path | None = None,
) -> CompletionRecord:
    actions = [RequestedAction.CREATE_PR, RequestedAction.PUSH_BRANCH] if pr else []
    return CompletionRecord(
        session_id="coding-1",
        timestamp="2026-04-17T00:00:00Z",
        outcome=CompletionOutcome.COMPLETED,
        summary="done",
        implementation="",
        problems="",
        requested_actions=actions,
        validation_record_path=(
            str(validation_record_path) if validation_record_path else None
        ),
    )


def _write_validation_record(path: Path, *, head_sha: str, passed: bool = True) -> None:
    path.write_text(json.dumps({"passed": passed, "head_sha": head_sha}))


def _store_cached_approval(
    session_output: _FakeSessionOutput,
    worktree: Path,
    validation_record_path: Path | None,
) -> None:
    session_output.store_review_exchange_summary(
        worktree,
        "review-exchange-230",
        {
            "status": "ok",
            "completed_rounds": 1,
            "response_text": "Looks good.",
        },
        validation_record_path=validation_record_path,
    )


def _build(
    tmp_path: Path,
    job_runner: _FakeJobRunner,
    started_events: list[dict[str, Any]],
    outcome_events: list[dict[str, Any]],
    *,
    require_validation: bool = False,
) -> tuple[CompletionReviewExchange, _FakeSessionOutput]:
    from issue_orchestrator.control.background_job_supervisor import (
        BackgroundJobSupervisor,
    )

    session_output = _FakeSessionOutput(tmp_path)

    def _on_started(**kwargs: Any) -> None:
        started_events.append(kwargs)

    def _on_outcome(**kwargs: Any) -> None:
        outcome_events.append(kwargs)

    # Tests driving the async path wrap the fake runner in a real supervisor
    # — matching the production contract. Nothing calls supervisor.tick in
    # these tests because the fake runner doesn't raise; when failure paths
    # need testing, tests call `supervisor.tick()` themselves.
    review = CompletionReviewExchange(
        config=_make_config(tmp_path, require_validation=require_validation),
        session_output=cast(SessionOutput, session_output),
        emit_review_started=_on_started,
        emit_review_outcome=_on_outcome,
        job_supervisor=BackgroundJobSupervisor(job_runner),
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
        _plan,
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
        kwargs["on_started"](session_output.run_dir)
        recorded_on_started.append(session_output.run_dir)
        return ReviewExchangeOutcome(
            status="ok",
            rounds=1,
            reason="Looks good.",
            reviewer_response=ReviewExchangeResponse(
                response_type="ok", getting_closer=True, response_text="Looks good."
            ),
            exchange_dir=session_output.run_dir / "review-exchange",
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
    (_, _mode, outcome, completed, halt, deferred) = review.prepare_review_exchange(
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


def test_cached_review_is_reused_when_validation_sha_matches(tmp_path: Path) -> None:
    job_runner = _FakeJobRunner()
    started_events: list[dict[str, Any]] = []
    outcome_events: list[dict[str, Any]] = []
    review, session_output = _build(
        tmp_path,
        job_runner,
        started_events,
        outcome_events,
        require_validation=True,
    )

    cached_validation = tmp_path / "cached-validation.json"
    current_validation = tmp_path / "current-validation.json"
    _write_validation_record(cached_validation, head_sha="same-sha")
    _write_validation_record(current_validation, head_sha="same-sha")
    _store_cached_approval(session_output, tmp_path, cached_validation)

    def fake_loop(**_: Any) -> ReviewExchangeOutcome:
        raise AssertionError("matching cached approval should be reused")

    actions_taken: list[str] = []
    (_, _, outcome, completed, halt, deferred) = review.prepare_review_exchange(
        requested_actions=(RequestedAction.CREATE_PR,),
        worktree=tmp_path,
        issue_number=230,
        issue_title="Example",
        session_name="coding-1",
        agent_label="agent:backend",
        record=_make_record(validation_record_path=current_validation),
        errors=[],
        actions_taken=actions_taken,
        run_review_exchange_loop=fake_loop,
    )

    assert deferred is False
    assert halt is False
    assert completed is True
    assert outcome is not None and outcome.status == "ok"
    assert outcome.summary is not None
    assert not any(key.startswith("_cache_") for key in outcome.summary)
    assert outcome.cache_metadata == {
        "review_cache_summary_path": str(
            session_output.run_dir / "review-exchange" / "summary.json"
        ),
        "review_cache_validation_record_path": str(cached_validation),
        "review_cache_head_sha": "same-sha",
    }
    assert started_events[0]["review_cache_head_sha"] == "same-sha"
    assert outcome_events[0]["review_cache_head_sha"] == "same-sha"
    assert actions_taken == ["Review exchange passed (cached)"]
    assert job_runner.submitted == []


def test_cached_review_is_ignored_when_validation_sha_differs(tmp_path: Path) -> None:
    job_runner = _FakeJobRunner()
    review, session_output = _build(
        tmp_path,
        job_runner,
        [],
        [],
        require_validation=True,
    )

    cached_validation = tmp_path / "cached-validation.json"
    current_validation = tmp_path / "current-validation.json"
    _write_validation_record(cached_validation, head_sha="old-sha")
    _write_validation_record(current_validation, head_sha="new-sha")
    _store_cached_approval(session_output, tmp_path, cached_validation)

    def fake_loop(**_: Any) -> ReviewExchangeOutcome:
        raise AssertionError("fresh exchange should be deferred to background job")

    actions_taken: list[str] = []
    (_, _, outcome, completed, halt, deferred) = review.prepare_review_exchange(
        requested_actions=(RequestedAction.CREATE_PR,),
        worktree=tmp_path,
        issue_number=230,
        issue_title="Example",
        session_name="coding-1",
        agent_label="agent:backend",
        record=_make_record(validation_record_path=current_validation),
        errors=[],
        actions_taken=actions_taken,
        run_review_exchange_loop=fake_loop,
    )

    assert deferred is True
    assert halt is False
    assert completed is False
    assert outcome is None
    assert actions_taken == []
    assert len(job_runner.submitted) == 1


def test_cached_review_before_scratch_boundary_is_ignored(tmp_path: Path) -> None:
    job_runner = _FakeJobRunner()
    review, session_output = _build(
        tmp_path,
        job_runner,
        [],
        [],
        require_validation=True,
    )

    cached_validation = tmp_path / "cached-validation.json"
    current_validation = tmp_path / "current-validation.json"
    _write_validation_record(cached_validation, head_sha="same-sha")
    _write_validation_record(current_validation, head_sha="same-sha")
    _store_cached_approval(session_output, tmp_path, cached_validation)

    def fake_loop(**_: Any) -> ReviewExchangeOutcome:
        raise AssertionError("fresh exchange should be deferred to background job")

    actions_taken: list[str] = []
    (_, _, outcome, completed, halt, deferred) = review.prepare_review_exchange(
        requested_actions=(RequestedAction.CREATE_PR,),
        worktree=tmp_path,
        issue_number=230,
        issue_title="Example",
        session_name="coding-1",
        agent_label="agent:backend",
        record=_make_record(validation_record_path=current_validation),
        errors=[],
        actions_taken=actions_taken,
        run_review_exchange_loop=fake_loop,
        review_cache_boundary_started_at="future-boundary",
    )

    assert deferred is True
    assert halt is False
    assert completed is False
    assert outcome is None
    assert actions_taken == []
    assert len(job_runner.submitted) == 1


@pytest.mark.parametrize(
    ("cached_record_text", "case_id"),
    [
        (None, "missing-file"),
        ("{", "malformed-json"),
        (json.dumps({"passed": True}), "missing-head-sha"),
        (json.dumps({"passed": True, "head_sha": ""}), "empty-head-sha"),
    ],
)
def test_cached_review_is_ignored_without_matching_cached_sha_even_when_validation_not_required(
    tmp_path: Path,
    cached_record_text: str | None,
    case_id: str,
) -> None:
    job_runner = _FakeJobRunner()
    review, session_output = _build(
        tmp_path,
        job_runner,
        [],
        [],
        require_validation=False,
    )

    cached_validation = tmp_path / f"cached-validation-{case_id}.json"
    current_validation = tmp_path / "current-validation.json"
    if cached_record_text is not None:
        cached_validation.write_text(cached_record_text)
    _write_validation_record(current_validation, head_sha="new-sha")
    _store_cached_approval(session_output, tmp_path, cached_validation)

    def fake_loop(**_: Any) -> ReviewExchangeOutcome:
        raise AssertionError("fresh exchange should be deferred to background job")

    actions_taken: list[str] = []
    (_, _, outcome, completed, halt, deferred) = review.prepare_review_exchange(
        requested_actions=(RequestedAction.CREATE_PR,),
        worktree=tmp_path,
        issue_number=230,
        issue_title="Example",
        session_name="coding-1",
        agent_label="agent:backend",
        record=_make_record(validation_record_path=current_validation),
        errors=[],
        actions_taken=actions_taken,
        run_review_exchange_loop=fake_loop,
    )

    assert deferred is True
    assert halt is False
    assert completed is False
    assert outcome is None
    assert actions_taken == []
    assert len(job_runner.submitted) == 1


def test_cached_review_is_ignored_when_current_validation_sha_is_unavailable(
    tmp_path: Path,
) -> None:
    job_runner = _FakeJobRunner()
    review, session_output = _build(
        tmp_path,
        job_runner,
        [],
        [],
        require_validation=True,
    )

    cached_validation = tmp_path / "cached-validation.json"
    current_validation = tmp_path / "current-validation.json"
    _write_validation_record(cached_validation, head_sha="cached-sha")
    current_validation.write_text(json.dumps({"passed": True}))
    _store_cached_approval(session_output, tmp_path, cached_validation)

    def fake_loop(**_: Any) -> ReviewExchangeOutcome:
        raise AssertionError("fresh exchange should be deferred to background job")

    actions_taken: list[str] = []
    (_, _, outcome, completed, halt, deferred) = review.prepare_review_exchange(
        requested_actions=(RequestedAction.CREATE_PR,),
        worktree=tmp_path,
        issue_number=230,
        issue_title="Example",
        session_name="coding-1",
        agent_label="agent:backend",
        record=_make_record(validation_record_path=current_validation),
        errors=[],
        actions_taken=actions_taken,
        run_review_exchange_loop=fake_loop,
    )

    assert deferred is True
    assert halt is False
    assert completed is False
    assert outcome is None
    assert actions_taken == []
    assert len(job_runner.submitted) == 1


def test_cached_review_is_ignored_when_current_validation_failed_on_same_sha(
    tmp_path: Path,
) -> None:
    """Same-SHA cache hit must be rejected when the current validation
    explicitly failed. Without this guard the validation-failed-after-approval
    reroute would replay the cached approval forever (see #6086)."""
    job_runner = _FakeJobRunner()
    review, session_output = _build(
        tmp_path,
        job_runner,
        [],
        [],
        require_validation=True,
    )

    cached_validation = tmp_path / "cached-validation.json"
    current_validation = tmp_path / "current-validation.json"
    _write_validation_record(cached_validation, head_sha="same-sha", passed=True)
    _write_validation_record(current_validation, head_sha="same-sha", passed=False)
    _store_cached_approval(session_output, tmp_path, cached_validation)

    def fake_loop(**_: Any) -> ReviewExchangeOutcome:
        raise AssertionError(
            "fresh exchange should be deferred to background job, "
            "not replayed from the stale cached approval"
        )

    actions_taken: list[str] = []
    (_, _, outcome, completed, halt, deferred) = review.prepare_review_exchange(
        requested_actions=(RequestedAction.CREATE_PR,),
        worktree=tmp_path,
        issue_number=230,
        issue_title="Example",
        session_name="coding-1",
        agent_label="agent:backend",
        record=_make_record(validation_record_path=current_validation),
        errors=[],
        actions_taken=actions_taken,
        run_review_exchange_loop=fake_loop,
    )

    assert deferred is True
    assert halt is False
    assert completed is False
    assert outcome is None
    assert actions_taken == []
    assert len(job_runner.submitted) == 1


def test_no_job_runner_falls_back_to_inline_execution(tmp_path: Path) -> None:
    # Construct without a job runner — the NullBackgroundJobRunner default must
    # short-circuit to inline execution so tests without async wiring still pass.
    started: list[dict[str, Any]] = []
    outcomes: list[dict[str, Any]] = []
    session_output = _FakeSessionOutput(tmp_path)

    review = CompletionReviewExchange(
        config=_make_config(tmp_path),
        session_output=cast(SessionOutput, session_output),
        emit_review_started=lambda **kw: started.append(kw),
        emit_review_outcome=lambda **kw: outcomes.append(kw),
    )

    def fake_loop(**kwargs: Any) -> ReviewExchangeOutcome:
        kwargs["on_started"](session_output.run_dir)
        return ReviewExchangeOutcome(
            status="ok",
            rounds=1,
            reason="Looks good.",
            reviewer_response=ReviewExchangeResponse(
                response_type="ok", getting_closer=True, response_text="Looks good."
            ),
            exchange_dir=session_output.run_dir / "review-exchange",
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
