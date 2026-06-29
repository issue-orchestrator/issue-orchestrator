"""Tests for CompletionObserver deferred review-exchange observation.

Async parity for issue #6009: a coder completion whose background review
exchange is still running must be observed as RUNNING (a deferral) so the
session stays observable, instead of being finalized like the synchronous
``decide_outcome`` path. The observer routes that decision through the same
``CompletionFinalizationPlan`` owner as the synchronous path, so a timed-out
visible session whose exchange is still inside its supervisor deadline also
keeps deferring (rather than being hard-coded to finalize).
"""

import json
from pathlib import Path
from unittest.mock import Mock

from issue_orchestrator.control.completion_observer import CompletionObserver
from issue_orchestrator.domain.completion_finalization import (
    CompletionFinalizationCommand,
    CompletionFinalizationPlan,
    CompletionRuntimeState,
    decide_completion_finalization,
)
from issue_orchestrator.domain.issue_key import FakeIssueKey
from issue_orchestrator.domain.models import (
    COMPLETION_RECORD_PATH,
    AgentConfig,
    CompletionOutcome,
    Issue,
    RequestedAction,
    Session,
    SessionKey,
    SessionStatus,
    TaskKind,
)
from issue_orchestrator.observation.observation import (
    SessionObservation,
    SessionObservationResult,
)
from tests.unit.session_run_helpers import make_session_run_assets


class _StubFinalizationPlanner:
    """Faithful stand-in for ``CompletionProcessor.completion_finalization_plan``.

    Builds the same ``CompletionFinalizationCommand`` the processor builds and
    delegates to the real ``decide_completion_finalization`` matrix, with the
    review-exchange running/within-deadline facts injected instead of probed
    from a live supervisor. This exercises the real finalization policy through
    the observer without standing up a processor.
    """

    def __init__(self, *, running: bool, within_deadline: bool = False) -> None:
        self._running = running
        self._within_deadline = within_deadline
        self.commands: list[CompletionFinalizationCommand] = []

    def completion_finalization_plan(
        self,
        *,
        issue_number: int,
        session_name: str | None,
        outcome: CompletionOutcome,
        requested_actions: tuple[RequestedAction, ...],
        runtime_state: CompletionRuntimeState,
        validation_preflight_configured: bool,
    ) -> CompletionFinalizationPlan:
        command = CompletionFinalizationCommand(
            issue_number=issue_number,
            session_name=session_name,
            outcome=outcome,
            requested_actions=requested_actions,
            runtime_state=runtime_state,
            review_exchange_running=self._running,
            validation_preflight_configured=validation_preflight_configured,
            # Mirror the processor: within-deadline is only meaningful while
            # the background job is actually running.
            review_exchange_within_deadline=self._running and self._within_deadline,
        )
        self.commands.append(command)
        return decide_completion_finalization(command)


def _session(tmp_path: Path, *, terminal_id: str = "issue-9") -> Session:
    worktree = tmp_path / "worktree"
    worktree.mkdir(parents=True, exist_ok=True)
    return Session(
        key=SessionKey(FakeIssueKey("9"), TaskKind.CODE),
        issue=Issue(9, "Issue 9", ["agent:test"]),
        agent_config=AgentConfig(prompt_path=tmp_path / "prompt.md"),
        terminal_id=terminal_id,
        worktree_path=worktree,
        branch_name="agent/issue-9",
        run_assets=make_session_run_assets(worktree, session_name=terminal_id),
    )


def _write_completion(session: Session) -> None:
    path = session.worktree_path / COMPLETION_RECORD_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "session_id": session.terminal_id,
                "timestamp": "2026-04-22T00:00:00Z",
                "outcome": CompletionOutcome.COMPLETED.value,
                "summary": "done",
                "requested_actions": [RequestedAction.CREATE_PR.value],
                "implementation": "Did the thing",
                "problems": "None",
            }
        )
    )


def test_observe_completion_defers_when_review_exchange_running(tmp_path: Path) -> None:
    session = _session(tmp_path)
    _write_completion(session)
    planner = _StubFinalizationPlanner(running=True)
    observer = CompletionObserver(session_output=Mock(), finalization_planner=planner)

    decision = observer.observe_completion(
        session,
        SessionObservationResult(
            observation=SessionObservation.TERMINATED,
            session_exists=True,
        ),
    )

    assert decision.status == SessionStatus.RUNNING
    assert decision.observed is None
    assert "review exchange" in decision.reason.lower()
    # The finalization command is built from the observed completion record and
    # the terminal observation state.
    assert len(planner.commands) == 1
    command = planner.commands[0]
    assert command.issue_number == 9
    assert command.session_name == "issue-9"
    assert RequestedAction.CREATE_PR in command.requested_actions
    assert command.runtime_state is CompletionRuntimeState.TERMINATED


def test_observe_completion_finalizes_when_review_exchange_idle(tmp_path: Path) -> None:
    session = _session(tmp_path)
    _write_completion(session)
    planner = _StubFinalizationPlanner(running=False)
    observer = CompletionObserver(session_output=Mock(), finalization_planner=planner)

    decision = observer.observe_completion(
        session,
        SessionObservationResult(
            observation=SessionObservation.TERMINATED,
            session_exists=True,
        ),
    )

    assert decision.status == SessionStatus.COMPLETED
    assert decision.observed is not None


def test_observe_completion_defers_timed_out_within_deadline(tmp_path: Path) -> None:
    """A timed-out session keeps deferring while its exchange is in-budget.

    Parity with the synchronous finalization matrix: a TIMED_OUT visible
    session whose background review exchange is still inside its own supervisor
    deadline must stay RUNNING (preserved for re-observation), not be finalized
    (issue #6009).
    """
    session = _session(tmp_path)
    _write_completion(session)
    planner = _StubFinalizationPlanner(running=True, within_deadline=True)
    observer = CompletionObserver(session_output=Mock(), finalization_planner=planner)

    decision = observer.observe_completion(
        session,
        SessionObservationResult(
            observation=SessionObservation.TIMED_OUT,
            session_exists=True,
        ),
    )

    assert decision.status == SessionStatus.RUNNING
    assert decision.observed is None
    assert decision.recovered_from_timeout is False
    assert planner.commands[0].runtime_state is CompletionRuntimeState.TIMED_OUT


def test_observe_completion_finalizes_timed_out_past_deadline(tmp_path: Path) -> None:
    """A timed-out session is finalized once its exchange overshoots its budget.

    When the background exchange is no longer within its supervisor deadline the
    owner returns the terminal-timeout decision, so the observer recovers the
    completed work instead of deferring forever.
    """
    session = _session(tmp_path)
    _write_completion(session)
    planner = _StubFinalizationPlanner(running=True, within_deadline=False)
    observer = CompletionObserver(session_output=Mock(), finalization_planner=planner)

    decision = observer.observe_completion(
        session,
        SessionObservationResult(
            observation=SessionObservation.TIMED_OUT,
            session_exists=True,
        ),
    )

    assert decision.status != SessionStatus.RUNNING
    assert decision.observed is not None
    assert decision.recovered_from_timeout is True
