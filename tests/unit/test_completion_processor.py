"""Tests for CompletionProcessor - verifies orchestrator applies labels correctly.

These tests verify that when an agent writes a completion.json, the orchestrator
(via CompletionProcessor) correctly executes the requested actions including
label application.

Architecture reminder:
- Agent writes completion.json with requested_actions
- Orchestrator reads it and calls CompletionProcessor.process()
- CompletionProcessor executes actions via adapters (labels, PR, comments)
"""

import json
import pytest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, MagicMock, patch

from issue_orchestrator.models import (
    CompletionRecord,
    CompletionOutcome,
    RequestedAction,
    COMPLETION_RECORD_PATH,
)
from issue_orchestrator.control.completion_processor import (
    CompletionProcessor,
    ProcessingResult,
    LabelAdapter,
    PRAdapter,
    GitAdapter,
    PRInfo,
    PushResult,
)
from issue_orchestrator.domain.events import EventBus, SessionEvent


# ==================== Fixtures ====================


@pytest.fixture
def mock_label_adapter():
    """Mock adapter for label operations."""
    adapter = Mock(spec=LabelAdapter)
    adapter.add_label = Mock()
    adapter.remove_label = Mock()
    return adapter


@pytest.fixture
def mock_pr_adapter():
    """Mock adapter for PR operations."""
    adapter = Mock(spec=PRAdapter)
    adapter.create_pr = Mock(return_value=PRInfo(number=42, url="https://github.com/owner/repo/pull/42"))
    adapter.add_comment = Mock(return_value="comment-id")
    return adapter


@pytest.fixture
def mock_git_adapter():
    """Mock adapter for git operations."""
    adapter = Mock(spec=GitAdapter)
    adapter.push = Mock(return_value=PushResult(success=True, message="Pushed"))
    adapter.get_current_branch = Mock(return_value="issue-123")
    adapter.has_uncommitted_changes = Mock(return_value=False)
    return adapter


@pytest.fixture
def event_bus():
    """EventBus for capturing emitted events."""
    return EventBus()


@pytest.fixture
def processor(mock_label_adapter, mock_pr_adapter, mock_git_adapter, event_bus):
    """Create a CompletionProcessor with mocked adapters."""
    return CompletionProcessor(
        label_adapter=mock_label_adapter,
        pr_adapter=mock_pr_adapter,
        git_adapter=mock_git_adapter,
        event_bus=event_bus,
        label_config={
            "blocked": "blocked",
            "needs_human": "needs-human",
            "code_reviewed": "code-reviewed",
            "needs_rework": "needs-rework",
            "code_review": "needs-code-review",
            "in_progress": "in-progress",
        },
    )


def make_record(
    outcome: CompletionOutcome,
    requested_actions: list[RequestedAction],
    summary: str = "Test summary",
    **kwargs
) -> CompletionRecord:
    """Helper to create CompletionRecord with required fields."""
    return CompletionRecord(
        session_id="test-session",
        timestamp=datetime.now().isoformat(),
        outcome=outcome,
        summary=summary,
        requested_actions=requested_actions,
        **kwargs,
    )


@pytest.fixture
def worktree_with_completion(tmp_path):
    """Factory for creating worktrees with completion records."""
    def _create(record: CompletionRecord) -> Path:
        worktree = tmp_path / "worktree"
        worktree.mkdir(parents=True, exist_ok=True)
        record_dir = worktree / ".issue-orchestrator"
        record_dir.mkdir(parents=True, exist_ok=True)
        record_path = record_dir / "completion.json"
        record_path.write_text(json.dumps(record.to_dict()))
        return worktree
    return _create


# ==================== Unit Tests ====================


class TestCompletionProcessorLabelActions:
    """Tests for label-related actions from completion records."""

    def test_completed_outcome_does_not_add_labels_directly(
        self, processor, mock_label_adapter, worktree_with_completion
    ):
        """Completed outcome requests push/PR, no label actions needed."""
        record = make_record(
            outcome=CompletionOutcome.COMPLETED,
            requested_actions=[
                RequestedAction.PUSH_BRANCH,
                RequestedAction.CREATE_PR,
            ],
            summary="Implemented feature",
            implementation="Added the feature",
        )
        worktree = worktree_with_completion(record)

        result = processor.process(worktree, issue_number=123, issue_title="Test Issue")

        assert result.success
        mock_label_adapter.add_label.assert_not_called()
        mock_label_adapter.remove_label.assert_not_called()

    def test_blocked_outcome_adds_blocked_label(
        self, processor, mock_label_adapter, worktree_with_completion
    ):
        """Blocked outcome should add the blocked label."""
        record = make_record(
            outcome=CompletionOutcome.BLOCKED,
            requested_actions=[
                RequestedAction.ADD_BLOCKED_LABEL,
                RequestedAction.POST_COMMENT,
            ],
            summary="Blocked on dependency",
            blocked_reason="Waiting for API access",
        )
        worktree = worktree_with_completion(record)

        result = processor.process(worktree, issue_number=123, issue_title="Test Issue")

        assert result.success
        mock_label_adapter.add_label.assert_called_once_with(123, "blocked")

    def test_needs_human_outcome_adds_needs_human_label(
        self, processor, mock_label_adapter, worktree_with_completion
    ):
        """Needs-human outcome should add the needs-human label."""
        record = make_record(
            outcome=CompletionOutcome.NEEDS_HUMAN,
            requested_actions=[
                RequestedAction.ADD_NEEDS_HUMAN_LABEL,
                RequestedAction.POST_COMMENT,
            ],
            summary="Need clarification",
            question="Should we use Redis or Memcached?",
        )
        worktree = worktree_with_completion(record)

        result = processor.process(worktree, issue_number=123, issue_title="Test Issue")

        assert result.success
        mock_label_adapter.add_label.assert_called_once_with(123, "needs-human")

    def test_review_approved_adds_code_reviewed_removes_review_label(
        self, processor, mock_label_adapter, worktree_with_completion
    ):
        """Approved review should add code-reviewed and remove needs-code-review."""
        record = make_record(
            outcome=CompletionOutcome.REVIEW_APPROVED,
            requested_actions=[
                RequestedAction.ADD_CODE_REVIEWED_LABEL,
                RequestedAction.REMOVE_CODE_REVIEW_LABEL,
                RequestedAction.POST_COMMENT,
            ],
            summary="LGTM",
            review_summary="Code looks good",
        )
        worktree = worktree_with_completion(record)

        result = processor.process(worktree, issue_number=42, issue_title="PR Title")

        assert result.success
        mock_label_adapter.add_label.assert_called_once_with(42, "code-reviewed")
        mock_label_adapter.remove_label.assert_called_once_with(42, "needs-code-review")

    def test_review_changes_requested_adds_needs_rework_removes_review_label(
        self, processor, mock_label_adapter, worktree_with_completion
    ):
        """Changes requested should add needs-rework and remove needs-code-review."""
        record = make_record(
            outcome=CompletionOutcome.REVIEW_CHANGES_REQUESTED,
            requested_actions=[
                RequestedAction.ADD_NEEDS_REWORK_LABEL,
                RequestedAction.REMOVE_CODE_REVIEW_LABEL,
                RequestedAction.POST_COMMENT,
            ],
            summary="Need fixes",
            review_issues="Missing error handling",
        )
        worktree = worktree_with_completion(record)

        result = processor.process(worktree, issue_number=42, issue_title="PR Title")

        assert result.success
        mock_label_adapter.add_label.assert_called_once_with(42, "needs-rework")
        mock_label_adapter.remove_label.assert_called_once_with(42, "needs-code-review")


class TestCompletionProcessorPRActions:
    """Tests for PR-related actions from completion records."""

    def test_create_pr_action_calls_adapter(
        self, processor, mock_pr_adapter, worktree_with_completion
    ):
        """CREATE_PR action should create a PR via adapter."""
        record = make_record(
            outcome=CompletionOutcome.COMPLETED,
            requested_actions=[
                RequestedAction.PUSH_BRANCH,
                RequestedAction.CREATE_PR,
            ],
            summary="Implemented feature",
            implementation="Added the feature",
        )
        worktree = worktree_with_completion(record)

        result = processor.process(worktree, issue_number=123, issue_title="Add feature")

        assert result.success
        assert result.pr_url == "https://github.com/owner/repo/pull/42"
        mock_pr_adapter.create_pr.assert_called_once()
        call_args = mock_pr_adapter.create_pr.call_args
        assert call_args.kwargs["title"] == "#123: Add feature"
        assert call_args.kwargs["head"] == "issue-123"

    def test_post_comment_action_with_body(
        self, processor, mock_pr_adapter, worktree_with_completion
    ):
        """POST_COMMENT action should post comment via adapter."""
        record = make_record(
            outcome=CompletionOutcome.BLOCKED,
            requested_actions=[
                RequestedAction.ADD_BLOCKED_LABEL,
                RequestedAction.POST_COMMENT,
            ],
            summary="Blocked",
            blocked_reason="API unavailable",
            comment_body="## Blocked\n\nWaiting for API access.",
        )
        worktree = worktree_with_completion(record)

        result = processor.process(worktree, issue_number=123, issue_title="Test Issue")

        assert result.success
        mock_pr_adapter.add_comment.assert_called_once_with(
            123, "## Blocked\n\nWaiting for API access."
        )


class TestCompletionProcessorGitActions:
    """Tests for git-related actions from completion records."""

    def test_push_branch_action_calls_adapter(
        self, processor, mock_git_adapter, worktree_with_completion
    ):
        """PUSH_BRANCH action should push via adapter."""
        record = make_record(
            outcome=CompletionOutcome.COMPLETED,
            requested_actions=[RequestedAction.PUSH_BRANCH],
            summary="Done",
        )
        worktree = worktree_with_completion(record)

        result = processor.process(worktree, issue_number=123, issue_title="Test")

        assert result.success
        mock_git_adapter.push.assert_called_once_with(worktree)

    def test_push_failure_is_recorded(
        self, processor, mock_git_adapter, worktree_with_completion
    ):
        """Failed push should be recorded in result."""
        mock_git_adapter.push.return_value = PushResult(
            success=False, message="Remote rejected"
        )
        record = make_record(
            outcome=CompletionOutcome.COMPLETED,
            requested_actions=[RequestedAction.PUSH_BRANCH],
            summary="Done",
        )
        worktree = worktree_with_completion(record)

        result = processor.process(worktree, issue_number=123, issue_title="Test")

        assert not result.success
        assert any("Push failed" in err for err in result.errors)


class TestCompletionProcessorValidation:
    """Tests for validation logic."""

    def test_no_completion_record_returns_failure(self, processor, tmp_path):
        """Missing completion record should return failure."""
        worktree = tmp_path / "empty-worktree"
        worktree.mkdir()

        result = processor.process(worktree, issue_number=123, issue_title="Test")

        assert not result.success
        assert "no completion record found" in result.message.lower()

    def test_invalid_json_returns_failure(self, processor, tmp_path):
        """Invalid JSON in completion record should return failure."""
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        record_dir = worktree / ".issue-orchestrator"
        record_dir.mkdir()
        (record_dir / "completion.json").write_text("not valid json{")

        result = processor.process(worktree, issue_number=123, issue_title="Test")

        assert not result.success

    def test_protected_branch_push_rejected(
        self, processor, mock_git_adapter, worktree_with_completion
    ):
        """Push to main branch should be rejected."""
        mock_git_adapter.get_current_branch.return_value = "main"
        record = make_record(
            outcome=CompletionOutcome.COMPLETED,
            requested_actions=[RequestedAction.PUSH_BRANCH],
            summary="Done",
        )
        worktree = worktree_with_completion(record)

        result = processor.process(worktree, issue_number=123, issue_title="Test")

        assert not result.success
        assert "protected branch" in result.message.lower()
        mock_git_adapter.push.assert_not_called()


class TestCompletionProcessorEvents:
    """Tests for event emission during processing."""

    def test_successful_completion_emits_event(
        self, processor, event_bus, worktree_with_completion
    ):
        """Successful processing should emit completed event."""
        record = make_record(
            outcome=CompletionOutcome.COMPLETED,
            requested_actions=[RequestedAction.PUSH_BRANCH],
            summary="Done",
        )
        worktree = worktree_with_completion(record)

        # Subscribe to capture events
        events_received = []
        event_bus.subscribe(
            SessionEvent.COMPLETED,
            lambda e: events_received.append(e)
        )

        processor.process(worktree, issue_number=123, issue_title="Test")

        assert len(events_received) == 1
        assert events_received[0].entity_id == 123

    def test_failed_processing_emits_failed_event(
        self, processor, event_bus, mock_git_adapter, worktree_with_completion
    ):
        """Failed processing should emit failed event."""
        mock_git_adapter.push.return_value = PushResult(
            success=False, message="Rejected"
        )
        record = make_record(
            outcome=CompletionOutcome.COMPLETED,
            requested_actions=[RequestedAction.PUSH_BRANCH],
            summary="Done",
        )
        worktree = worktree_with_completion(record)

        events_received = []
        event_bus.subscribe(
            SessionEvent.FAILED,
            lambda e: events_received.append(e)
        )

        processor.process(worktree, issue_number=123, issue_title="Test")

        assert len(events_received) == 1


class TestCompletionProcessorAuditLogging:
    """Tests for audit logging of all actions."""

    def test_all_actions_logged(
        self, processor, worktree_with_completion, caplog
    ):
        """All executed actions should be logged for audit."""
        record = make_record(
            outcome=CompletionOutcome.REVIEW_APPROVED,
            requested_actions=[
                RequestedAction.ADD_CODE_REVIEWED_LABEL,
                RequestedAction.REMOVE_CODE_REVIEW_LABEL,
                RequestedAction.POST_COMMENT,
            ],
            summary="LGTM",
            review_summary="Looks good",
            comment_body="Approved!",
        )
        worktree = worktree_with_completion(record)

        import logging
        with caplog.at_level(logging.INFO):
            processor.process(worktree, issue_number=42, issue_title="Test PR")

        # Verify key actions are logged
        log_text = caplog.text
        assert "Executing action: add_code_reviewed_label" in log_text
        assert "Executing action: remove_code_review_label" in log_text
        assert "Processing completion for #42" in log_text

    def test_result_includes_actions_taken(
        self, processor, worktree_with_completion
    ):
        """Result should list all actions taken for audit."""
        record = make_record(
            outcome=CompletionOutcome.BLOCKED,
            requested_actions=[
                RequestedAction.ADD_BLOCKED_LABEL,
            ],
            summary="Blocked",
        )
        worktree = worktree_with_completion(record)

        result = processor.process(worktree, issue_number=123, issue_title="Test")

        assert result.actions_taken is not None
        assert any("blocked" in action.lower() for action in result.actions_taken)
