"""Tests for SessionController - the core decision logic.

These tests verify the observer/controller separation:
- Given observations + completion records -> correct decisions

No external mocking needed - pure logic tests.
"""

import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch

from datetime import datetime
from issue_orchestrator.control.session_controller import SessionController, SessionDecision
from issue_orchestrator.control.completion_processor import CompletionProcessor
from issue_orchestrator.observation.observation import SessionObservation, SessionObservationResult
from issue_orchestrator.models import SessionStatus, CompletionRecord, CompletionOutcome, RequestedAction
from issue_orchestrator.ports import NullEventSink


def make_record(outcome: CompletionOutcome, **kwargs) -> CompletionRecord:
    """Helper to create a CompletionRecord with required fields."""
    return CompletionRecord(
        session_id=kwargs.get("session_id", "test"),
        timestamp=datetime.now().isoformat(),
        outcome=outcome,
        summary=kwargs.get("summary", "Test summary"),
        requested_actions=kwargs.get("requested_actions", []),
        implementation=kwargs.get("implementation"),
        problems=kwargs.get("problems"),
        blocked_reason=kwargs.get("blocked_reason"),
        blocked_by=kwargs.get("blocked_by"),
        attempted=kwargs.get("attempted"),
        when_unblocked=kwargs.get("when_unblocked"),
        question=kwargs.get("question"),
        context=kwargs.get("context"),
        options=kwargs.get("options"),
        default_action=kwargs.get("default_action"),
        review_summary=kwargs.get("review_summary"),
        review_issues=kwargs.get("review_issues"),
        risk_level=kwargs.get("risk_level"),
        checks_passed=kwargs.get("checks_passed"),
        checks_needed=kwargs.get("checks_needed"),
        comment_body=kwargs.get("comment_body"),
        validation_record_path=kwargs.get("validation_record_path"),
    )


class MockCompletionProcessor:
    """Fake completion processor for testing decisions without I/O."""

    def __init__(self):
        self.completion_record: CompletionRecord | None = None
        self.process_result = MagicMock()
        self.process_result.success = True
        self.process_result.pr_url = "https://github.com/test/repo/pull/1"

    def read_completion_record(self, worktree_path: Path) -> CompletionRecord | None:
        return self.completion_record

    def process(self, worktree_path: Path, issue_number: int, issue_title: str, pr_number: int | None = None):
        return self.process_result


class TestSessionControllerRunning:
    """Tests for RUNNING observation."""

    def test_running_session_returns_running_status(self):
        """A running session should stay running."""
        processor = MockCompletionProcessor()
        controller = SessionController(processor, NullEventSink())

        observation = SessionObservationResult.running(runtime_minutes=5.0)

        decision = controller.decide_outcome(
            observation=observation,
            worktree_path=Path("/tmp/test"),
            issue_number=123,
            issue_title="Test Issue",
            session_name="issue-123",
        )

        assert decision.status == SessionStatus.RUNNING
        assert not decision.completion_processed


class TestSessionControllerTerminated:
    """Tests for TERMINATED observation (session exited)."""

    def test_terminated_without_completion_record_is_failed(self):
        """Session that exits without completion.json = FAILED."""
        processor = MockCompletionProcessor()
        processor.completion_record = None  # No completion record
        controller = SessionController(processor, NullEventSink())

        observation = SessionObservationResult.terminated(runtime_minutes=10.0)

        decision = controller.decide_outcome(
            observation=observation,
            worktree_path=Path("/tmp/test"),
            issue_number=123,
            issue_title="Test Issue",
            session_name="issue-123",
        )

        assert decision.status == SessionStatus.FAILED
        assert not decision.completion_processed
        assert "without completion record" in decision.reason

    def test_terminated_with_completed_record_is_completed(self):
        """Session that exits with completed outcome = COMPLETED."""
        processor = MockCompletionProcessor()
        processor.completion_record = make_record(
            CompletionOutcome.COMPLETED,
            summary="Done",
            requested_actions=[RequestedAction.CREATE_PR],
        )
        controller = SessionController(processor, NullEventSink())

        observation = SessionObservationResult.terminated(runtime_minutes=10.0)

        decision = controller.decide_outcome(
            observation=observation,
            worktree_path=Path("/tmp/test"),
            issue_number=123,
            issue_title="Test Issue",
            session_name="issue-123",
        )

        assert decision.status == SessionStatus.COMPLETED
        assert decision.completion_processed
        assert not decision.recovered_from_timeout

    def test_terminated_with_blocked_record_is_blocked(self):
        """Session that exits with blocked outcome = BLOCKED."""
        processor = MockCompletionProcessor()
        processor.completion_record = make_record(
            CompletionOutcome.BLOCKED,
            summary="Blocked by dependency",
            blocked_reason="Waiting for API",
        )
        controller = SessionController(processor, NullEventSink())

        observation = SessionObservationResult.terminated(runtime_minutes=10.0)

        decision = controller.decide_outcome(
            observation=observation,
            worktree_path=Path("/tmp/test"),
            issue_number=123,
            issue_title="Test Issue",
            session_name="issue-123",
        )

        assert decision.status == SessionStatus.BLOCKED
        assert decision.completion_processed

    def test_terminated_with_needs_human_record_is_needs_human(self):
        """Session that exits with needs_human outcome = NEEDS_HUMAN."""
        processor = MockCompletionProcessor()
        processor.completion_record = make_record(
            CompletionOutcome.NEEDS_HUMAN,
            summary="Need clarification",
            question="What API should I use?",
        )
        controller = SessionController(processor, NullEventSink())

        observation = SessionObservationResult.terminated(runtime_minutes=10.0)

        decision = controller.decide_outcome(
            observation=observation,
            worktree_path=Path("/tmp/test"),
            issue_number=123,
            issue_title="Test Issue",
            session_name="issue-123",
        )

        assert decision.status == SessionStatus.NEEDS_HUMAN
        assert decision.completion_processed


class TestSessionControllerTimeout:
    """Tests for TIMED_OUT observation - the key recovery case."""

    def test_timeout_without_completion_record_is_timed_out(self):
        """Session that times out without completion.json = TIMED_OUT."""
        processor = MockCompletionProcessor()
        processor.completion_record = None  # No completion record
        controller = SessionController(processor, NullEventSink())

        observation = SessionObservationResult.timed_out(
            runtime_minutes=60.0,
            timeout_minutes=45,
            session_exists=True,
        )

        decision = controller.decide_outcome(
            observation=observation,
            worktree_path=Path("/tmp/test"),
            issue_number=123,
            issue_title="Test Issue",
            session_name="issue-123",
        )

        assert decision.status == SessionStatus.TIMED_OUT
        assert not decision.completion_processed
        assert not decision.recovered_from_timeout

    def test_timeout_with_completed_record_is_recovered(self):
        """Session that times out WITH completion.json = COMPLETED (recovered)."""
        processor = MockCompletionProcessor()
        processor.completion_record = make_record(
            CompletionOutcome.COMPLETED,
            summary="Done",
            requested_actions=[RequestedAction.CREATE_PR],
        )
        controller = SessionController(processor, NullEventSink())

        observation = SessionObservationResult.timed_out(
            runtime_minutes=60.0,
            timeout_minutes=45,
            session_exists=True,
        )

        decision = controller.decide_outcome(
            observation=observation,
            worktree_path=Path("/tmp/test"),
            issue_number=123,
            issue_title="Test Issue",
            session_name="issue-123",
        )

        # Key assertion: even though timeout occurred, work was recovered
        assert decision.status == SessionStatus.COMPLETED
        assert decision.completion_processed
        assert decision.recovered_from_timeout

    def test_timeout_with_blocked_record_is_blocked_recovered(self):
        """Session that times out with blocked outcome = BLOCKED (recovered)."""
        processor = MockCompletionProcessor()
        processor.completion_record = make_record(
            CompletionOutcome.BLOCKED,
            summary="Blocked",
            blocked_reason="External dependency",
        )
        controller = SessionController(processor, NullEventSink())

        observation = SessionObservationResult.timed_out(
            runtime_minutes=60.0,
            timeout_minutes=45,
            session_exists=True,
        )

        decision = controller.decide_outcome(
            observation=observation,
            worktree_path=Path("/tmp/test"),
            issue_number=123,
            issue_title="Test Issue",
            session_name="issue-123",
        )

        assert decision.status == SessionStatus.BLOCKED
        assert decision.completion_processed
        assert decision.recovered_from_timeout


class TestSessionControllerReviewOutcomes:
    """Tests for review session outcomes."""

    def test_review_approved_is_completed(self):
        """Review approved outcome maps to COMPLETED status."""
        processor = MockCompletionProcessor()
        processor.completion_record = make_record(
            CompletionOutcome.REVIEW_APPROVED,
            summary="LGTM",
        )
        controller = SessionController(processor, NullEventSink())

        observation = SessionObservationResult.terminated(runtime_minutes=5.0)

        decision = controller.decide_outcome(
            observation=observation,
            worktree_path=Path("/tmp/test"),
            issue_number=123,
            issue_title="Test Issue",
            session_name="review-456",
        )

        assert decision.status == SessionStatus.COMPLETED

    def test_review_changes_requested_is_completed(self):
        """Review changes_requested outcome maps to COMPLETED status."""
        processor = MockCompletionProcessor()
        processor.completion_record = make_record(
            CompletionOutcome.REVIEW_CHANGES_REQUESTED,
            summary="Needs work",
        )
        controller = SessionController(processor, NullEventSink())

        observation = SessionObservationResult.terminated(runtime_minutes=5.0)

        decision = controller.decide_outcome(
            observation=observation,
            worktree_path=Path("/tmp/test"),
            issue_number=123,
            issue_title="Test Issue",
            session_name="review-456",
        )

        # Review session completed its job (even if changes requested)
        assert decision.status == SessionStatus.COMPLETED
