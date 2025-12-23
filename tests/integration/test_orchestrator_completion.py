"""Integration tests for orchestrator-CompletionProcessor flow.

These tests verify that when a session exits, the orchestrator:
1. Reads completion.json from the worktree
2. Executes requested actions (push, PR, labels)
3. Emits appropriate trace events
4. Returns correct SessionStatus

This is the critical integration that connects agents to the orchestrator.
"""

import json
import pytest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, MagicMock, patch

from issue_orchestrator.orchestrator import Orchestrator
from issue_orchestrator.config import Config, DangerousConfig
from issue_orchestrator.models import (
    Issue,
    Session,
    SessionStatus,
    CompletionRecord,
    CompletionOutcome,
    RequestedAction,
    AgentConfig,
)
from issue_orchestrator.ports import TraceEvent


def make_completion_record(
    outcome: CompletionOutcome,
    requested_actions: list[RequestedAction],
    **kwargs
) -> CompletionRecord:
    """Helper to create a CompletionRecord with required fields."""
    return CompletionRecord(
        session_id="test-session",
        timestamp=datetime.now().isoformat(),
        outcome=outcome,
        summary="Test completion",
        requested_actions=requested_actions,
        **kwargs,
    )


def write_completion_to_worktree(worktree: Path, record: CompletionRecord) -> None:
    """Write completion record to worktree."""
    record_dir = worktree / ".issue-orchestrator"
    record_dir.mkdir(parents=True, exist_ok=True)
    record_path = record_dir / "completion.json"
    record_path.write_text(json.dumps(record.to_dict()))


@pytest.fixture
def test_config(tmp_path):
    """Create a real Config for testing."""
    config = Config()
    config.repo = "owner/repo"
    config.repo_root = tmp_path
    config.code_reviewed_label = "code-reviewed"
    config.code_review_label = "needs-code-review"
    config.ui_mode = "tmux"
    config.max_concurrent_sessions = 1
    config.dangerous = DangerousConfig(skip_verification=True, allow_unsupported_agents=True)
    return config


@pytest.fixture
def mock_repository_host():
    """Mock GitHub adapter."""
    adapter = MagicMock()
    adapter.add_label = Mock()
    adapter.remove_label = Mock()
    adapter.create_pr = Mock(return_value=MagicMock(number=42, url="https://github.com/owner/repo/pull/42"))
    adapter.add_comment = Mock()
    return adapter


@pytest.fixture
def orchestrator(test_config, mock_repository_host):
    """Create an Orchestrator with mocked dependencies.

    Note: The conftest.py autouse fixture will inject MockEventSink automatically.
    Access it via orchestrator._mock_event_sink.
    """
    with patch('issue_orchestrator.orchestrator.SessionObserver'):
        orch = Orchestrator(
            config=test_config,
            _repository_host=mock_repository_host,
        )
        return orch


@pytest.fixture
def session_with_worktree(tmp_path):
    """Factory to create a Session with a real worktree."""
    def _create(issue_number: int = 123) -> Session:
        worktree = tmp_path / f"worktree-{issue_number}"
        worktree.mkdir(parents=True, exist_ok=True)

        # Initialize git repo in worktree properly
        import subprocess
        subprocess.run(["git", "init"], cwd=worktree, capture_output=True, check=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=worktree, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=worktree, capture_output=True)
        # Create initial commit so we can have a branch
        (worktree / "README.md").write_text("# Test")
        subprocess.run(["git", "add", "."], cwd=worktree, capture_output=True)
        subprocess.run(["git", "commit", "-m", "initial"], cwd=worktree, capture_output=True)
        subprocess.run(["git", "checkout", "-b", f"issue-{issue_number}"], cwd=worktree, capture_output=True)

        issue = Issue(
            number=issue_number,
            title="Test Issue",
            labels=["test"],
        )
        agent_config = AgentConfig(
            prompt_path=worktree / "prompt.md",
            worktree_base=tmp_path,
            timeout_minutes=30,
        )
        return Session(
            issue=issue,
            tmux_session_name=f"issue-{issue_number}",
            branch_name=f"issue-{issue_number}",
            worktree_path=worktree,
            agent_config=agent_config,
        )
    return _create


class TestProcessSessionExit:
    """Tests for _process_session_exit method."""

    def test_no_completion_record_returns_failed(
        self, orchestrator, session_with_worktree
    ):
        """Session without completion.json should return FAILED status."""
        session = session_with_worktree()

        status, result = orchestrator._process_session_exit(session)

        assert status == SessionStatus.FAILED
        assert result is None
        # Should emit completion.missing event (via conftest's MockEventSink)
        events = orchestrator._mock_event_sink.events
        assert any(e.name == "completion.missing" for e in events)

    def test_completed_outcome_returns_completed_status(
        self, orchestrator, session_with_worktree
    ):
        """COMPLETED outcome should return COMPLETED status."""
        session = session_with_worktree()
        record = make_completion_record(
            outcome=CompletionOutcome.COMPLETED,
            requested_actions=[RequestedAction.PUSH_BRANCH],
            implementation="Added feature",
        )
        write_completion_to_worktree(session.worktree_path, record)

        status, result = orchestrator._process_session_exit(session)

        assert status == SessionStatus.COMPLETED
        # Should emit completion.processing event
        events = orchestrator._mock_event_sink.events
        event_names = [e.name for e in events]
        assert "completion.processing" in event_names

    def test_blocked_outcome_returns_blocked_status(
        self, orchestrator, session_with_worktree, mock_repository_host
    ):
        """BLOCKED outcome should return BLOCKED status and add label."""
        session = session_with_worktree()
        record = make_completion_record(
            outcome=CompletionOutcome.BLOCKED,
            requested_actions=[RequestedAction.ADD_BLOCKED_LABEL],
            blocked_reason="Waiting for API access",
        )
        write_completion_to_worktree(session.worktree_path, record)

        status, result = orchestrator._process_session_exit(session)

        assert status == SessionStatus.BLOCKED
        # Should have called add_label with blocked
        mock_repository_host.add_label.assert_called_once_with(session.issue.number, "blocked")

    def test_needs_human_outcome_returns_needs_human_status(
        self, orchestrator, session_with_worktree, mock_repository_host
    ):
        """NEEDS_HUMAN outcome should return NEEDS_HUMAN status and add label."""
        session = session_with_worktree()
        record = make_completion_record(
            outcome=CompletionOutcome.NEEDS_HUMAN,
            requested_actions=[RequestedAction.ADD_NEEDS_HUMAN_LABEL],
            question="Which approach should I use?",
        )
        write_completion_to_worktree(session.worktree_path, record)

        status, result = orchestrator._process_session_exit(session)

        assert status == SessionStatus.NEEDS_HUMAN
        mock_repository_host.add_label.assert_called_once_with(session.issue.number, "needs-human")

    def test_review_approved_returns_completed_status(
        self, orchestrator, session_with_worktree, mock_repository_host
    ):
        """REVIEW_APPROVED outcome should return COMPLETED status and update labels."""
        session = session_with_worktree()
        record = make_completion_record(
            outcome=CompletionOutcome.REVIEW_APPROVED,
            requested_actions=[
                RequestedAction.ADD_CODE_REVIEWED_LABEL,
                RequestedAction.REMOVE_CODE_REVIEW_LABEL,
            ],
            review_summary="LGTM",
        )
        write_completion_to_worktree(session.worktree_path, record)

        status, result = orchestrator._process_session_exit(session)

        assert status == SessionStatus.COMPLETED
        mock_repository_host.add_label.assert_called_once_with(session.issue.number, "code-reviewed")
        mock_repository_host.remove_label.assert_called_once_with(session.issue.number, "needs-code-review")

    def test_review_changes_requested_returns_completed_and_adds_rework_label(
        self, orchestrator, session_with_worktree, mock_repository_host
    ):
        """REVIEW_CHANGES_REQUESTED should return COMPLETED and add needs-rework label."""
        session = session_with_worktree()
        record = make_completion_record(
            outcome=CompletionOutcome.REVIEW_CHANGES_REQUESTED,
            requested_actions=[
                RequestedAction.ADD_NEEDS_REWORK_LABEL,
                RequestedAction.REMOVE_CODE_REVIEW_LABEL,
            ],
            review_issues="Missing error handling",
        )
        write_completion_to_worktree(session.worktree_path, record)

        status, result = orchestrator._process_session_exit(session)

        assert status == SessionStatus.COMPLETED  # Review session completed its job
        mock_repository_host.add_label.assert_called_once_with(session.issue.number, "needs-rework")
        mock_repository_host.remove_label.assert_called_once_with(session.issue.number, "needs-code-review")


class TestEventEmission:
    """Tests for trace event emission during completion processing."""

    def test_emits_completion_processing_event(
        self, orchestrator, session_with_worktree
    ):
        """Should emit completion.processing event with action details."""
        session = session_with_worktree()
        record = make_completion_record(
            outcome=CompletionOutcome.COMPLETED,
            requested_actions=[RequestedAction.PUSH_BRANCH, RequestedAction.CREATE_PR],
        )
        write_completion_to_worktree(session.worktree_path, record)

        orchestrator._process_session_exit(session)

        events = orchestrator._mock_event_sink.events
        processing_events = [e for e in events if e.name == "completion.processing"]
        assert len(processing_events) == 1
        assert processing_events[0].data["issue_number"] == session.issue.number
        assert processing_events[0].data["outcome"] == "completed"
        assert "push_branch" in processing_events[0].data["requested_actions"]

    def test_emits_completion_succeeded_on_success(
        self, orchestrator, session_with_worktree
    ):
        """Should emit completion.succeeded event on successful processing."""
        session = session_with_worktree()
        record = make_completion_record(
            outcome=CompletionOutcome.BLOCKED,
            requested_actions=[RequestedAction.ADD_BLOCKED_LABEL],
        )
        write_completion_to_worktree(session.worktree_path, record)

        orchestrator._process_session_exit(session)

        events = orchestrator._mock_event_sink.events
        succeeded_events = [e for e in events if e.name == "completion.succeeded"]
        assert len(succeeded_events) == 1
        assert succeeded_events[0].data["status"] == "blocked"

    def test_emits_completion_missing_when_no_record(
        self, orchestrator, session_with_worktree
    ):
        """Should emit completion.missing event when no completion.json."""
        session = session_with_worktree()
        # Don't write completion record

        orchestrator._process_session_exit(session)

        events = orchestrator._mock_event_sink.events
        missing_events = [e for e in events if e.name == "completion.missing"]
        assert len(missing_events) == 1
        assert missing_events[0].data["issue_number"] == session.issue.number
