"""Tests for SessionController - the core decision logic.

These tests verify the observer/controller separation:
- Given observations + completion records -> correct decisions

No external mocking needed - pure logic tests.
"""

import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch

from datetime import datetime
from issue_orchestrator.events import EventName
from issue_orchestrator.control.session_controller import SessionController, SessionDecision
from issue_orchestrator.control.completion_processor import CompletionProcessor
from issue_orchestrator.observation.observation import SessionObservation, SessionObservationResult
from issue_orchestrator.domain.models import SessionStatus, CompletionRecord, CompletionOutcome, RequestedAction
from issue_orchestrator.ports import NullEventSink
from issue_orchestrator.ports.event_sink import TraceEvent
from issue_orchestrator.execution.session_output_adapter import FileSystemSessionOutput


class StubWorkingCopy:
    """Stub WorkingCopy for SessionController tests.

    SessionController only uses get_head_sha for validation caching.
    These tests don't exercise validation, so we just return a fixed SHA.
    """

    def get_head_sha(self, worktree: Path) -> str | None:
        return "abc1234567890"

    def get_current_branch(self, worktree: Path) -> str | None:
        return "test-branch"


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

    def read_completion_record(self, worktree_path: Path, completion_path: str | None = None) -> CompletionRecord | None:
        return self.completion_record

    def process(self, worktree_path: Path, issue_number: int, issue_title: str, pr_number: int | None = None, completion_path: str | None = None):
        return self.process_result


class RecordingEventSink:
    def __init__(self) -> None:
        self.events: list[TraceEvent] = []

    def publish(self, event: TraceEvent) -> None:
        self.events.append(event)


class TestSessionControllerRunning:
    """Tests for RUNNING observation."""

    def test_running_session_returns_running_status(self):
        """A running session should stay running."""
        processor = MockCompletionProcessor()
        controller = SessionController(
            completion_processor=processor,
            events=NullEventSink(),
            session_output=FileSystemSessionOutput(),
            working_copy=StubWorkingCopy(),
        )

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

    def test_terminated_without_completion_record_is_failed(self, tmp_path: Path):
        """Session that exits without completion.json = FAILED."""
        processor = MockCompletionProcessor()
        processor.completion_record = None  # No completion record
        controller = SessionController(
            completion_processor=processor,
            events=NullEventSink(),
            session_output=FileSystemSessionOutput(),
            working_copy=StubWorkingCopy(),
        )

        observation = SessionObservationResult.terminated(runtime_minutes=10.0)

        decision = controller.decide_outcome(
            observation=observation,
            worktree_path=tmp_path,
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
        controller = SessionController(
            completion_processor=processor,
            events=NullEventSink(),
            session_output=FileSystemSessionOutput(),
            working_copy=StubWorkingCopy(),
        )

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
        controller = SessionController(
            completion_processor=processor,
            events=NullEventSink(),
            session_output=FileSystemSessionOutput(),
            working_copy=StubWorkingCopy(),
        )

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
        assert decision.blocked_reason == "Waiting for API"

    def test_terminated_with_needs_human_record_is_needs_human(self):
        """Session that exits with needs_human outcome = NEEDS_HUMAN."""
        processor = MockCompletionProcessor()
        processor.completion_record = make_record(
            CompletionOutcome.NEEDS_HUMAN,
            summary="Need clarification",
            question="What API should I use?",
        )
        controller = SessionController(
            completion_processor=processor,
            events=NullEventSink(),
            session_output=FileSystemSessionOutput(),
            working_copy=StubWorkingCopy(),
        )

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

    def test_timeout_without_completion_record_is_timed_out(self, tmp_path: Path):
        """Session that times out without completion.json = TIMED_OUT."""
        processor = MockCompletionProcessor()
        processor.completion_record = None  # No completion record
        controller = SessionController(
            completion_processor=processor,
            events=NullEventSink(),
            session_output=FileSystemSessionOutput(),
            working_copy=StubWorkingCopy(),
        )

        observation = SessionObservationResult.timed_out(
            runtime_minutes=60.0,
            timeout_minutes=45,
            session_exists=True,
        )

        decision = controller.decide_outcome(
            observation=observation,
            worktree_path=tmp_path,
            issue_number=123,
            issue_title="Test Issue",
            session_name="issue-123",
        )

        assert decision.status == SessionStatus.TIMED_OUT
        assert not decision.completion_processed
        assert not decision.recovered_from_timeout

    def test_timeout_without_completion_writes_diagnostic(self, tmp_path: Path):
        """Missing completion writes a no-completion diagnostic artifact."""
        processor = MockCompletionProcessor()
        processor.completion_record = None
        controller = SessionController(
            completion_processor=processor,
            events=NullEventSink(),
            session_output=FileSystemSessionOutput(),
            working_copy=StubWorkingCopy(),
        )

        observation = SessionObservationResult.timed_out(
            runtime_minutes=60.0,
            timeout_minutes=45,
            session_exists=True,
        )
        completion_rel_path = ".issue-orchestrator/sessions/coding-1/completion-agent_backend.json"

        decision = controller.decide_outcome(
            observation=observation,
            worktree_path=tmp_path,
            issue_number=123,
            issue_title="Test Issue",
            session_name="issue-123",
            completion_path=completion_rel_path,
        )

        # Diagnostic should be written to the run dir resolved from the
        # completion_path session name ("coding-1"), not the session_name
        # ("issue-123").  _resolve_run_dir prefers completion_session_name.
        run_dir = controller.session_output.find_run_dir(tmp_path, "coding-1")
        assert run_dir is not None
        diagnostic_files = list(run_dir.glob("no-completion-*.json"))
        assert len(diagnostic_files) == 1
        diagnostic = FileSystemSessionOutput()._read_json(diagnostic_files[0])
        assert diagnostic["kind"] == "no-completion-record"
        assert diagnostic["requested_completion_path"] == completion_rel_path
        assert diagnostic["requested_completion_exists"] is False
        assert diagnostic["observation"] == "timed_out"
        assert diagnostic["agent_done_marker_exists"] is False
        assert diagnostic["nearby_completion_candidates"] == []
        assert decision.status == SessionStatus.TIMED_OUT

    def test_timeout_without_completion_records_marker_and_nearby_candidates(self, tmp_path: Path):
        """No-completion diagnostics should capture marker presence and alternate completion files."""
        processor = MockCompletionProcessor()
        processor.completion_record = None
        controller = SessionController(
            completion_processor=processor,
            events=NullEventSink(),
            session_output=FileSystemSessionOutput(),
            working_copy=StubWorkingCopy(),
        )

        (tmp_path / ".agent-done-marker").write_text(
            "agent-done completed called at 2026-03-17T00:00:00\n",
            encoding="utf-8",
        )
        alt_completion = tmp_path / ".issue-orchestrator" / "sessions" / "other-run" / "completion-agent_backend.json"
        alt_completion.parent.mkdir(parents=True, exist_ok=True)
        alt_completion.write_text('{"outcome":"completed"}', encoding="utf-8")

        observation = SessionObservationResult.timed_out(
            runtime_minutes=60.0,
            timeout_minutes=45,
            session_exists=True,
        )
        completion_rel_path = ".issue-orchestrator/sessions/coding-1/completion-agent_backend.json"

        decision = controller.decide_outcome(
            observation=observation,
            worktree_path=tmp_path,
            issue_number=123,
            issue_title="Test Issue",
            session_name="issue-123",
            completion_path=completion_rel_path,
        )

        run_dir = controller.session_output.find_run_dir(tmp_path, "coding-1")
        assert run_dir is not None
        diagnostic_files = list(run_dir.glob("no-completion-*.json"))
        diagnostic = FileSystemSessionOutput()._read_json(diagnostic_files[0])
        assert diagnostic["agent_done_marker_exists"] is True
        assert "agent-done completed" in diagnostic["agent_done_marker_preview"]
        nearby = diagnostic["nearby_completion_candidates"]
        assert len(nearby) == 1
        assert nearby[0]["path"] == str(alt_completion.resolve())
        assert nearby[0]["under_run_dir"] is False
        assert decision.status == SessionStatus.TIMED_OUT

    def test_timeout_with_completed_record_is_recovered(self):
        """Session that times out WITH completion.json = COMPLETED (recovered)."""
        processor = MockCompletionProcessor()
        processor.completion_record = make_record(
            CompletionOutcome.COMPLETED,
            summary="Done",
            requested_actions=[RequestedAction.CREATE_PR],
        )
        controller = SessionController(
            completion_processor=processor,
            events=NullEventSink(),
            session_output=FileSystemSessionOutput(),
            working_copy=StubWorkingCopy(),
        )

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
        controller = SessionController(
            completion_processor=processor,
            events=NullEventSink(),
            session_output=FileSystemSessionOutput(),
            working_copy=StubWorkingCopy(),
        )

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
        assert decision.blocked_reason == "External dependency"


class TestSessionControllerReviewOutcomes:
    """Tests for review session outcomes."""

    def test_review_approved_is_completed(self):
        """Review approved outcome maps to COMPLETED status."""
        processor = MockCompletionProcessor()
        processor.completion_record = make_record(
            CompletionOutcome.REVIEW_APPROVED,
            summary="LGTM",
        )
        controller = SessionController(
            completion_processor=processor,
            events=NullEventSink(),
            session_output=FileSystemSessionOutput(),
            working_copy=StubWorkingCopy(),
        )

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
        controller = SessionController(
            completion_processor=processor,
            events=NullEventSink(),
            session_output=FileSystemSessionOutput(),
            working_copy=StubWorkingCopy(),
        )

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


class MockCommandRunner:
    """Mock command runner for testing validation."""

    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "", timed_out: bool = False):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.timed_out = timed_out
        self.run_calls: list[dict] = []

    def run(self, command, *, cwd=None, env=None, timeout_seconds=None, shell=False):
        """Record the call and return configured result."""
        self.run_calls.append({
            "command": command,
            "cwd": cwd,
            "timeout_seconds": timeout_seconds,
            "shell": shell,
        })
        # Return a result-like object
        from types import SimpleNamespace
        return SimpleNamespace(
            returncode=self.returncode,
            stdout=self.stdout,
            stderr=self.stderr,
            timed_out=self.timed_out,
        )


class MockWorkingCopy:
    """Mock WorkingCopy for testing validation caching."""

    def __init__(self, head_sha: str = "abc1234567890"):
        self.head_sha = head_sha
        self.get_head_sha_calls: list[Path] = []

    def get_head_sha(self, worktree: Path) -> str | None:
        self.get_head_sha_calls.append(worktree)
        return self.head_sha

    def get_current_branch(self, worktree: Path) -> str | None:
        return "test-branch"


class TestSessionControllerValidationCaching:
    """Tests for validation caching via PublishGate."""

    def test_validation_runs_with_sha_logged(self, tmp_path):
        """Validation runs and logs SHA on cache miss."""
        processor = MockCompletionProcessor()
        processor.completion_record = make_record(
            CompletionOutcome.COMPLETED,
            summary="Done",
            requested_actions=[RequestedAction.CREATE_PR],
        )

        command_runner = MockCommandRunner(returncode=0)
        working_copy = MockWorkingCopy(head_sha="deadbeef1234567890")

        controller = SessionController(
            completion_processor=processor,
            events=NullEventSink(),
            session_output=FileSystemSessionOutput(),
            working_copy=working_copy,
            command_runner=command_runner,
            validation_cmd="make test",
            validation_timeout_seconds=60,
        )

        # Create worktree with git repo so PublishGate can read SHA
        worktree = tmp_path / "worktree"
        worktree.mkdir()

        observation = SessionObservationResult.terminated(runtime_minutes=10.0)

        decision = controller.decide_outcome(
            observation=observation,
            worktree_path=worktree,
            issue_number=123,
            issue_title="Test Issue",
            session_name="issue-123",
        )

        # Validation should pass
        assert decision.status == SessionStatus.COMPLETED
        assert decision.validation_passed is True

        # Command should have been run
        assert len(command_runner.run_calls) == 1
        assert command_runner.run_calls[0]["command"] == "make test"

        # SHA should have been fetched
        assert len(working_copy.get_head_sha_calls) >= 1

    def test_validation_failure_returns_error(self, tmp_path):
        """Validation failure returns correct status and error info."""
        processor = MockCompletionProcessor()
        processor.completion_record = make_record(
            CompletionOutcome.COMPLETED,
            summary="Done",
            requested_actions=[RequestedAction.CREATE_PR],
        )

        command_runner = MockCommandRunner(returncode=1, stderr="Tests failed!")
        working_copy = MockWorkingCopy(head_sha="deadbeef1234567890")

        controller = SessionController(
            completion_processor=processor,
            events=NullEventSink(),
            session_output=FileSystemSessionOutput(),
            working_copy=working_copy,
            command_runner=command_runner,
            validation_cmd="make test",
            validation_timeout_seconds=60,
        )

        worktree = tmp_path / "worktree"
        worktree.mkdir()

        observation = SessionObservationResult.terminated(runtime_minutes=10.0)

        decision = controller.decide_outcome(
            observation=observation,
            worktree_path=worktree,
            issue_number=123,
            issue_title="Test Issue",
            session_name="issue-123",
        )

        # Validation failed
        assert decision.status == SessionStatus.VALIDATION_FAILED
        assert decision.validation_passed is False
        assert decision.validation_error is not None

    def test_validation_retry_event_includes_validation_reason(self, tmp_path: Path):
        """Retry events should carry the concrete validation failure reason."""
        processor = MockCompletionProcessor()
        processor.completion_record = make_record(
            CompletionOutcome.COMPLETED,
            summary="Done",
            requested_actions=[RequestedAction.CREATE_PR],
        )

        events = RecordingEventSink()
        command_runner = MockCommandRunner(returncode=1, stderr="dashboard tests failed")
        working_copy = MockWorkingCopy(head_sha="deadbeef1234567890")

        controller = SessionController(
            completion_processor=processor,
            events=events,
            session_output=FileSystemSessionOutput(),
            working_copy=working_copy,
            command_runner=command_runner,
            validation_cmd="make test-web",
            validation_timeout_seconds=60,
            max_validation_retries=3,
        )

        worktree = tmp_path / "worktree"
        worktree.mkdir()

        decision = controller.decide_outcome(
            observation=SessionObservationResult.terminated(runtime_minutes=10.0),
            worktree_path=worktree,
            issue_number=123,
            issue_title="Test Issue",
            session_name="issue-123",
        )

        assert decision.status == SessionStatus.NEEDS_VALIDATION_RETRY
        retry_event = next(
            event for event in events.events if event.event_type == EventName.SESSION_VALIDATION_RETRY_NEEDED
        )
        assert retry_event.data["validation_reason"] == "Validation failed for deadbeef (exit_code=1)"
        assert retry_event.data["validation_error_summary"] == "dashboard tests failed"

    def test_no_validation_when_no_command_configured(self):
        """No validation runs when validation_cmd is not configured."""
        processor = MockCompletionProcessor()
        processor.completion_record = make_record(
            CompletionOutcome.COMPLETED,
            summary="Done",
            requested_actions=[RequestedAction.CREATE_PR],
        )

        command_runner = MockCommandRunner(returncode=0)
        working_copy = MockWorkingCopy()

        # No validation_cmd
        controller = SessionController(
            completion_processor=processor,
            events=NullEventSink(),
            session_output=FileSystemSessionOutput(),
            working_copy=working_copy,
            command_runner=command_runner,
            # validation_cmd not set
        )

        observation = SessionObservationResult.terminated(runtime_minutes=10.0)

        decision = controller.decide_outcome(
            observation=observation,
            worktree_path=Path("/tmp/test"),
            issue_number=123,
            issue_title="Test Issue",
            session_name="issue-123",
        )

        # Should complete without validation
        assert decision.status == SessionStatus.COMPLETED
        assert decision.validation_passed is None  # Not run

        # Command runner should NOT have been called
        assert len(command_runner.run_calls) == 0
