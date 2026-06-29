"""Tests for CompletionObserver deferred review-exchange observation.

Async parity for issue #6009. The observer routes every completion through the
same ``CompletionFinalizationPlan`` owner as the synchronous ``decide_outcome``
path and handles each decision intentionally:

- A coder completion whose background review exchange is still running (or a
  timed-out session whose exchange is still inside its supervisor deadline) is
  observed as RUNNING (a deferral) so the session stays observable.
- A timed-out session whose exchange has overshot its deadline is finalized as a
  terminal timeout: the hidden job is cancelled via the owner and the observer
  produces NO ObservedCompletion, so no publish job is enqueued against an
  exchange that would only defer again.
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


class _StubFinalizationOwner:
    """Faithful stand-in for the ``CompletionProcessor`` finalization owner.

    ``completion_finalization_plan`` builds the same
    ``CompletionFinalizationCommand`` the processor builds and delegates to the
    real ``decide_completion_finalization`` matrix, with the review-exchange
    running/within-deadline facts injected instead of probed from a live
    supervisor. ``cancel_deferred_review_exchange`` records its calls and returns
    a configurable error string so the terminal-timeout halt path can be
    exercised through the observer without standing up a processor.
    """

    def __init__(
        self,
        *,
        running: bool,
        within_deadline: bool = False,
        cancel_error: str | None = None,
    ) -> None:
        self._running = running
        self._within_deadline = within_deadline
        self._cancel_error = cancel_error
        self.commands: list[CompletionFinalizationCommand] = []
        self.cancellations: list[dict[str, object]] = []

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

    def cancel_deferred_review_exchange(
        self,
        *,
        issue_number: int,
        session_name: str | None,
        reason: str,
    ) -> str | None:
        self.cancellations.append(
            {
                "issue_number": issue_number,
                "session_name": session_name,
                "reason": reason,
            }
        )
        return self._cancel_error


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


def _write_completion(
    session: Session,
    *,
    requested_actions: list[RequestedAction] | None = None,
) -> None:
    actions = requested_actions or [RequestedAction.CREATE_PR]
    path = session.worktree_path / COMPLETION_RECORD_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "session_id": session.terminal_id,
                "timestamp": "2026-04-22T00:00:00Z",
                "outcome": CompletionOutcome.COMPLETED.value,
                "summary": "done",
                "requested_actions": [action.value for action in actions],
                "implementation": "Did the thing",
                "problems": "None",
            }
        )
    )


def test_observe_completion_defers_when_review_exchange_running(tmp_path: Path) -> None:
    session = _session(tmp_path)
    _write_completion(session)
    owner = _StubFinalizationOwner(running=True)
    observer = CompletionObserver(session_output=Mock(), finalization_owner=owner)

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
    assert len(owner.commands) == 1
    command = owner.commands[0]
    assert command.issue_number == 9
    assert command.session_name == "issue-9"
    assert RequestedAction.CREATE_PR in command.requested_actions
    assert command.runtime_state is CompletionRuntimeState.TERMINATED
    # A running deferral is not a terminal boundary; nothing is cancelled.
    assert owner.cancellations == []


def test_observe_completion_finalizes_when_review_exchange_idle(tmp_path: Path) -> None:
    session = _session(tmp_path)
    _write_completion(session)
    owner = _StubFinalizationOwner(running=False)
    observer = CompletionObserver(session_output=Mock(), finalization_owner=owner)

    decision = observer.observe_completion(
        session,
        SessionObservationResult(
            observation=SessionObservation.TERMINATED,
            session_exists=True,
        ),
    )

    assert decision.status == SessionStatus.COMPLETED
    assert decision.observed is not None
    assert owner.cancellations == []


def test_observe_completion_defers_timed_out_within_deadline(tmp_path: Path) -> None:
    """A timed-out session keeps deferring while its exchange is in-budget.

    Parity with the synchronous finalization matrix: a TIMED_OUT visible
    session whose background review exchange is still inside its own supervisor
    deadline must stay RUNNING (preserved for re-observation), not be finalized
    (issue #6009).
    """
    session = _session(tmp_path)
    _write_completion(session)
    owner = _StubFinalizationOwner(running=True, within_deadline=True)
    observer = CompletionObserver(session_output=Mock(), finalization_owner=owner)

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
    assert owner.commands[0].runtime_state is CompletionRuntimeState.TIMED_OUT
    assert owner.cancellations == []


def test_observe_completion_halts_timed_out_past_deadline(tmp_path: Path) -> None:
    """A timed-out exchange that overshoots its deadline is halted, not recovered.

    The reviewer's over-deadline async case (issue #6009 round 2): a completed
    record requesting both PUSH_BRANCH and CREATE_PR whose exchange is running
    but past its supervisor deadline must NOT become a normal observed/publish
    completion. Otherwise the publish worker returns another
    ``review_exchange_deferred`` with no PR while the active session is already
    gone, leaving the issue stuck. Instead the observer cancels the hidden job
    and finalizes as a terminal TIMED_OUT failure with no ObservedCompletion.
    """
    session = _session(tmp_path)
    _write_completion(
        session,
        requested_actions=[RequestedAction.PUSH_BRANCH, RequestedAction.CREATE_PR],
    )
    owner = _StubFinalizationOwner(running=True, within_deadline=False)
    observer = CompletionObserver(session_output=Mock(), finalization_owner=owner)

    decision = observer.observe_completion(
        session,
        SessionObservationResult(
            observation=SessionObservation.TIMED_OUT,
            session_exists=True,
        ),
    )

    # Not a normal observed/publish completion: no ObservedCompletion is built,
    # so no publish job is enqueued.
    assert decision.observed is None
    assert decision.status == SessionStatus.TIMED_OUT
    assert owner.commands[0].runtime_state is CompletionRuntimeState.TIMED_OUT
    # The terminal review-exchange failure is surfaced by cancelling the hidden
    # job before the active session is removed.
    assert len(owner.cancellations) == 1
    cancellation = owner.cancellations[0]
    assert cancellation["issue_number"] == 9
    assert cancellation["session_name"] == "issue-9"
    assert cancellation["reason"] == "session-timeout"


def test_observe_completion_surfaces_terminal_cancel_error(tmp_path: Path) -> None:
    """A failed cancellation is surfaced in the terminal-timeout reason."""
    session = _session(tmp_path)
    _write_completion(session)
    owner = _StubFinalizationOwner(
        running=True,
        within_deadline=False,
        cancel_error="failed to cancel runtime work: boom",
    )
    observer = CompletionObserver(session_output=Mock(), finalization_owner=owner)

    decision = observer.observe_completion(
        session,
        SessionObservationResult(
            observation=SessionObservation.TIMED_OUT,
            session_exists=True,
        ),
    )

    assert decision.status == SessionStatus.TIMED_OUT
    assert decision.observed is None
    assert "failed to cancel runtime work: boom" in decision.reason
    assert len(owner.cancellations) == 1
