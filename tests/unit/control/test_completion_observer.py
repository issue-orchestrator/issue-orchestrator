"""Tests for CompletionObserver deferred review-exchange observation.

Async parity for issue #6009: a terminated coder completion whose background
review exchange is still running must be observed as RUNNING (a deferral) so the
session stays observable, instead of being finalized like the synchronous
``decide_outcome`` path.
"""

import json
from pathlib import Path
from unittest.mock import Mock

from issue_orchestrator.control.completion_observer import CompletionObserver
from issue_orchestrator.domain.completion_finalization import (
    ReviewExchangeRunningQuery,
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


class _StubReviewExchangeProbe:
    """Records queries and reports a configured running state."""

    def __init__(self, running: bool) -> None:
        self._running = running
        self.queries: list[ReviewExchangeRunningQuery] = []

    def is_review_exchange_running_for_completion(
        self, query: ReviewExchangeRunningQuery
    ) -> bool:
        self.queries.append(query)
        return self._running


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
    probe = _StubReviewExchangeProbe(running=True)
    observer = CompletionObserver(session_output=Mock(), review_exchange_probe=probe)

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
    # The deferral query is built from the observed completion record.
    assert len(probe.queries) == 1
    query = probe.queries[0]
    assert query.issue_number == 9
    assert query.session_name == "issue-9"
    assert RequestedAction.CREATE_PR in query.requested_actions


def test_observe_completion_finalizes_when_review_exchange_idle(tmp_path: Path) -> None:
    session = _session(tmp_path)
    _write_completion(session)
    probe = _StubReviewExchangeProbe(running=False)
    observer = CompletionObserver(session_output=Mock(), review_exchange_probe=probe)

    decision = observer.observe_completion(
        session,
        SessionObservationResult(
            observation=SessionObservation.TERMINATED,
            session_exists=True,
        ),
    )

    assert decision.status == SessionStatus.COMPLETED
    assert decision.observed is not None


def test_observe_completion_does_not_defer_timed_out_session(tmp_path: Path) -> None:
    """A timed-out session is finalized; cancel/terminal policy is execution's."""
    session = _session(tmp_path)
    _write_completion(session)
    probe = _StubReviewExchangeProbe(running=True)
    observer = CompletionObserver(session_output=Mock(), review_exchange_probe=probe)

    decision = observer.observe_completion(
        session,
        SessionObservationResult(
            observation=SessionObservation.TIMED_OUT,
            session_exists=True,
        ),
    )

    assert decision.status != SessionStatus.RUNNING
    assert decision.recovered_from_timeout is True
    assert probe.queries == []
