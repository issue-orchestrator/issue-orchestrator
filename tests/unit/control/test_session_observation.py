from pathlib import Path
from unittest.mock import MagicMock

from issue_orchestrator.control.active_sessions import append_unique_active_sessions, has_active_terminal
from issue_orchestrator.control.completion_observer import ObservationDecision
from issue_orchestrator.control.session_observation import (
    observe_active_sessions,
)
from issue_orchestrator.domain.issue_key import FakeIssueKey
from issue_orchestrator.domain.models import (
    AgentConfig,
    Issue,
    OrchestratorState,
    Session,
    SessionKey,
    SessionStatus,
    TaskKind,
)
from issue_orchestrator.events import EventName
from issue_orchestrator.infra.provider_resilience import ProviderStatus, now_iso
from issue_orchestrator.observation.observation import (
    SessionObservation,
    SessionObservationResult,
)
from issue_orchestrator.ports import TraceEvent
from issue_orchestrator.ports.provider_resilience import ProviderErrorType


class CapturingEventSink:
    def __init__(self) -> None:
        self.events: list[TraceEvent] = []

    def publish(self, event: TraceEvent) -> None:
        self.events.append(event)


def _session(
    tmp_path: Path,
    *,
    issue_number: int,
    terminal_id: str,
    lease_id: str | None = None,
) -> Session:
    issue = Issue(issue_number, f"Issue {issue_number}", ["agent:test"])
    return Session(
        key=SessionKey(FakeIssueKey(str(issue_number)), TaskKind.CODE),
        issue=issue,
        agent_config=AgentConfig(prompt_path=tmp_path / "prompt.md"),
        terminal_id=terminal_id,
        worktree_path=tmp_path / f"worktree-{issue_number}",
        branch_name=f"agent/issue-{issue_number}",
        lease_id=lease_id,
    )


def test_append_unique_active_sessions_suppresses_duplicate_terminals(tmp_path: Path) -> None:
    existing = _session(tmp_path, issue_number=1, terminal_id="issue-1")
    duplicate = _session(tmp_path, issue_number=2, terminal_id="issue-1")
    incoming = _session(tmp_path, issue_number=3, terminal_id="issue-3")
    active_sessions = [existing]

    added = append_unique_active_sessions(active_sessions, [duplicate, incoming])

    assert added == [incoming]
    assert [session.issue.number for session in active_sessions] == [1, 3]
    assert has_active_terminal(active_sessions, "issue-1") is True
    assert has_active_terminal(active_sessions, "missing") is False


def test_observe_active_sessions_leaves_running_sessions_active(tmp_path: Path) -> None:
    session = _session(tmp_path, issue_number=1, terminal_id="issue-1")
    state = OrchestratorState(active_sessions=[session])
    observer = MagicMock()
    observer.observe_session.return_value = SessionObservationResult.running()
    completion_observer = MagicMock()
    kill_session = MagicMock()

    observe_active_sessions(
        state,
        observer,
        completion_observer,
        kill_session,
    )

    assert state.active_sessions == [session]
    completion_observer.observe_completion.assert_not_called()
    kill_session.assert_not_called()


def test_observe_active_sessions_records_terminal_failure_and_releases_claim(
    tmp_path: Path,
) -> None:
    session = _session(
        tmp_path,
        issue_number=7,
        terminal_id="issue-7",
        lease_id="lease-7",
    )
    state = OrchestratorState(active_sessions=[session])
    observer = MagicMock()
    terminal_observation = SessionObservationResult(
        observation=SessionObservation.TERMINATED,
        session_exists=False,
    )
    observer.observe_session.return_value = terminal_observation
    completion_observer = MagicMock()
    completion_observer.observe_completion.return_value = ObservationDecision(
        status=SessionStatus.FAILED,
        provider_status=ProviderStatus(
            provider="codex",
            error_type=ProviderErrorType.TRANSIENT,
            attempts=2,
            succeeded=False,
            exit_code=1,
            timed_out=False,
            last_error_summary="rate limited",
            last_attempt_at=now_iso(),
        ),
    )
    kill_session = MagicMock()
    claim_manager = MagicMock()
    events = CapturingEventSink()
    provider_resilience = MagicMock()

    observe_active_sessions(
        state,
        observer,
        completion_observer,
        kill_session,
        claim_manager=claim_manager,
        events=events,
        provider_resilience=provider_resilience,
    )

    assert state.active_sessions == []
    assert state.failed_this_cycle == {7}
    assert len(state.discovered_failures) == 1
    assert state.discovered_failures[0].failure_reason == "failed"
    kill_session.assert_called_once_with("issue-7")
    claim_manager.release_claim.assert_called_once_with(7, "lease-7")
    provider_resilience.record_transient_failure.assert_called_once_with(
        "codex",
        error_summary="rate limited",
        attempts=2,
    )
    assert [event.event_type for event in events.events] == [
        EventName.OBSERVATION_RESULT,
        EventName.CLAIM_RELEASED,
    ]
    assert events.events[0].data["session_name"] == "issue-7"
    assert events.events[1].data["lease_id"] == "lease-7"


def test_observe_active_sessions_skips_duplicate_snapshot_entries(tmp_path: Path) -> None:
    session = _session(tmp_path, issue_number=7, terminal_id="issue-7")
    duplicate = _session(tmp_path, issue_number=7, terminal_id="issue-7")
    state = OrchestratorState(active_sessions=[session, duplicate])
    observer = MagicMock()
    terminal_observation = SessionObservationResult(
        observation=SessionObservation.TIMED_OUT,
        session_exists=True,
    )
    observer.observe_session.return_value = terminal_observation
    completion_observer = MagicMock()
    completion_observer.observe_completion.return_value = ObservationDecision(
        status=SessionStatus.TIMED_OUT,
    )
    kill_session = MagicMock()

    observe_active_sessions(
        state,
        observer,
        completion_observer,
        kill_session,
    )

    assert state.active_sessions == []
    observer.observe_session.assert_called_once_with(session)
    completion_observer.observe_completion.assert_called_once_with(
        session,
        terminal_observation,
    )
    kill_session.assert_called_once_with("issue-7")
