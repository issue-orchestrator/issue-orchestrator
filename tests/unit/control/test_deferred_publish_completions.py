"""Async deferred-publish completion recovery (issue #6009, review F1).

On the first async completion no review exchange is running, so the observer
finalizes the session and the publish worker is what *starts* the exchange and
reports ``review_exchange_deferred=True``. Without an owner carrying that fact
back across the async boundary, the finalized session is gone, no publish job is
pending, and the hidden exchange can finish without the publish path ever being
re-entered — the completion is stranded.

These tests pin the owner that closes the gap: it remembers the originating
session for each in-flight publish job and restores it for re-observation when a
deferral is reported. The integrated test drives the real observation function,
a real ``CompletionObserver``, and the real owner to prove the completion is
re-observed/resumed rather than disappearing.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock

from issue_orchestrator.control.completion_observer import CompletionObserver
from issue_orchestrator.control.deferred_publish_completions import (
    DeferredPublishCompletions,
)
from issue_orchestrator.control.session_observation import observe_active_sessions
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
    OrchestratorState,
    PublishJobResult,
    RequestedAction,
    Session,
    SessionKey,
    TaskKind,
)
from issue_orchestrator.observation.observation import (
    SessionObservation,
    SessionObservationResult,
)
from tests.unit.session_run_helpers import make_session_run_assets


class _FlippableFinalizationOwner:
    """Finalization owner whose ``review_exchange_running`` fact can be flipped.

    Delegates to the real ``decide_completion_finalization`` matrix so the
    observer maps PROCESS/DEFER exactly as production does. ``running`` starts
    False (no exchange at first observation) and is flipped True once the
    publish worker has started the exchange.
    """

    def __init__(self) -> None:
        self.running = False

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
            review_exchange_running=self.running,
            validation_preflight_configured=validation_preflight_configured,
        )
        return decide_completion_finalization(command)

    def cancel_deferred_review_exchange(
        self, *, issue_number: int, session_name: str | None, reason: str
    ) -> str | None:
        return None


def _session(
    tmp_path: Path,
    *,
    issue_number: int = 9,
    terminal_id: str = "issue-9",
    lease_id: str | None = "lease-9",
) -> Session:
    worktree = tmp_path / f"worktree-{issue_number}"
    worktree.mkdir(parents=True, exist_ok=True)
    return Session(
        key=SessionKey(FakeIssueKey(str(issue_number)), TaskKind.CODE),
        issue=Issue(issue_number, f"Issue {issue_number}", ["agent:test"]),
        agent_config=AgentConfig(prompt_path=tmp_path / "prompt.md"),
        terminal_id=terminal_id,
        worktree_path=worktree,
        branch_name=f"agent/issue-{issue_number}",
        run_assets=make_session_run_assets(worktree, session_name=terminal_id),
        lease_id=lease_id,
    )


def _write_publish_completion(session: Session) -> None:
    path = session.worktree_path / COMPLETION_RECORD_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "session_id": session.terminal_id,
                "timestamp": "2026-04-22T00:00:00Z",
                "outcome": CompletionOutcome.COMPLETED.value,
                "summary": "done",
                # PUSH_BRANCH makes it needs_publish; CREATE_PR makes the
                # publish worker start a review exchange.
                "requested_actions": [
                    RequestedAction.PUSH_BRANCH.value,
                    RequestedAction.CREATE_PR.value,
                ],
                "implementation": "Did the thing",
                "problems": "None",
            }
        )
    )


def _deferred_result(session: Session) -> PublishJobResult:
    return PublishJobResult(
        job_id="job-1",
        issue_number=session.issue.number,
        session_key=session.key.stable_id(),
        success=True,
        review_exchange_deferred=True,
    )


# ---------------------------------------------------------------------------
# Owner unit behavior
# ---------------------------------------------------------------------------


def test_resume_if_deferred_restores_tracked_session(tmp_path: Path) -> None:
    session = _session(tmp_path)
    state = OrchestratorState(active_sessions=[])
    owner = DeferredPublishCompletions()
    owner.track(session)

    handled = owner.resume_if_deferred(_deferred_result(session), state)

    assert handled is True
    assert state.active_sessions == [session]
    # The released claim must not be renewable on the restored session.
    assert session.lease_id is None
    assert session.lease_expires_at is None


def test_resume_if_deferred_is_idempotent_when_already_active(tmp_path: Path) -> None:
    session = _session(tmp_path)
    state = OrchestratorState(active_sessions=[session])
    owner = DeferredPublishCompletions()
    owner.track(session)

    handled = owner.resume_if_deferred(_deferred_result(session), state)

    assert handled is True
    # Not appended twice — the session is already being observed.
    assert state.active_sessions == [session]


def test_resume_if_deferred_terminal_result_drops_tracking(tmp_path: Path) -> None:
    session = _session(tmp_path)
    state = OrchestratorState(active_sessions=[])
    owner = DeferredPublishCompletions()
    owner.track(session)

    terminal = PublishJobResult(
        job_id="job-1",
        issue_number=session.issue.number,
        session_key=session.key.stable_id(),
        success=True,
        pr_url="https://github.com/test/repo/pull/5",
        pr_number=5,
    )
    handled = owner.resume_if_deferred(terminal, state)

    assert handled is False
    assert state.active_sessions == []
    # Tracking is dropped: a later deferral with the same key cannot resurrect
    # an already-finalized session.
    assert owner.resume_if_deferred(_deferred_result(session), state) is True
    assert state.active_sessions == []


def test_resume_if_deferred_without_tracking_does_not_crash(tmp_path: Path) -> None:
    session = _session(tmp_path)
    state = OrchestratorState(active_sessions=[])
    owner = DeferredPublishCompletions()

    handled = owner.resume_if_deferred(_deferred_result(session), state)

    # Still reported as handled (a deferral is never a terminal result), but
    # nothing is restored because the session was never tracked.
    assert handled is True
    assert state.active_sessions == []


# ---------------------------------------------------------------------------
# Integrated async flow: observe -> defer -> restore -> re-observe
# ---------------------------------------------------------------------------


def test_first_time_async_deferral_is_re_observed_not_dropped(tmp_path: Path) -> None:
    """The reviewer's required integrated case (issue #6009 F1).

    No review exchange is running at observation time. The observer finalizes the
    session (PROCESS) and hands it to the deferred-publish owner. The publish
    worker then starts the exchange and returns ``review_exchange_deferred=True``;
    the owner restores the session. A second observation — now with the exchange
    running — keeps the session active (DEFER), proving the completion is
    re-observed and resumed rather than disappearing.
    """
    session = _session(tmp_path)
    _write_publish_completion(session)
    state = OrchestratorState(active_sessions=[session])

    finalization = _FlippableFinalizationOwner()
    completion_observer = CompletionObserver(
        session_output=MagicMock(), finalization_owner=finalization
    )
    owner = DeferredPublishCompletions()

    observer = MagicMock()
    observer.observe_session.return_value = SessionObservationResult(
        observation=SessionObservation.TERMINATED,
        session_exists=True,
    )
    kill_session = MagicMock()
    claim_manager = MagicMock()

    # --- First observation: no exchange running -> finalize + track ----------
    observe_active_sessions(
        state,
        observer,
        completion_observer,
        kill_session,
        claim_manager=claim_manager,
        deferred_publish=owner,
    )

    # The session was finalized: removed, killed, claim released, completion
    # recorded for publishing.
    assert state.active_sessions == []
    assert len(state.observed_completions) == 1
    assert state.observed_completions[0].needs_publish is True
    kill_session.assert_called_once_with("issue-9")
    claim_manager.release_claim.assert_called_once_with(9, "lease-9")

    # --- Publish worker starts the exchange and reports a deferral -----------
    finalization.running = True
    handled = owner.resume_if_deferred(_deferred_result(session), state)

    assert handled is True
    # The completion is NOT stranded: the originating session is back in active
    # tracking for the next observation tick.
    assert state.active_sessions == [session]

    # --- Second observation: exchange now running -> stays deferred ----------
    observe_active_sessions(
        state,
        observer,
        completion_observer,
        kill_session,
        claim_manager=claim_manager,
        deferred_publish=owner,
    )

    # Preserved as RUNNING; no new publish job, no extra teardown.
    assert state.active_sessions == [session]
    assert len(state.observed_completions) == 1
    assert kill_session.call_count == 1
    assert claim_manager.release_claim.call_count == 1


def test_finalized_non_publish_session_is_not_tracked(tmp_path: Path) -> None:
    """A blocked/failed finalization must not leave a restorable registration.

    Only needs-publish completions get a publish job that can defer. A session
    finalized without one must drop any tracking so a later same-key deferral
    cannot restore it.
    """
    session = _session(tmp_path)
    # No completion record on disk -> terminated-without-record -> FAILED.
    state = OrchestratorState(active_sessions=[session])

    finalization = _FlippableFinalizationOwner()
    completion_observer = CompletionObserver(
        session_output=MagicMock(), finalization_owner=finalization
    )
    owner = DeferredPublishCompletions()
    # Pre-seed a stale registration to prove it gets discarded.
    owner.track(session)

    observer = MagicMock()
    observer.observe_session.return_value = SessionObservationResult(
        observation=SessionObservation.TERMINATED,
        session_exists=False,
    )

    observe_active_sessions(
        state,
        observer,
        completion_observer,
        MagicMock(),
        deferred_publish=owner,
    )

    assert state.active_sessions == []
    assert state.observed_completions == []
    # The stale registration was discarded; a deferral cannot restore it.
    assert owner.resume_if_deferred(_deferred_result(session), state) is True
    assert state.active_sessions == []
