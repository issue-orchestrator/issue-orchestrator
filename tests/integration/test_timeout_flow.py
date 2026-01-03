"""Integration tests for session timeout handling.

These tests verify that a timed-out session is detected and mapped to the
appropriate SessionDecision without requiring full e2e orchestration.
"""

from datetime import datetime, timedelta
from pathlib import Path

from issue_orchestrator.infra.config import Config
from issue_orchestrator.control.completion_processor import CompletionProcessor
from issue_orchestrator.control.session_controller import SessionController
from issue_orchestrator.domain.issue_key import FakeIssueKey
from issue_orchestrator.domain.session_key import SessionKey, TaskKind
from issue_orchestrator.events import EventName
from issue_orchestrator.models import AgentConfig, Issue, Session, SessionStatus
from issue_orchestrator.observation.observation import SessionObservation, SessionObservationResult
from issue_orchestrator.observation.observer import SessionObserver
from issue_orchestrator.ports import TraceEvent


class StubSessionRunner:
    def session_exists_by_name(self, session_name: str) -> bool:
        return True


class CollectingEventSink:
    def __init__(self) -> None:
        self.events: list[TraceEvent] = []

    def publish(self, event: TraceEvent) -> None:
        self.events.append(event)


class StubLabelAdapter:
    def add_label(self, issue_number: int, label: str) -> None:
        return None

    def remove_label(self, issue_number: int, label: str) -> None:
        return None


class StubPrAdapter:
    def create_pr(self, title: str, body: str, head: str, base: str = "main"):
        raise RuntimeError("create_pr not expected")

    def add_comment(self, issue_or_pr_number: int, body: str) -> str:
        return "comment-id"


class StubGitAdapter:
    def push(self, worktree: Path, remote: str = "origin", force_with_lease: bool = True,
             set_upstream: bool = True, skip_hooks: bool = False):
        raise RuntimeError("push not expected")

    def get_current_branch(self, worktree: Path) -> str | None:
        return "issue-1"

    def has_uncommitted_changes(self, worktree: Path) -> bool:
        return False


def _make_session(worktree: Path, timeout_minutes: int = 1) -> Session:
    issue = Issue(number=1, title="Timeout test", labels=["test"])
    agent_config = AgentConfig(
        prompt_path=worktree / "prompt.md",
        worktree_base=worktree.parent,
        timeout_minutes=timeout_minutes,
    )
    issue_key = FakeIssueKey(name="1")
    session_key = SessionKey(issue=issue_key, task=TaskKind.CODE)
    return Session(
        key=session_key,
        issue=issue,
        agent_config=agent_config,
        terminal_id="issue-1",
        worktree_path=worktree,
        branch_name="issue-1",
        started_at=datetime.now() - timedelta(minutes=timeout_minutes + 1),
    )


def test_timeout_observation_and_decision(tmp_path):
    worktree = tmp_path / "worktree"
    worktree.mkdir(parents=True, exist_ok=True)
    (worktree / ".issue-orchestrator").mkdir(parents=True, exist_ok=True)

    events = CollectingEventSink()
    config = Config()
    config.session_no_output_seconds = 120
    config.session_no_output_tail_lines = 50
    config.session_no_output_max_bytes = 10000
    config.session_no_output_repeat_seconds = 120
    observer = SessionObserver(
        config=config,
        events=events,
        session_runner=StubSessionRunner(),
        repository_host=None,
    )

    session = _make_session(worktree)
    observation = observer.observe_session(session)
    assert observation.observation == SessionObservation.TIMED_OUT
    assert observation.session_exists is True

    completion_processor = CompletionProcessor(
        label_adapter=StubLabelAdapter(),
        pr_adapter=StubPrAdapter(),
        git_adapter=StubGitAdapter(),
    )
    controller = SessionController(completion_processor=completion_processor, events=events)

    decision = controller.decide_outcome(
        observation=SessionObservationResult(
            observation=SessionObservation.TIMED_OUT,
            session_exists=True,
            runtime_minutes=2,
            timeout_minutes=1,
        ),
        worktree_path=worktree,
        issue_number=session.issue.number,
        issue_title=session.issue.title,
        session_name=session.terminal_id,
    )

    assert decision.status == SessionStatus.TIMED_OUT
    assert any(e.name == EventName.SESSION_NO_COMPLETION_RECORD for e in events.events)
