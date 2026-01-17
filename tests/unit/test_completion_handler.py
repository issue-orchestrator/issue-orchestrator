"""Comprehensive behavior-centric tests for CompletionHandler.

These tests verify the behaviors of CompletionHandler which handles:
1. Session completion state machine updates
2. Event emission for trace events
3. History entry creation
4. Cleanup decision logic
5. Label/comment action generation

Tests focus on invariant outcomes, state transitions, and business rules
rather than implementation details.
"""

import pytest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional
from unittest.mock import MagicMock, Mock

from issue_orchestrator.infra.config import (
    Config,
    CleanupConfig,
    CleanupWithTriage,
    CleanupWithoutTriage,
)
from issue_orchestrator.control.completion_handler import CompletionHandler, CompletionResult
from issue_orchestrator.control.actions import (
    AddLabelAction,
    RemoveLabelAction,
    AddCommentAction,
    ActionType,
)
from issue_orchestrator.domain.issue_key import FakeIssueKey
from issue_orchestrator.domain.session_key import SessionKey, TaskKind
from issue_orchestrator.domain.state_machines.issue_machine import IssueStateMachine, IssueState
from issue_orchestrator.domain.state_machines.session_machine import SessionStateMachine, SessionState
from issue_orchestrator.domain.state_machines.review_machine import ReviewStateMachine, ReviewState
from issue_orchestrator.domain.models import AgentConfig, Issue, Session, SessionStatus, PendingCleanup
from issue_orchestrator.ports import NullEventSink, InMemoryEventSink, TraceEvent
from issue_orchestrator.ports.session_output import SessionOutput
from issue_orchestrator.events import EventName
from issue_orchestrator.infra import labels


# =============================================================================
# Test Fixtures
# =============================================================================


@pytest.fixture
def config() -> Config:
    """Create a minimal valid config."""
    cfg = Config()
    cfg.repo = "owner/repo"
    return cfg


@pytest.fixture
def tmp_worktree(tmp_path: Path) -> Path:
    """Create a temporary worktree directory."""
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    return worktree


@pytest.fixture
def agent_config(tmp_path: Path) -> AgentConfig:
    """Create a minimal agent config."""
    return AgentConfig(
        prompt_path=tmp_path / "prompt.txt",
        timeout_minutes=45,
    )


def make_issue(number: int = 1, title: str = "Test issue", labels: list[str] | None = None, repo: str = "owner/repo") -> Issue:
    """Create a test issue."""
    return Issue(
        number=number,
        title=title,
        labels=labels or ["agent:test"],
        repo=repo,
    )


def create_test_session(
    issue: Issue,
    agent_config: AgentConfig,
    worktree_path: Path,
    terminal_id: str = "issue-1",
    task_kind: TaskKind = TaskKind.CODE,
) -> Session:
    """Create a test session."""
    issue_key = FakeIssueKey(str(issue.number))
    session_key = SessionKey(issue=issue_key, task=task_kind)
    return Session(
        key=session_key,
        issue=issue,
        agent_config=agent_config,
        terminal_id=terminal_id,
        worktree_path=worktree_path,
        branch_name=f"issue-{issue.number}",
    )


def make_repository_host(prs: list[Any] | None = None, pr_info: Any | None = None) -> SimpleNamespace:
    """Create a mock repository host."""
    return SimpleNamespace(
        get_prs_for_branch=lambda _branch: prs or [],
        get_pr=lambda _pr_number: pr_info,
        set_pr_draft=Mock(),
    )


def make_handler(
    config: Config,
    events: Any | None = None,
    repository_host: Any | None = None,
    issue_machine: Any | None = None,
    session_machine: Any | None = None,
    review_machine: Any | None = None,
    session_output: SessionOutput | None = None,
) -> CompletionHandler:
    """Create a CompletionHandler with sensible defaults."""
    # Use explicit None check - InMemoryEventSink with 0 events is falsy
    return CompletionHandler(
        config=config,
        events=events if events is not None else NullEventSink(),
        repository_host=repository_host if repository_host is not None else make_repository_host(),
        get_issue_machine_fn=lambda _issue: issue_machine,
        get_session_machine_fn=lambda _terminal_id: session_machine,
        get_review_machine_fn=lambda _pr_number: review_machine,
        session_output=session_output if session_output is not None else Mock(spec=SessionOutput),
    )


# =============================================================================
# Test: History Entry Creation
# =============================================================================


class TestHistoryEntryCreation:
    """Tests for history entry creation on session completion."""

    def test_completed_session_creates_history_with_pr_url(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        """Completed session with PR records the PR URL in history."""
        issue = make_issue()
        session = create_test_session(issue, agent_config, tmp_worktree)
        pr_url = "https://github.com/owner/repo/pull/42"

        repository_host = make_repository_host(
            prs=[SimpleNamespace(url=pr_url, number=42, labels=[])]
        )
        handler = make_handler(config, repository_host=repository_host)

        result = handler.process_completion(session, SessionStatus.COMPLETED)

        assert result.history_entry.pr_url == pr_url
        assert result.history_entry.status == "completed"
        assert result.history_entry.issue_number == issue.number

    def test_failed_session_creates_history_without_pr_url(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        """Failed session has no PR URL in history."""
        issue = make_issue()
        session = create_test_session(issue, agent_config, tmp_worktree)
        handler = make_handler(config)

        result = handler.process_completion(session, SessionStatus.FAILED)

        assert result.history_entry.pr_url is None
        assert result.history_entry.status == "failed"
        assert "without PR" in result.history_entry.status_reason

    def test_timed_out_session_records_timeout_info(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        """Timed out session records timeout information in history."""
        agent_config.timeout_minutes = 30
        issue = make_issue()
        session = create_test_session(issue, agent_config, tmp_worktree)
        handler = make_handler(config)

        result = handler.process_completion(session, SessionStatus.TIMED_OUT)

        assert result.history_entry.status == "timed_out"
        assert "30" in result.history_entry.status_reason
        assert "timeout" in result.history_entry.status_reason.lower()

    def test_blocked_session_records_blocked_reason(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        """Blocked session records blocked status in history."""
        issue = make_issue()
        session = create_test_session(issue, agent_config, tmp_worktree)
        handler = make_handler(config)

        result = handler.process_completion(session, SessionStatus.BLOCKED)

        assert result.history_entry.status == "blocked"
        assert "blocked" in result.history_entry.status_reason.lower()

    def test_needs_human_session_records_reason(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        """Needs-human session records human intervention reason."""
        issue = make_issue()
        session = create_test_session(issue, agent_config, tmp_worktree)
        handler = make_handler(config)

        result = handler.process_completion(session, SessionStatus.NEEDS_HUMAN)

        assert result.history_entry.status == "needs_human"
        assert "human" in result.history_entry.status_reason.lower()

    def test_completed_with_critical_errors_records_failed_status(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        """Agent reported completed but push/PR failed shows FAILED in history."""
        from issue_orchestrator.control.completion_processor import ERROR_PREFIX_PUSH

        issue = make_issue()
        session = create_test_session(issue, agent_config, tmp_worktree)
        handler = make_handler(config)

        # Agent said completed, but push failed
        processing_errors = [f"{ERROR_PREFIX_PUSH}: Push failed: branch too large"]

        result = handler.process_completion(
            session,
            SessionStatus.COMPLETED,
            processing_errors=processing_errors,
        )

        # History should show FAILED, not COMPLETED
        assert result.history_entry.status == "failed"
        assert "push" in result.history_entry.status_reason.lower() or "pr" in result.history_entry.status_reason.lower()

    def test_completed_without_errors_records_completed_status(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        """Successful completion with no errors shows COMPLETED in history."""
        issue = make_issue()
        session = create_test_session(issue, agent_config, tmp_worktree)

        repository_host = make_repository_host(
            prs=[SimpleNamespace(url="http://pr/42", number=42, labels=[])]
        )
        handler = make_handler(config, repository_host=repository_host)

        # No processing errors - successful completion
        result = handler.process_completion(session, SessionStatus.COMPLETED)

        assert result.history_entry.status == "completed"
        assert "PR created" in result.history_entry.status_reason


# =============================================================================
# Test: Event Emission
# =============================================================================


class TestEventEmission:
    """Tests for trace event emission during completion processing."""

    def test_completed_session_emits_session_completed_event(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        """Completed session emits SESSION_COMPLETED event with PR info."""
        events = InMemoryEventSink()
        issue = make_issue()
        session = create_test_session(issue, agent_config, tmp_worktree)
        pr_url = "https://github.com/owner/repo/pull/42"

        repository_host = make_repository_host(
            prs=[SimpleNamespace(url=pr_url, number=42, labels=[])]
        )
        handler = make_handler(config, events=events, repository_host=repository_host)

        handler.process_completion(session, SessionStatus.COMPLETED)

        assert events.has_event(str(EventName.SESSION_COMPLETED))
        event = events.last_event(str(EventName.SESSION_COMPLETED))
        assert event is not None
        assert event.data["issue_number"] == issue.number
        assert event.data["pr_url"] == pr_url

    def test_failed_session_emits_session_failed_event(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        """Failed session emits SESSION_FAILED event with error info."""
        events = InMemoryEventSink()
        issue = make_issue()
        session = create_test_session(issue, agent_config, tmp_worktree)
        handler = make_handler(config, events=events)

        handler.process_completion(session, SessionStatus.FAILED)

        assert events.has_event(str(EventName.SESSION_FAILED))
        event = events.last_event(str(EventName.SESSION_FAILED))
        assert event is not None
        assert event.data["issue_number"] == issue.number
        assert "error" in event.data

    def test_timed_out_session_emits_session_failed_event(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        """Timed out session emits SESSION_FAILED event."""
        events = InMemoryEventSink()
        issue = make_issue()
        session = create_test_session(issue, agent_config, tmp_worktree)
        handler = make_handler(config, events=events)

        handler.process_completion(session, SessionStatus.TIMED_OUT)

        assert events.has_event(str(EventName.SESSION_FAILED))

    def test_blocked_session_emits_issue_blocked_event(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        """Blocked session emits ISSUE_BLOCKED event."""
        events = InMemoryEventSink()
        issue = make_issue()
        session = create_test_session(issue, agent_config, tmp_worktree)
        handler = make_handler(config, events=events)

        handler.process_completion(session, SessionStatus.BLOCKED)

        assert events.has_event(str(EventName.ISSUE_BLOCKED))
        event = events.last_event(str(EventName.ISSUE_BLOCKED))
        assert event is not None
        assert event.data["issue_number"] == issue.number

    def test_needs_human_session_emits_issue_needs_human_event(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        """Needs-human session emits ISSUE_NEEDS_HUMAN event."""
        events = InMemoryEventSink()
        issue = make_issue()
        session = create_test_session(issue, agent_config, tmp_worktree)
        handler = make_handler(config, events=events)

        handler.process_completion(session, SessionStatus.NEEDS_HUMAN)

        assert events.has_event(str(EventName.ISSUE_NEEDS_HUMAN))

    def test_pr_view_changed_event_emitted_for_completed_with_pr(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        """PR_VIEW_CHANGED event is emitted when a PR is discovered."""
        events = InMemoryEventSink()
        issue = make_issue()
        session = create_test_session(issue, agent_config, tmp_worktree)
        pr_url = "https://github.com/owner/repo/pull/42"

        repository_host = make_repository_host(
            prs=[SimpleNamespace(url=pr_url, number=42, labels=["some-label"])]
        )
        handler = make_handler(config, events=events, repository_host=repository_host)

        handler.process_completion(session, SessionStatus.COMPLETED)

        assert events.has_event(str(EventName.PR_VIEW_CHANGED))
        event = events.last_event(str(EventName.PR_VIEW_CHANGED))
        assert event is not None
        assert event.data["pr_number"] == 42
        assert event.data["labels"] == ["some-label"]


# =============================================================================
# Test: State Machine Transitions
# =============================================================================


class TestStateMachineTransitions:
    """Tests for state machine transitions during completion processing."""

    def test_completed_issue_session_transitions_to_pr_pending(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        """Issue session with PR transitions issue state to PR_PENDING."""
        issue = make_issue()
        issue_machine = IssueStateMachine(issue, initial_state=IssueState.IN_PROGRESS)
        session = create_test_session(issue, agent_config, tmp_worktree, terminal_id="issue-1")

        repository_host = make_repository_host(
            prs=[SimpleNamespace(url="http://pr", number=42, labels=[])]
        )
        handler = make_handler(
            config, repository_host=repository_host, issue_machine=issue_machine
        )

        handler.process_completion(session, SessionStatus.COMPLETED)

        assert issue_machine.get_state() == IssueState.PR_PENDING

    def test_pr_created_ignored_when_issue_already_pr_pending(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        """Duplicate pr_created transitions are ignored for issue sessions."""
        issue = make_issue()
        issue_machine = IssueStateMachine(issue, initial_state=IssueState.PR_PENDING)
        session = create_test_session(issue, agent_config, tmp_worktree, terminal_id="issue-1")

        repository_host = make_repository_host(
            prs=[SimpleNamespace(url="http://pr", number=42, labels=[])]
        )
        handler = make_handler(
            config, repository_host=repository_host, issue_machine=issue_machine
        )

        handler.process_completion(session, SessionStatus.COMPLETED)

        # State should remain PR_PENDING (no transition)
        assert issue_machine.get_state() == IssueState.PR_PENDING

    def test_review_session_does_not_trigger_pr_created(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        """Review sessions don't trigger pr_created on the issue machine."""
        issue = make_issue()
        issue_machine = IssueStateMachine(issue, initial_state=IssueState.IN_PROGRESS)
        session = create_test_session(
            issue, agent_config, tmp_worktree,
            terminal_id="review-42",
            task_kind=TaskKind.REVIEW,
        )

        repository_host = make_repository_host(
            prs=[SimpleNamespace(url="http://pr", number=42, labels=[])]
        )
        handler = make_handler(
            config, repository_host=repository_host, issue_machine=issue_machine
        )

        handler.process_completion(session, SessionStatus.COMPLETED)

        # Issue state should remain IN_PROGRESS (review sessions don't change it)
        assert issue_machine.get_state() == IssueState.IN_PROGRESS

    def test_blocked_session_transitions_issue_to_blocked(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        """Blocked session transitions issue to BLOCKED state."""
        issue = make_issue()
        issue_machine = IssueStateMachine(issue, initial_state=IssueState.IN_PROGRESS)
        session = create_test_session(issue, agent_config, tmp_worktree)

        handler = make_handler(config, issue_machine=issue_machine)

        handler.process_completion(session, SessionStatus.BLOCKED)

        assert issue_machine.get_state() == IssueState.BLOCKED

    def test_needs_human_session_transitions_issue_to_needs_human(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        """Needs-human session transitions issue to NEEDS_HUMAN state."""
        issue = make_issue()
        issue_machine = IssueStateMachine(issue, initial_state=IssueState.IN_PROGRESS)
        session = create_test_session(issue, agent_config, tmp_worktree)

        handler = make_handler(config, issue_machine=issue_machine)

        handler.process_completion(session, SessionStatus.NEEDS_HUMAN)

        assert issue_machine.get_state() == IssueState.NEEDS_HUMAN

    def test_session_machine_transitions_on_completion(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        """Session state machine transitions to COMPLETED on success."""
        issue = make_issue()
        session = create_test_session(issue, agent_config, tmp_worktree, terminal_id="issue-1")
        session_machine = SessionStateMachine(
            session_id="issue-1",
            issue_number=issue.number,
            initial_state=SessionState.RUNNING,
        )

        handler = make_handler(config, session_machine=session_machine)

        handler.process_completion(session, SessionStatus.COMPLETED)

        assert session_machine.get_state() == SessionState.COMPLETED

    def test_session_machine_transitions_on_failure(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        """Session state machine transitions to FAILED on failure."""
        issue = make_issue()
        session = create_test_session(issue, agent_config, tmp_worktree, terminal_id="issue-1")
        session_machine = SessionStateMachine(
            session_id="issue-1",
            issue_number=issue.number,
            initial_state=SessionState.RUNNING,
        )

        handler = make_handler(config, session_machine=session_machine)

        handler.process_completion(session, SessionStatus.FAILED)

        assert session_machine.get_state() == SessionState.FAILED

    def test_session_machine_transitions_on_timeout(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        """Session state machine transitions to TIMED_OUT on timeout."""
        issue = make_issue()
        session = create_test_session(issue, agent_config, tmp_worktree, terminal_id="issue-1")
        session_machine = SessionStateMachine(
            session_id="issue-1",
            issue_number=issue.number,
            initial_state=SessionState.RUNNING,
        )

        handler = make_handler(config, session_machine=session_machine)

        handler.process_completion(session, SessionStatus.TIMED_OUT)

        assert session_machine.get_state() == SessionState.TIMED_OUT


# =============================================================================
# Test: Review Session Machine Transitions
# =============================================================================


class TestReviewMachineTransitions:
    """Tests for review state machine transitions on review session completion."""

    def test_review_session_with_code_reviewed_label_approves_review(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        """Review session with code-reviewed label approves the review."""
        config.code_reviewed_label = "code-reviewed"
        issue = make_issue()
        session = create_test_session(
            issue, agent_config, tmp_worktree,
            terminal_id="review-42",
            task_kind=TaskKind.REVIEW,
        )
        review_machine = ReviewStateMachine(
            pr_number=42,
            issue_number=issue.number,
            initial_state=ReviewState.IN_REVIEW,
        )

        pr_info = SimpleNamespace(number=42, labels=["code-reviewed"], url="http://pr", draft=True)
        repository_host = make_repository_host(
            prs=[pr_info],
            pr_info=pr_info,
        )
        handler = make_handler(
            config,
            repository_host=repository_host,
            review_machine=review_machine,
        )

        handler.process_completion(session, SessionStatus.COMPLETED)

        assert review_machine.get_state() == ReviewState.APPROVED
        repository_host.set_pr_draft.assert_called_once_with(42, False)

    def test_review_session_with_needs_rework_label_requests_changes_and_queues_rework(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        """Review session with needs-rework label requests changes and queues rework."""
        config.label_needs_rework = "needs-rework"
        issue = make_issue()
        session = create_test_session(
            issue, agent_config, tmp_worktree,
            terminal_id="review-42",
            task_kind=TaskKind.REVIEW,
        )
        review_machine = ReviewStateMachine(
            pr_number=42,
            issue_number=issue.number,
            initial_state=ReviewState.IN_REVIEW,
        )

        pr_info = SimpleNamespace(number=42, labels=["needs-rework"], url="http://pr")
        repository_host = make_repository_host(
            prs=[pr_info],
            pr_info=pr_info,
        )
        handler = make_handler(
            config,
            repository_host=repository_host,
            review_machine=review_machine,
        )

        handler.process_completion(session, SessionStatus.COMPLETED)

        # After request_changes and queue_rework
        assert review_machine.get_state() == ReviewState.REWORK_PENDING

    def test_review_session_no_transition_when_machine_not_found(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        """No error when review machine is not found for a review session."""
        config.code_reviewed_label = "code-reviewed"
        issue = make_issue()
        session = create_test_session(
            issue, agent_config, tmp_worktree,
            terminal_id="review-42",
            task_kind=TaskKind.REVIEW,
        )

        pr_info = SimpleNamespace(number=42, labels=["code-reviewed"], url="http://pr")
        repository_host = make_repository_host(
            prs=[pr_info],
            pr_info=pr_info,
        )
        # No review machine provided (None)
        handler = make_handler(config, repository_host=repository_host)

        # Should not raise
        result = handler.process_completion(session, SessionStatus.COMPLETED)
        assert result is not None


# =============================================================================
# Test: PR Detection and Handling
# =============================================================================


class TestPRDetection:
    """Tests for PR detection from completed sessions."""

    def test_completed_session_detects_pr_from_branch(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        """Completed session fetches and returns PR info from branch."""
        issue = make_issue()
        session = create_test_session(issue, agent_config, tmp_worktree)

        repository_host = make_repository_host(
            prs=[SimpleNamespace(url="http://pr/123", number=123, labels=[])]
        )
        handler = make_handler(config, repository_host=repository_host)

        result = handler.process_completion(session, SessionStatus.COMPLETED)

        assert result.pr_url == "http://pr/123"
        assert result.pr_number == 123

    def test_completed_session_without_pr_returns_none(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        """Completed session without PR returns None for pr_url and pr_number."""
        issue = make_issue()
        session = create_test_session(issue, agent_config, tmp_worktree)

        repository_host = make_repository_host(prs=[])
        handler = make_handler(config, repository_host=repository_host)

        result = handler.process_completion(session, SessionStatus.COMPLETED)

        assert result.pr_url is None
        assert result.pr_number is None

    def test_failed_session_does_not_fetch_prs(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        """Failed sessions don't fetch PRs."""
        issue = make_issue()
        session = create_test_session(issue, agent_config, tmp_worktree)

        fetch_called = []
        repository_host = SimpleNamespace(
            get_prs_for_branch=lambda _: (fetch_called.append(True), [])[1],
            get_pr=lambda _: None,
        )
        handler = make_handler(config, repository_host=repository_host)

        result = handler.process_completion(session, SessionStatus.FAILED)

        assert not fetch_called
        assert result.pr_url is None

    def test_multiple_prs_uses_first_one(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        """When multiple PRs exist for branch, the first one is used."""
        issue = make_issue()
        session = create_test_session(issue, agent_config, tmp_worktree)

        repository_host = make_repository_host(prs=[
            SimpleNamespace(url="http://pr/1", number=1, labels=[]),
            SimpleNamespace(url="http://pr/2", number=2, labels=[]),
        ])
        handler = make_handler(config, repository_host=repository_host)

        result = handler.process_completion(session, SessionStatus.COMPLETED)

        assert result.pr_url == "http://pr/1"
        assert result.pr_number == 1


# =============================================================================
# Test: Cleanup Strategy Determination
# =============================================================================


class TestCleanupStrategy:
    """Tests for cleanup strategy determination."""

    def test_completed_work_session_with_triage_defers_cleanup(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        """Work session with triage agent configured defers cleanup."""
        config.triage_review_agent = "agent:triage"
        config.cleanup.with_triage.close_ai_session_tabs = True
        issue = make_issue()
        session = create_test_session(issue, agent_config, tmp_worktree, terminal_id="issue-1")

        repository_host = make_repository_host(
            prs=[SimpleNamespace(url="http://pr", number=42, labels=[])]
        )
        handler = make_handler(config, repository_host=repository_host)

        result = handler.process_completion(session, SessionStatus.COMPLETED)

        assert result.should_defer_cleanup is True
        assert result.pending_cleanup is not None
        assert result.pending_cleanup.pr_number == 42

    def test_completed_work_session_without_triage_uses_code_review_config(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        """Work session without triage uses code_review cleanup config."""
        config.triage_review_agent = None
        config.code_review_agent = "agent:reviewer"
        config.cleanup.without_triage.wait_for_code_review = True
        config.cleanup.without_triage.close_ai_session_tabs = True
        issue = make_issue()
        session = create_test_session(issue, agent_config, tmp_worktree, terminal_id="issue-1")

        repository_host = make_repository_host(
            prs=[SimpleNamespace(url="http://pr", number=42, labels=[])]
        )
        handler = make_handler(config, repository_host=repository_host)

        result = handler.process_completion(session, SessionStatus.COMPLETED)

        assert result.should_defer_cleanup is True

    def test_review_session_does_not_defer_cleanup(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        """Review sessions don't create pending cleanups."""
        config.triage_review_agent = "agent:triage"
        issue = make_issue()
        session = create_test_session(
            issue, agent_config, tmp_worktree,
            terminal_id="review-42",
            task_kind=TaskKind.REVIEW,
        )

        repository_host = make_repository_host(
            prs=[SimpleNamespace(url="http://pr", number=42, labels=[])]
        )
        handler = make_handler(config, repository_host=repository_host)

        result = handler.process_completion(session, SessionStatus.COMPLETED)

        assert result.should_defer_cleanup is False
        assert result.pending_cleanup is None

    def test_rework_session_does_not_defer_cleanup(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        """Rework sessions don't create pending cleanups."""
        config.triage_review_agent = "agent:triage"
        issue = make_issue()
        session = create_test_session(
            issue, agent_config, tmp_worktree,
            terminal_id="rework-42",
            task_kind=TaskKind.REWORK,
        )

        repository_host = make_repository_host(
            prs=[SimpleNamespace(url="http://pr", number=42, labels=[])]
        )
        handler = make_handler(config, repository_host=repository_host)

        result = handler.process_completion(session, SessionStatus.COMPLETED)

        assert result.should_defer_cleanup is False

    def test_failed_session_does_not_defer_cleanup(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        """Failed sessions don't create pending cleanups."""
        config.triage_review_agent = "agent:triage"
        issue = make_issue()
        session = create_test_session(issue, agent_config, tmp_worktree, terminal_id="issue-1")

        handler = make_handler(config)

        result = handler.process_completion(session, SessionStatus.FAILED)

        assert result.should_defer_cleanup is False
        assert result.pending_cleanup is None

    def test_pending_cleanup_contains_correct_info(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        """PendingCleanup object contains all required information."""
        config.triage_review_agent = "agent:triage"
        config.cleanup.with_triage.close_ai_session_tabs = True
        issue = make_issue(number=123)
        session = create_test_session(
            issue, agent_config, tmp_worktree,
            terminal_id="issue-123",
        )

        pr_url = "http://github.com/owner/repo/pull/456"
        repository_host = make_repository_host(
            prs=[SimpleNamespace(url=pr_url, number=456, labels=[])]
        )
        handler = make_handler(config, repository_host=repository_host)

        result = handler.process_completion(session, SessionStatus.COMPLETED)

        cleanup = result.pending_cleanup
        assert cleanup is not None
        assert cleanup.issue_number == 123
        assert cleanup.pr_number == 456
        assert cleanup.pr_url == pr_url
        assert cleanup.terminal_session_name == "issue-123"
        assert cleanup.branch_name == "issue-123"


# =============================================================================
# Test: Review Queue Decision
# =============================================================================


class TestReviewQueueDecision:
    """Tests for code review queueing decisions."""

    def test_completed_with_pr_and_code_review_agent_queues_review(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        """Session with PR and code_review_agent configured queues review."""
        config.code_review_agent = "agent:reviewer"
        issue = make_issue()
        session = create_test_session(issue, agent_config, tmp_worktree, terminal_id="issue-1")

        repository_host = make_repository_host(
            prs=[SimpleNamespace(url="http://pr", number=42, labels=[])]
        )
        handler = make_handler(config, repository_host=repository_host)

        result = handler.process_completion(session, SessionStatus.COMPLETED)

        assert result.should_queue_review is True

    def test_completed_without_code_review_agent_does_not_queue(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        """Session without code_review_agent does not queue review."""
        config.code_review_agent = None
        issue = make_issue()
        session = create_test_session(issue, agent_config, tmp_worktree, terminal_id="issue-1")

        repository_host = make_repository_host(
            prs=[SimpleNamespace(url="http://pr", number=42, labels=[])]
        )
        handler = make_handler(config, repository_host=repository_host)

        result = handler.process_completion(session, SessionStatus.COMPLETED)

        assert result.should_queue_review is False

    def test_skip_review_agent_config_prevents_queue(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        """Agent with skip_review=True does not queue review."""
        config.code_review_agent = "agent:reviewer"
        agent_config.skip_review = True
        issue = make_issue()
        session = create_test_session(issue, agent_config, tmp_worktree, terminal_id="issue-1")

        repository_host = make_repository_host(
            prs=[SimpleNamespace(url="http://pr", number=42, labels=[])]
        )
        handler = make_handler(config, repository_host=repository_host)

        result = handler.process_completion(session, SessionStatus.COMPLETED)

        assert result.should_queue_review is False

    def test_review_session_does_not_queue_another_review(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        """Review sessions don't queue another review."""
        config.code_review_agent = "agent:reviewer"
        issue = make_issue()
        session = create_test_session(
            issue, agent_config, tmp_worktree,
            terminal_id="review-42",
            task_kind=TaskKind.REVIEW,
        )

        repository_host = make_repository_host(
            prs=[SimpleNamespace(url="http://pr", number=42, labels=[])]
        )
        handler = make_handler(config, repository_host=repository_host)

        result = handler.process_completion(session, SessionStatus.COMPLETED)

        assert result.should_queue_review is False

    def test_completed_without_pr_does_not_queue_review(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        """Session completed without PR does not queue review."""
        config.code_review_agent = "agent:reviewer"
        issue = make_issue()
        session = create_test_session(issue, agent_config, tmp_worktree, terminal_id="issue-1")

        repository_host = make_repository_host(prs=[])
        handler = make_handler(config, repository_host=repository_host)

        result = handler.process_completion(session, SessionStatus.COMPLETED)

        assert result.should_queue_review is False


# =============================================================================
# Test: Label Action Generation
# =============================================================================


class TestLabelActionGeneration:
    """Tests for label/comment action generation on completion."""

    def test_timeout_generates_blocked_failed_label_and_comment(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        """Timeout generates blocked-failed label, comment, and removes in-progress."""
        agent_config.timeout_minutes = 30
        issue = make_issue(number=123)
        session = create_test_session(issue, agent_config, tmp_worktree)
        handler = make_handler(config)

        result = handler.process_completion(session, SessionStatus.TIMED_OUT)

        actions = result.actions
        assert len(actions) == 3

        # Find each action type
        add_label = next((a for a in actions if isinstance(a, AddLabelAction)), None)
        comment = next((a for a in actions if isinstance(a, AddCommentAction)), None)
        remove_label = next((a for a in actions if isinstance(a, RemoveLabelAction)), None)

        assert add_label is not None
        assert add_label.label == labels.BLOCKED_FAILED
        assert add_label.issue_number == 123

        assert comment is not None
        assert "Timed Out" in comment.comment
        assert "30" in comment.comment  # timeout minutes

        assert remove_label is not None
        assert remove_label.label == config.get_label_in_progress()

    def test_failure_generates_blocked_needs_human_label_and_comment(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        """Failure (no completion) generates blocked-needs-human label for investigation."""
        issue = make_issue(number=456)
        session = create_test_session(issue, agent_config, tmp_worktree)
        handler = make_handler(config)

        result = handler.process_completion(session, SessionStatus.FAILED)

        actions = result.actions

        add_label = next((a for a in actions if isinstance(a, AddLabelAction)), None)
        comment = next((a for a in actions if isinstance(a, AddCommentAction)), None)
        remove_label = next((a for a in actions if isinstance(a, RemoveLabelAction)), None)

        assert add_label is not None
        assert add_label.label == labels.BLOCKED_NEEDS_HUMAN  # Needs human investigation

        assert comment is not None
        assert "Investigation" in comment.comment  # Explains agent-done was not called

        assert remove_label is not None
        assert remove_label.label == config.get_label_in_progress()

    def test_review_failure_adds_comment_only(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        """Review failure should not mark issue blocked-failed."""
        issue = make_issue(number=789)
        session = create_test_session(
            issue,
            agent_config,
            tmp_worktree,
            terminal_id="review-123",
            task_kind=TaskKind.REVIEW,
        )
        handler = make_handler(config)

        result = handler.process_completion(session, SessionStatus.FAILED)

        actions = result.actions
        assert any(isinstance(a, AddCommentAction) for a in actions)
        assert not any(
            isinstance(a, AddLabelAction) and a.label == labels.BLOCKED_FAILED
            for a in actions
        )
        assert not any(isinstance(a, RemoveLabelAction) for a in actions)

    def test_completed_only_removes_in_progress_label(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        """Completed session only removes in-progress label."""
        issue = make_issue()
        session = create_test_session(issue, agent_config, tmp_worktree)
        handler = make_handler(config)

        result = handler.process_completion(session, SessionStatus.COMPLETED)

        actions = result.actions
        assert len(actions) == 1

        remove_label = actions[0]
        assert isinstance(remove_label, RemoveLabelAction)
        assert remove_label.label == config.get_label_in_progress()

    def test_blocked_does_not_generate_actions(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        """Blocked session does not generate label actions (keeps in-progress)."""
        issue = make_issue()
        session = create_test_session(issue, agent_config, tmp_worktree)
        handler = make_handler(config)

        result = handler.process_completion(session, SessionStatus.BLOCKED)

        # BLOCKED maintains ownership via in-progress label - no actions generated
        assert len(result.actions) == 0

    def test_needs_human_does_not_generate_actions(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        """Needs-human session does not generate label actions (keeps in-progress)."""
        issue = make_issue()
        session = create_test_session(issue, agent_config, tmp_worktree)
        handler = make_handler(config)

        result = handler.process_completion(session, SessionStatus.NEEDS_HUMAN)

        # NEEDS_HUMAN maintains ownership via in-progress label - no actions generated
        assert len(result.actions) == 0

    def test_label_prefix_applied_to_in_progress_label(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        """Label prefix is applied to in-progress label removal."""
        config.label_prefix = "bot"
        issue = make_issue()
        session = create_test_session(issue, agent_config, tmp_worktree)
        handler = make_handler(config)

        result = handler.process_completion(session, SessionStatus.COMPLETED)

        remove_label = result.actions[0]
        assert isinstance(remove_label, RemoveLabelAction)
        assert remove_label.label == "bot:in-progress"


# =============================================================================
# Test: Edge Cases and Error Handling
# =============================================================================


class TestEdgeCases:
    """Tests for edge cases and error handling."""

    def test_missing_state_machines_handled_gracefully(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        """Handler works when state machines are not found."""
        issue = make_issue()
        session = create_test_session(issue, agent_config, tmp_worktree)

        # All machine getters return None
        handler = make_handler(config)

        # Should not raise
        result = handler.process_completion(session, SessionStatus.COMPLETED)
        assert result is not None
        assert result.history_entry is not None

    def test_review_machine_lookup_handles_exception(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        """Review machine lookup exceptions are caught."""
        config.code_reviewed_label = "code-reviewed"
        issue = make_issue()
        session = create_test_session(
            issue, agent_config, tmp_worktree,
            terminal_id="review-42",
            task_kind=TaskKind.REVIEW,
        )

        def failing_get_pr(_: int) -> None:
            raise Exception("GitHub API error")

        repository_host = SimpleNamespace(
            get_prs_for_branch=lambda _: [],
            get_pr=failing_get_pr,
        )
        handler = make_handler(config, repository_host=repository_host)

        # Should not raise
        result = handler.process_completion(session, SessionStatus.COMPLETED)
        assert result is not None

    def test_non_review_terminal_id_patterns(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        """Terminal IDs that don't match review pattern are handled."""
        issue = make_issue()
        session = create_test_session(
            issue, agent_config, tmp_worktree,
            terminal_id="some-other-pattern",
        )

        handler = make_handler(config)

        # Should not raise
        result = handler.process_completion(session, SessionStatus.COMPLETED)
        assert result is not None

    def test_completion_result_has_all_fields(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        """CompletionResult contains all expected fields."""
        issue = make_issue()
        session = create_test_session(issue, agent_config, tmp_worktree)

        repository_host = make_repository_host(
            prs=[SimpleNamespace(url="http://pr", number=42, labels=[])]
        )
        handler = make_handler(config, repository_host=repository_host)

        result = handler.process_completion(session, SessionStatus.COMPLETED)

        # Verify all fields are present and have sensible values
        assert result.history_entry is not None
        assert result.pr_url == "http://pr"
        assert result.pr_number == 42
        assert isinstance(result.should_defer_cleanup, bool)
        assert isinstance(result.should_queue_review, bool)
        # pending_cleanup can be None
        assert isinstance(result.actions, tuple)


# =============================================================================
# Test: Integration of Multiple Behaviors
# =============================================================================


class TestIntegrationBehaviors:
    """Tests that verify multiple behaviors work together correctly."""

    def test_successful_completion_updates_all_states_and_returns_actions(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        """Successful completion updates machines, emits events, and returns actions."""
        config.code_review_agent = "agent:reviewer"
        config.triage_review_agent = "agent:triage"
        config.cleanup.with_triage.close_ai_session_tabs = True

        events = InMemoryEventSink()
        issue = make_issue()
        issue_machine = IssueStateMachine(issue, initial_state=IssueState.IN_PROGRESS)
        session = create_test_session(issue, agent_config, tmp_worktree, terminal_id="issue-1")
        session_machine = SessionStateMachine(
            session_id="issue-1",
            issue_number=issue.number,
            initial_state=SessionState.RUNNING,
        )

        repository_host = make_repository_host(
            prs=[SimpleNamespace(url="http://pr", number=42, labels=[])]
        )
        handler = make_handler(
            config,
            events=events,
            repository_host=repository_host,
            issue_machine=issue_machine,
            session_machine=session_machine,
        )

        result = handler.process_completion(session, SessionStatus.COMPLETED)

        # State machines updated
        assert issue_machine.get_state() == IssueState.PR_PENDING
        assert session_machine.get_state() == SessionState.COMPLETED

        # Events emitted
        assert events.has_event(str(EventName.SESSION_COMPLETED))
        assert events.has_event(str(EventName.PR_VIEW_CHANGED))

        # Actions generated
        assert len(result.actions) == 1
        assert isinstance(result.actions[0], RemoveLabelAction)

        # Cleanup and review decisions
        assert result.should_defer_cleanup is True
        assert result.should_queue_review is True
        assert result.pending_cleanup is not None

    def test_failed_session_comprehensive_behavior(
        self, config: Config, agent_config: AgentConfig, tmp_worktree: Path
    ) -> None:
        """Failed session correctly updates state, emits events, and generates actions."""
        events = InMemoryEventSink()
        issue = make_issue()
        issue_machine = IssueStateMachine(issue, initial_state=IssueState.IN_PROGRESS)
        session = create_test_session(issue, agent_config, tmp_worktree, terminal_id="issue-1")
        session_machine = SessionStateMachine(
            session_id="issue-1",
            issue_number=issue.number,
            initial_state=SessionState.RUNNING,
        )

        handler = make_handler(
            config,
            events=events,
            issue_machine=issue_machine,
            session_machine=session_machine,
        )

        result = handler.process_completion(session, SessionStatus.FAILED)

        # Session failed but issue stays in_progress (failed machine state)
        assert session_machine.get_state() == SessionState.FAILED

        # Events emitted
        assert events.has_event(str(EventName.SESSION_FAILED))

        # Actions include blocked-needs-human label (for investigation) and comment
        add_label = next((a for a in result.actions if isinstance(a, AddLabelAction)), None)
        assert add_label is not None
        assert add_label.label == labels.BLOCKED_NEEDS_HUMAN

        # No cleanup or review queuing
        assert result.should_defer_cleanup is False
        assert result.should_queue_review is False
