"""Unit tests for CompletionHandler state machine updates."""

from types import SimpleNamespace
from pathlib import Path

from issue_orchestrator.config import Config
from issue_orchestrator.control.completion_handler import CompletionHandler
from issue_orchestrator.domain.issue_key import FakeIssueKey
from issue_orchestrator.domain.session_key import SessionKey, TaskKind
from issue_orchestrator.domain.state_machines.issue_machine import IssueStateMachine, IssueState
from issue_orchestrator.models import AgentConfig, Issue, Session, SessionStatus
from issue_orchestrator.ports import NullEventSink


def test_pr_created_ignored_when_issue_already_pr_pending(tmp_path: Path) -> None:
    """Ignore duplicate pr_created transitions for issue sessions."""
    config = Config()
    config.repo = "owner/repo"

    issue = Issue(number=1, title="Test issue", labels=["agent:test"], repo="owner/repo")
    issue_key = FakeIssueKey("TEST-1")
    session_key = SessionKey(issue=issue_key, task=TaskKind.CODE)
    agent_config = AgentConfig(prompt_path=tmp_path / "prompt.txt", worktree_base=tmp_path)
    session = Session(
        key=session_key,
        issue=issue,
        agent_config=agent_config,
        terminal_id="issue-1",
        worktree_path=tmp_path / "worktree",
        branch_name="issue-1",
    )

    issue_machine = IssueStateMachine(issue, initial_state=IssueState.PR_PENDING)

    repository_host = SimpleNamespace(
        get_prs_for_branch=lambda _branch: [SimpleNamespace(url="http://pr", number=1)],
    )

    handler = CompletionHandler(
        config=config,
        events=NullEventSink(),
        repository_host=repository_host,
        get_issue_machine_fn=lambda _issue: issue_machine,
        get_session_machine_fn=lambda _terminal_id: None,
        get_review_machine_fn=lambda _pr_number: None,
    )

    handler.process_completion(session, SessionStatus.COMPLETED)

    assert issue_machine.get_state() == IssueState.PR_PENDING
