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
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, MagicMock, call, patch

from issue_orchestrator.domain.models import (
    CompletionRecord,
    CompletionOutcome,
    RequestedAction,
    COMPLETION_RECORD_PATH,
    AgentConfig,
)
from issue_orchestrator.control.review_exchange_loop import ReviewExchangeOutcome
from issue_orchestrator.control.completion_processor import (
    CompletionProcessor,
    ProcessingResult,
    LabelAdapter,
    PRAdapter,
    GitAdapter,
)
from issue_orchestrator.control.pre_publish_gate import PrePublishGateResult
from issue_orchestrator.infra.config import Config
from issue_orchestrator.execution.session_output_adapter import FileSystemSessionOutput
from issue_orchestrator.events import EventContext, EventName
from issue_orchestrator.ports.event_sink import InMemoryEventSink
from issue_orchestrator.ports.pull_request_tracker import PRInfo
from issue_orchestrator.ports.working_copy import PushResult
from issue_orchestrator.domain.events import EventBus, SessionEvent
from issue_orchestrator.infra.issue_diagnostics import DiagnosticReference


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
    adapter.get_prs_for_issue = Mock(return_value=[])
    adapter.get_prs_for_branch = Mock(return_value=[])
    adapter.create_pr = Mock(return_value=PRInfo(
        number=42,
        title="Test PR",
        url="https://github.com/owner/repo/pull/42",
        branch="issue-123",
        body="Test body",
        state="open",
        labels=[],
    ))
    adapter.add_comment = Mock(return_value="comment-id")
    return adapter


@pytest.fixture
def mock_git_adapter():
    """Mock adapter for git operations."""
    adapter = Mock(spec=GitAdapter)
    adapter.push = Mock(return_value=PushResult(
        success=True,
        branch="issue-123",
        remote="origin",
        message="Pushed",
    ))
    adapter.rebase_on_branch = Mock(return_value=MagicMock(success=True, message="Rebased"))
    adapter.create_branch_from_current = Mock()
    adapter.list_branch_names = Mock(return_value=["issue-123"])
    adapter.get_current_branch = Mock(return_value="issue-123")
    adapter.has_uncommitted_changes = Mock(return_value=False)
    adapter.has_tracked_changes = Mock(return_value=False)
    adapter.list_dirty_files = Mock(return_value=[])
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
        session_output=FileSystemSessionOutput(),
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
        # Create session output directory if session_id is present
        if record.session_id:
            session_dir = record_dir / "sessions" / record.session_id
            session_dir.mkdir(parents=True, exist_ok=True)
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


class TestReviewExchangeModeResolution:
    """Tests for review exchange mode selection and derivation."""

    def _make_config(self, tmp_path: Path) -> Config:
        coder_prompt = tmp_path / "coder.md"
        reviewer_prompt = tmp_path / "reviewer.md"
        coder_prompt.write_text("Coder prompt")
        reviewer_prompt.write_text("Reviewer prompt")
        config = Config()
        config.review_enabled = True
        config.review_exchange_mode = "auto"
        config.code_review_agent = "agent:reviewer"
        config.agents = {
            "agent:coder": AgentConfig(prompt_path=coder_prompt, ai_system="claude-code"),
            "agent:reviewer": AgentConfig(prompt_path=reviewer_prompt, ai_system="codex"),
        }
        return config

    def _make_processor(self, config: Config) -> CompletionProcessor:
        return CompletionProcessor(
            label_adapter=Mock(spec=LabelAdapter),
            pr_adapter=Mock(spec=PRAdapter),
            git_adapter=Mock(spec=GitAdapter),
            session_output=FileSystemSessionOutput(),
            event_bus=EventBus(),
            label_config={},
            config=config,
        )

    def test_auto_mode_uses_mcp_when_supported(self, tmp_path, monkeypatch):
        config = self._make_config(tmp_path)
        processor = self._make_processor(config)

        monkeypatch.setattr(
            "issue_orchestrator.infra.review_exchange_registry.supports_mcp_pair",
            lambda *_args, **_kwargs: True,
        )

        assert processor._resolve_review_exchange_mode("agent:coder") == "via-mcp"

    def test_explicit_local_loop_mode_does_not_depend_on_review_enabled(self, tmp_path):
        config = self._make_config(tmp_path)
        config.review_enabled = False
        config.review_exchange_mode = "via-local-loop"
        processor = self._make_processor(config)

        assert processor._resolve_review_exchange_mode("agent:coder") == "via-local-loop"


class TestReviewExchangeExecution:
    """Tests for review exchange execution paths."""

    def _make_config(self, tmp_path: Path) -> Config:
        coder_prompt = tmp_path / "coder.md"
        reviewer_prompt = tmp_path / "reviewer.md"
        coder_prompt.write_text("Coder prompt")
        reviewer_prompt.write_text("Reviewer prompt")
        config = Config()
        config.review_enabled = True
        config.review_exchange_mode = "via-mcp"
        config.code_review_agent = "agent:reviewer"
        config.agents = {
            "agent:coder": AgentConfig(prompt_path=coder_prompt, ai_system="claude-code"),
            "agent:reviewer": AgentConfig(prompt_path=reviewer_prompt, ai_system="codex"),
        }
        return config

    def _make_processor(self, config: Config) -> CompletionProcessor:
        return CompletionProcessor(
            label_adapter=Mock(spec=LabelAdapter),
            pr_adapter=Mock(spec=PRAdapter),
            git_adapter=Mock(spec=GitAdapter),
            session_output=FileSystemSessionOutput(),
            event_bus=EventBus(),
            label_config={},
            config=config,
        )

    def test_exchange_failure_halts_before_pr_creation(
        self,
        tmp_path,
        mock_label_adapter,
        mock_pr_adapter,
        mock_git_adapter,
        event_bus,
        worktree_with_completion,
        monkeypatch,
    ) -> None:
        config = self._make_config(tmp_path)
        processor = CompletionProcessor(
            label_adapter=mock_label_adapter,
            pr_adapter=mock_pr_adapter,
            git_adapter=mock_git_adapter,
            session_output=FileSystemSessionOutput(),
            event_bus=event_bus,
            label_config={
                "code_reviewed": "code-reviewed",
                "code_review": "needs-code-review",
            },
            config=config,
        )
        record = make_record(
            outcome=CompletionOutcome.COMPLETED,
            requested_actions=[
                RequestedAction.PUSH_BRANCH,
                RequestedAction.CREATE_PR,
            ],
        )
        worktree = worktree_with_completion(record)

        monkeypatch.setattr(
            "issue_orchestrator.infra.review_exchange_registry.supports_mcp_pair",
            lambda *_args, **_kwargs: True,
        )
        processor._run_review_exchange_loop = MagicMock(  # noqa: SLF001
            return_value=ReviewExchangeOutcome(status="error", rounds=1, reason="boom")
        )

        result = processor.process(
            worktree,
            issue_number=123,
            issue_title="Test Issue",
            agent_label="agent:coder",
        )

        assert result.success is False
        assert result.pr_url is None
        assert result.errors and "review_exchange" in result.errors[0]
        mock_pr_adapter.create_pr.assert_not_called()

    def test_exchange_success_marks_review_labels(
        self,
        tmp_path,
        mock_label_adapter,
        mock_pr_adapter,
        mock_git_adapter,
        event_bus,
        worktree_with_completion,
    ) -> None:
        config = self._make_config(tmp_path)
        config.review_exchange_mode = "via-local-loop"
        config.worktree_remediation_pr_collision = "reuse_open"
        processor = CompletionProcessor(
            label_adapter=mock_label_adapter,
            pr_adapter=mock_pr_adapter,
            git_adapter=mock_git_adapter,
            session_output=FileSystemSessionOutput(),
            event_bus=event_bus,
            label_config={
                "code_reviewed": "code-reviewed",
                "code_review": "needs-code-review",
            },
            config=config,
        )
        record = make_record(
            outcome=CompletionOutcome.COMPLETED,
            requested_actions=[
                RequestedAction.PUSH_BRANCH,
                RequestedAction.CREATE_PR,
            ],
        )
        worktree = worktree_with_completion(record)

        processor._run_review_exchange_loop = MagicMock(  # noqa: SLF001
            return_value=ReviewExchangeOutcome(status="ok", rounds=2, reason="reviewer_ok")
        )

        result = processor.process(
            worktree,
            issue_number=123,
            issue_title="Test Issue",
            agent_label="agent:coder",
        )

        assert result.success is True
        assert result.actions_taken is not None
        assert result.actions_taken[0] == "Review exchange passed"
        mock_label_adapter.add_label.assert_any_call(42, "code-reviewed")
        mock_label_adapter.remove_label.assert_any_call(42, "needs-code-review")

    def test_existing_pr_reuse_does_not_bypass_local_loop_review(
        self,
        tmp_path,
        mock_label_adapter,
        mock_pr_adapter,
        mock_git_adapter,
        event_bus,
        worktree_with_completion,
    ) -> None:
        """Existing PR reuse must run local-loop review before returning success."""
        config = self._make_config(tmp_path)
        config.review_exchange_mode = "via-local-loop"
        processor = CompletionProcessor(
            label_adapter=mock_label_adapter,
            pr_adapter=mock_pr_adapter,
            git_adapter=mock_git_adapter,
            session_output=FileSystemSessionOutput(),
            event_bus=event_bus,
            label_config={
                "code_reviewed": "code-reviewed",
                "code_review": "needs-code-review",
            },
            config=config,
        )
        mock_pr_adapter.get_prs_for_issue.return_value = [
            PRInfo(
                number=99,
                title="#123 Existing PR",
                url="https://github.com/owner/repo/pull/99",
                branch="issue-123",
                body="Body",
                state="open",
                labels=[],
            )
        ]
        record = make_record(
            outcome=CompletionOutcome.COMPLETED,
            requested_actions=[
                RequestedAction.PUSH_BRANCH,
                RequestedAction.CREATE_PR,
            ],
        )
        worktree = worktree_with_completion(record)

        processor._run_review_exchange_loop = MagicMock(  # noqa: SLF001
            return_value=ReviewExchangeOutcome(status="error", rounds=1, reason="boom")
        )

        result = processor.process(
            worktree,
            issue_number=123,
            issue_title="Test Issue",
            agent_label="agent:coder",
        )

        assert result.success is False
        assert result.pr_url is None
        processor._run_review_exchange_loop.assert_called_once()  # noqa: SLF001
        mock_pr_adapter.create_pr.assert_not_called()

    def test_existing_pr_reuse_after_local_loop_success_marks_review_complete(
        self,
        tmp_path,
        mock_label_adapter,
        mock_pr_adapter,
        mock_git_adapter,
        event_bus,
        worktree_with_completion,
    ) -> None:
        """Reused PR should still get local-loop completion labels/comment."""
        config = self._make_config(tmp_path)
        config.review_exchange_mode = "via-local-loop"
        config.worktree_remediation_pr_collision = "reuse_open"
        processor = CompletionProcessor(
            label_adapter=mock_label_adapter,
            pr_adapter=mock_pr_adapter,
            git_adapter=mock_git_adapter,
            session_output=FileSystemSessionOutput(),
            event_bus=event_bus,
            label_config={
                "code_reviewed": "code-reviewed",
                "code_review": "needs-code-review",
            },
            config=config,
        )
        mock_pr_adapter.get_prs_for_issue.return_value = [
            PRInfo(
                number=99,
                title="#123 Existing PR",
                url="https://github.com/owner/repo/pull/99",
                branch="issue-123",
                body="Body",
                state="open",
                labels=[],
            )
        ]
        record = make_record(
            outcome=CompletionOutcome.COMPLETED,
            requested_actions=[
                RequestedAction.PUSH_BRANCH,
                RequestedAction.CREATE_PR,
            ],
        )
        worktree = worktree_with_completion(record)

        processor._run_review_exchange_loop = MagicMock(  # noqa: SLF001
            return_value=ReviewExchangeOutcome(status="ok", rounds=2, reason="reviewer_ok")
        )

        result = processor.process(
            worktree,
            issue_number=123,
            issue_title="Test Issue",
            agent_label="agent:coder",
        )

        assert result.success is True
        assert result.pr_url == "https://github.com/owner/repo/pull/99"
        assert result.review_exchange_completed is True
        processor._run_review_exchange_loop.assert_called_once()  # noqa: SLF001
        mock_pr_adapter.create_pr.assert_not_called()
        mock_label_adapter.add_label.assert_any_call(99, "code-reviewed")
        mock_label_adapter.remove_label.assert_any_call(99, "needs-code-review")

    def test_existing_pr_reuse_ignores_prior_attempt_branch_and_creates_new_pr(
        self,
        tmp_path,
        mock_label_adapter,
        mock_pr_adapter,
        mock_git_adapter,
        event_bus,
        worktree_with_completion,
        caplog,
    ) -> None:
        """Scratch retries must not reuse an open PR from an older branch."""
        config = self._make_config(tmp_path)
        config.review_exchange_mode = "via-local-loop"
        config.worktree_remediation_pr_collision = "reuse_open"
        processor = CompletionProcessor(
            label_adapter=mock_label_adapter,
            pr_adapter=mock_pr_adapter,
            git_adapter=mock_git_adapter,
            session_output=FileSystemSessionOutput(),
            event_bus=event_bus,
            label_config={
                "code_reviewed": "code-reviewed",
                "code_review": "needs-code-review",
            },
            config=config,
        )
        mock_git_adapter.get_current_branch.return_value = "123-fresh-branch"
        mock_pr_adapter.get_prs_for_issue.return_value = [
            PRInfo(
                number=99,
                title="#123 Existing PR",
                url="https://github.com/owner/repo/pull/99",
                branch="123-old-branch",
                body="Body",
                state="open",
                labels=[],
            )
        ]
        mock_pr_adapter.create_pr.return_value = PRInfo(
            number=100,
            title="#123 Fresh PR",
            url="https://github.com/owner/repo/pull/100",
            branch="123-fresh-branch",
            body="Body",
            state="open",
            labels=[],
        )
        record = make_record(
            outcome=CompletionOutcome.COMPLETED,
            requested_actions=[
                RequestedAction.PUSH_BRANCH,
                RequestedAction.CREATE_PR,
            ],
        )
        worktree = worktree_with_completion(record)

        processor._run_review_exchange_loop = MagicMock(  # noqa: SLF001
            return_value=ReviewExchangeOutcome(status="ok", rounds=1, reason="reviewer_ok")
        )

        with caplog.at_level("INFO"):
            result = processor.process(
                worktree,
                issue_number=123,
                issue_title="Test Issue",
                agent_label="agent:coder",
            )

        assert result.success is True
        assert result.pr_url == "https://github.com/owner/repo/pull/100"
        mock_pr_adapter.create_pr.assert_called_once()
        assert (
            "Ignoring open PR from prior attempt for issue #123: pr=99 branch=123-old-branch expected_branch=123-fresh-branch"
            in caplog.text
        )

    def test_local_loop_emits_review_started_and_approved_trace_events(
        self,
        tmp_path,
        mock_label_adapter,
        mock_pr_adapter,
        mock_git_adapter,
        event_bus,
        worktree_with_completion,
    ) -> None:
        """Local-loop success should publish explicit review lifecycle trace events."""
        config = self._make_config(tmp_path)
        config.review_exchange_mode = "via-local-loop"
        processor = CompletionProcessor(
            label_adapter=mock_label_adapter,
            pr_adapter=mock_pr_adapter,
            git_adapter=mock_git_adapter,
            session_output=FileSystemSessionOutput(),
            event_bus=event_bus,
            label_config={
                "code_reviewed": "code-reviewed",
                "code_review": "needs-code-review",
            },
            config=config,
        )
        sink = InMemoryEventSink()
        processor.set_event_emitter(sink, EventContext())
        record = make_record(
            outcome=CompletionOutcome.COMPLETED,
            requested_actions=[RequestedAction.PUSH_BRANCH, RequestedAction.CREATE_PR],
        )
        worktree = worktree_with_completion(record)
        review_exchange_run = (
            worktree
            / ".issue-orchestrator"
            / "sessions"
            / "20260218-030043Z__review-exchange-123"
        )
        processor._run_review_exchange_loop = MagicMock(  # noqa: SLF001
            return_value=ReviewExchangeOutcome(
                status="ok",
                rounds=1,
                reason="reviewer_ok",
                exchange_dir=review_exchange_run / "review-exchange",
            )
        )

        result = processor.process(
            worktree,
            issue_number=123,
            issue_title="Test Issue",
            agent_label="agent:coder",
        )

        assert result.success is True
        event_names = sink.event_names()
        assert str(EventName.REVIEW_STARTED) in event_names
        assert str(EventName.REVIEW_APPROVED) in event_names
        assert event_names.index(str(EventName.REVIEW_STARTED)) < event_names.index(str(EventName.REVIEW_APPROVED))
        review_started = sink.last_event(str(EventName.REVIEW_STARTED))
        review_approved = sink.last_event(str(EventName.REVIEW_APPROVED))
        assert review_started is not None
        assert review_approved is not None
        assert str(review_started.data.get("run_dir", "")).endswith(
            "/.issue-orchestrator/sessions/20260218-030043Z__review-exchange-123"
        )
        assert str(review_approved.data.get("run_dir", "")).endswith(
            "/.issue-orchestrator/sessions/20260218-030043Z__review-exchange-123"
        )
        review_events = [
            event
            for event in sink.events
            if str(event.name).startswith("review.")
        ]
        assert review_events, "Expected review lifecycle events to be emitted"
        for event in review_events:
            assert str(event.data.get("run_dir", "")).endswith(
                "/.issue-orchestrator/sessions/20260218-030043Z__review-exchange-123"
            ), f"missing run_dir on {event.name}"

    def test_local_loop_failure_emits_review_changes_requested_trace_event(
        self,
        tmp_path,
        mock_label_adapter,
        mock_pr_adapter,
        mock_git_adapter,
        event_bus,
        worktree_with_completion,
    ) -> None:
        """Local-loop halt should publish review.started then review.changes_requested."""
        config = self._make_config(tmp_path)
        config.review_exchange_mode = "via-local-loop"
        processor = CompletionProcessor(
            label_adapter=mock_label_adapter,
            pr_adapter=mock_pr_adapter,
            git_adapter=mock_git_adapter,
            session_output=FileSystemSessionOutput(),
            event_bus=event_bus,
            label_config={
                "code_reviewed": "code-reviewed",
                "code_review": "needs-code-review",
            },
            config=config,
        )
        sink = InMemoryEventSink()
        processor.set_event_emitter(sink, EventContext())
        record = make_record(
            outcome=CompletionOutcome.COMPLETED,
            requested_actions=[RequestedAction.PUSH_BRANCH, RequestedAction.CREATE_PR],
        )
        worktree = worktree_with_completion(record)
        review_exchange_run = (
            worktree
            / ".issue-orchestrator"
            / "sessions"
            / "20260218-030044Z__review-exchange-123"
        )
        processor._run_review_exchange_loop = MagicMock(  # noqa: SLF001
            return_value=ReviewExchangeOutcome(
                status="error",
                rounds=1,
                reason="boom",
                exchange_dir=review_exchange_run / "review-exchange",
            )
        )

        result = processor.process(
            worktree,
            issue_number=123,
            issue_title="Test Issue",
            agent_label="agent:coder",
        )

        assert result.success is False
        assert result.review_exchange_halted is True
        assert result.pr_url is None
        mock_git_adapter.push.assert_not_called()
        mock_pr_adapter.create_pr.assert_not_called()
        event_names = sink.event_names()
        assert str(EventName.REVIEW_STARTED) in event_names
        assert str(EventName.REVIEW_CHANGES_REQUESTED) in event_names
        assert event_names.index(str(EventName.REVIEW_STARTED)) < event_names.index(
            str(EventName.REVIEW_CHANGES_REQUESTED)
        )
        review_started = sink.last_event(str(EventName.REVIEW_STARTED))
        review_changes = sink.last_event(str(EventName.REVIEW_CHANGES_REQUESTED))
        assert review_started is not None
        assert review_changes is not None
        assert str(review_started.data.get("run_dir", "")).endswith(
            "/.issue-orchestrator/sessions/20260218-030044Z__review-exchange-123"
        )
        assert str(review_changes.data.get("run_dir", "")).endswith(
            "/.issue-orchestrator/sessions/20260218-030044Z__review-exchange-123"
        )

    def test_exchange_uses_cached_summary_after_restart(
        self,
        tmp_path,
        mock_label_adapter,
        mock_pr_adapter,
        mock_git_adapter,
        event_bus,
        monkeypatch,
    ) -> None:
        config = self._make_config(tmp_path)
        processor = CompletionProcessor(
            label_adapter=mock_label_adapter,
            pr_adapter=mock_pr_adapter,
            git_adapter=mock_git_adapter,
            session_output=FileSystemSessionOutput(),
            event_bus=event_bus,
            label_config={
                "code_reviewed": "code-reviewed",
                "code_review": "needs-code-review",
            },
            config=config,
        )
        sink = InMemoryEventSink()
        processor.set_event_emitter(sink, EventContext())
        record = make_record(
            outcome=CompletionOutcome.COMPLETED,
            requested_actions=[
                RequestedAction.PUSH_BRANCH,
                RequestedAction.CREATE_PR,
            ],
        )

        worktree = tmp_path / "worktree"
        worktree.mkdir()
        run_dir = worktree / ".issue-orchestrator" / "sessions" / "20260201-000000Z__review-exchange-123"
        exchange_dir = run_dir / "review-exchange"
        exchange_dir.mkdir(parents=True, exist_ok=True)
        (exchange_dir / "summary.json").write_text(
            json.dumps({
                "completed_rounds": 2,
                "status": "ok",
                "response_text": "Looks good",
                "timestamp": "2026-02-01T00:00:00Z",
            })
        )
        validation_record = run_dir / "validation-record.json"
        validation_record.write_text(json.dumps({"passed": True, "head_sha": "same-sha"}))
        record = make_record(
            outcome=CompletionOutcome.COMPLETED,
            requested_actions=[
                RequestedAction.PUSH_BRANCH,
                RequestedAction.CREATE_PR,
            ],
            validation_record_path=str(validation_record),
        )
        completion_path = (
            ".issue-orchestrator/sessions/20260201-000000Z__review-exchange-123/"
            "completion-coder.json"
        )
        completion_file = worktree / completion_path
        completion_file.parent.mkdir(parents=True, exist_ok=True)
        completion_file.write_text(json.dumps(record.to_dict()))

        monkeypatch.setattr(
            "issue_orchestrator.infra.review_exchange_registry.supports_mcp_pair",
            lambda *_args, **_kwargs: True,
        )
        processor._run_review_exchange_loop = MagicMock(  # noqa: SLF001
            side_effect=AssertionError("exchange should not re-run")
        )

        result = processor.process(
            worktree,
            issue_number=123,
            issue_title="Test Issue",
            agent_label="agent:coder",
            completion_path=completion_path,
        )

        assert result.success is True
        assert result.review_exchange_completed is True
        # Cache-replay must be tagged so the timeline narrates it as a
        # replay rather than claiming a fresh 2-round review happened
        # in this run (issue #228 regression).
        review_started = sink.last_event(str(EventName.REVIEW_STARTED))
        review_approved = sink.last_event(str(EventName.REVIEW_APPROVED))
        assert review_started is not None
        assert review_approved is not None
        assert review_started.data.get("cached") is True
        assert review_approved.data.get("cached") is True

    def test_cached_exchange_non_ok_status_emits_cached_changes_requested(
        self,
        tmp_path,
        mock_label_adapter,
        mock_pr_adapter,
        mock_git_adapter,
        event_bus,
        monkeypatch,
    ) -> None:
        # Symmetric to test_exchange_uses_cached_summary_after_restart but for
        # the non-ok branch: if a prior run persisted a changes_requested
        # outcome, the replay must also be tagged cached=True so the timeline
        # narrates it as a replay rather than a fresh reviewer verdict.
        config = self._make_config(tmp_path)
        processor = CompletionProcessor(
            label_adapter=mock_label_adapter,
            pr_adapter=mock_pr_adapter,
            git_adapter=mock_git_adapter,
            session_output=FileSystemSessionOutput(),
            event_bus=event_bus,
            label_config={
                "code_reviewed": "code-reviewed",
                "code_review": "needs-code-review",
            },
            config=config,
        )
        sink = InMemoryEventSink()
        processor.set_event_emitter(sink, EventContext())
        record = make_record(
            outcome=CompletionOutcome.COMPLETED,
            requested_actions=[
                RequestedAction.PUSH_BRANCH,
                RequestedAction.CREATE_PR,
            ],
        )

        worktree = tmp_path / "worktree"
        worktree.mkdir()
        run_dir = worktree / ".issue-orchestrator" / "sessions" / "20260201-000000Z__review-exchange-123"
        exchange_dir = run_dir / "review-exchange"
        exchange_dir.mkdir(parents=True, exist_ok=True)
        (exchange_dir / "summary.json").write_text(
            json.dumps({
                "completed_rounds": 3,
                "status": "changes_requested",
                "response_text": "Still three open comments.",
                "timestamp": "2026-02-01T00:00:00Z",
            })
        )
        validation_record = run_dir / "validation-record.json"
        validation_record.write_text(json.dumps({"passed": True, "head_sha": "same-sha"}))
        record = make_record(
            outcome=CompletionOutcome.COMPLETED,
            requested_actions=[
                RequestedAction.PUSH_BRANCH,
                RequestedAction.CREATE_PR,
            ],
            validation_record_path=str(validation_record),
        )
        completion_path = (
            ".issue-orchestrator/sessions/20260201-000000Z__review-exchange-123/"
            "completion-coder.json"
        )
        completion_file = worktree / completion_path
        completion_file.parent.mkdir(parents=True, exist_ok=True)
        completion_file.write_text(json.dumps(record.to_dict()))

        monkeypatch.setattr(
            "issue_orchestrator.infra.review_exchange_registry.supports_mcp_pair",
            lambda *_args, **_kwargs: True,
        )
        processor._run_review_exchange_loop = MagicMock(  # noqa: SLF001
            side_effect=AssertionError("exchange should not re-run on cache hit")
        )

        result = processor.process(
            worktree,
            issue_number=123,
            issue_title="Test Issue",
            agent_label="agent:coder",
            completion_path=completion_path,
        )

        assert result.success is False
        review_started = sink.last_event(str(EventName.REVIEW_STARTED))
        review_changes = sink.last_event(str(EventName.REVIEW_CHANGES_REQUESTED))
        assert review_started is not None
        assert review_changes is not None
        assert review_started.data.get("cached") is True
        assert review_changes.data.get("cached") is True
        # Fresh review.approved must not be emitted on the non-ok cache path.
        assert sink.last_event(str(EventName.REVIEW_APPROVED)) is None

    def test_cached_exchange_requires_validation_record(
        self,
        tmp_path,
        mock_label_adapter,
        mock_pr_adapter,
        mock_git_adapter,
        event_bus,
        monkeypatch,
    ) -> None:
        config = self._make_config(tmp_path)
        processor = CompletionProcessor(
            label_adapter=mock_label_adapter,
            pr_adapter=mock_pr_adapter,
            git_adapter=mock_git_adapter,
            session_output=FileSystemSessionOutput(),
            event_bus=event_bus,
            label_config={
                "code_reviewed": "code-reviewed",
                "code_review": "needs-code-review",
            },
            config=config,
        )
        record = make_record(
            outcome=CompletionOutcome.COMPLETED,
            requested_actions=[
                RequestedAction.PUSH_BRANCH,
                RequestedAction.CREATE_PR,
            ],
        )

        worktree = tmp_path / "worktree"
        worktree.mkdir()
        run_dir = worktree / ".issue-orchestrator" / "sessions" / "20260201-000000Z__review-exchange-123"
        exchange_dir = run_dir / "review-exchange"
        exchange_dir.mkdir(parents=True, exist_ok=True)
        (exchange_dir / "summary.json").write_text(
            json.dumps({
                "completed_rounds": 2,
                "status": "ok",
                "response_text": "Looks good",
                "timestamp": "2026-02-01T00:00:00Z",
            })
        )
        completion_path = (
            ".issue-orchestrator/sessions/20260201-000000Z__review-exchange-123/"
            "completion-coder.json"
        )
        completion_file = worktree / completion_path
        completion_file.parent.mkdir(parents=True, exist_ok=True)
        completion_file.write_text(json.dumps(record.to_dict()))

        monkeypatch.setattr(
            "issue_orchestrator.infra.review_exchange_registry.supports_mcp_pair",
            lambda *_args, **_kwargs: True,
        )
        processor._run_review_exchange_loop = MagicMock(  # noqa: SLF001
            return_value=ReviewExchangeOutcome(status="error", rounds=1, reason="no-validation")
        )

        result = processor.process(
            worktree,
            issue_number=123,
            issue_title="Test Issue",
            agent_label="agent:coder",
            completion_path=completion_path,
        )

        assert result.success is False
        assert result.errors

    def test_cached_exchange_uses_manifest_pointer(
        self,
        tmp_path,
        mock_label_adapter,
        mock_pr_adapter,
        mock_git_adapter,
        event_bus,
        monkeypatch,
    ) -> None:
        config = self._make_config(tmp_path)
        session_output = FileSystemSessionOutput()
        processor = CompletionProcessor(
            label_adapter=mock_label_adapter,
            pr_adapter=mock_pr_adapter,
            git_adapter=mock_git_adapter,
            session_output=session_output,
            event_bus=event_bus,
            label_config={
                "code_reviewed": "code-reviewed",
                "code_review": "needs-code-review",
            },
            config=config,
        )
        record = make_record(
            outcome=CompletionOutcome.COMPLETED,
            requested_actions=[
                RequestedAction.PUSH_BRANCH,
                RequestedAction.CREATE_PR,
            ],
        )

        worktree = tmp_path / "worktree"
        worktree.mkdir()
        session_name = "20260201-000000Z__review-exchange-123"
        issue_run_dir = session_output.ensure_run_dir(worktree, session_name)
        exchange_run_dir = tmp_path / "exchange-run"
        exchange_dir = exchange_run_dir / "review-exchange"
        exchange_dir.mkdir(parents=True, exist_ok=True)
        (exchange_dir / "summary.json").write_text(
            json.dumps({
                "completed_rounds": 2,
                "status": "ok",
                "response_text": "Looks good",
                "timestamp": "2026-02-01T00:00:00Z",
            })
        )
        validation_record = issue_run_dir / "validation-record.json"
        validation_record.write_text(json.dumps({"passed": True, "head_sha": "same-sha"}))
        record = make_record(
            outcome=CompletionOutcome.COMPLETED,
            requested_actions=[
                RequestedAction.PUSH_BRANCH,
                RequestedAction.CREATE_PR,
            ],
            validation_record_path=str(validation_record),
        )
        session_output.update_manifest(
            issue_run_dir,
            {"review_exchange_dir": str(exchange_dir)},
        )
        completion_path = (
            ".issue-orchestrator/sessions/20260201-000000Z__review-exchange-123/"
            "completion-coder.json"
        )
        completion_file = worktree / completion_path
        completion_file.parent.mkdir(parents=True, exist_ok=True)
        completion_file.write_text(json.dumps(record.to_dict()))

        monkeypatch.setattr(
            "issue_orchestrator.infra.review_exchange_registry.supports_mcp_pair",
            lambda *_args, **_kwargs: True,
        )
        processor._run_review_exchange_loop = MagicMock(  # noqa: SLF001
            side_effect=AssertionError("exchange should not re-run")
        )

        result = processor.process(
            worktree,
            issue_number=123,
            issue_title="Test Issue",
            agent_label="agent:coder",
            completion_path=completion_path,
        )

        assert result.success is True
        assert result.review_exchange_completed is True


    def test_auto_mode_falls_back_to_local_loop(self, tmp_path, monkeypatch):
        config = self._make_config(tmp_path)
        config.review_exchange_mode = "auto"
        processor = self._make_processor(config)

        monkeypatch.setattr(
            "issue_orchestrator.infra.review_exchange_registry.supports_mcp_pair",
            lambda *_args, **_kwargs: False,
        )

        assert processor._resolve_review_exchange_mode("agent:coder") == "via-local-loop"

    def test_auto_mode_without_agent_label_returns_none(self, tmp_path):
        config = self._make_config(tmp_path)
        config.review_exchange_mode = "auto"
        processor = self._make_processor(config)

        assert processor._resolve_review_exchange_mode(None) is None

    def test_via_mcp_requires_supported_pair(self, tmp_path, monkeypatch):
        config = self._make_config(tmp_path)
        config.review_exchange_mode = "via-mcp"
        processor = self._make_processor(config)

        monkeypatch.setattr(
            "issue_orchestrator.infra.review_exchange_registry.supports_mcp_pair",
            lambda *_args, **_kwargs: False,
        )

        with pytest.raises(ValueError, match="supported ai_system pair"):
            processor._resolve_review_exchange_mode("agent:coder")

    def test_run_review_exchange_uses_default_reviewer(self, tmp_path, monkeypatch):
        config = self._make_config(tmp_path)
        processor = self._make_processor(config)
        captured: dict[str, str] = {}

        def _fake_run(**kwargs):
            captured["coder_label"] = kwargs["coder_label"]
            captured["reviewer_label"] = kwargs["reviewer_label"]
            return MagicMock(status="ok", rounds=1, reason="reviewer_ok")

        monkeypatch.setattr(
            "issue_orchestrator.control.review_exchange_loop.run_review_exchange_loop",
            _fake_run,
        )

        processor._run_review_exchange_loop(
            worktree=tmp_path,
            issue_number=1,
            issue_title="Test",
            session_name="session-1",
            agent_label="agent:coder",
        )

        assert captured["coder_label"] == "agent:coder"
        assert captured["reviewer_label"] == "agent:reviewer"

    def test_resolve_agent_label_from_completion_path(self, tmp_path):
        coder_prompt = tmp_path / "coder.md"
        coder_prompt.write_text("Coder prompt")
        config = Config()
        config.agents = {
            "agent:backend": AgentConfig(prompt_path=coder_prompt, ai_system="claude-code")
        }
        processor = self._make_processor(config)

        label, error = processor._resolve_agent_label_from_completion_path(
            ".issue-orchestrator/sessions/issue-123/completion-agent_backend.json"
        )

        assert error is None
        assert label == "agent:backend"

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
                RequestedAction.REMOVE_NEEDS_REWORK_LABEL,
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
        mock_label_adapter.remove_label.assert_has_calls(
            [call(42, "needs-rework"), call(42, "needs-code-review")]
        )

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

    def test_review_changes_requested_writes_feedback_file(
        self, processor, worktree_with_completion
    ):
        """Changes requested with review_issues should write feedback file to run dir."""
        record = make_record(
            outcome=CompletionOutcome.REVIEW_CHANGES_REQUESTED,
            requested_actions=[
                RequestedAction.ADD_NEEDS_REWORK_LABEL,
                RequestedAction.REMOVE_CODE_REVIEW_LABEL,
            ],
            summary="Need fixes",
            review_issues="Missing error handling and unit tests",
        )
        worktree = worktree_with_completion(record)

        # Process with pr_number to indicate review session
        result = processor.process(
            worktree, issue_number=42, issue_title="Fix bug", pr_number=456
        )

        assert result.success
        # Verify feedback file was written to session run directory
        sessions_dir = worktree / ".issue-orchestrator" / "sessions"
        # Find the session directory (may have timestamp suffix)
        session_dirs = list(sessions_dir.iterdir()) if sessions_dir.exists() else []
        assert len(session_dirs) > 0, "Session directory should exist"
        feedback_file = session_dirs[0] / "reviewer-feedback.json"
        assert feedback_file.exists(), "Feedback file should be written"
        # Verify content
        feedback_data = json.loads(feedback_file.read_text())
        assert feedback_data["pr_number"] == 456
        assert feedback_data["review_issues"] == "Missing error handling and unit tests"
        assert "timestamp" in feedback_data

    def test_review_without_issues_does_not_write_feedback_file(
        self, processor, worktree_with_completion
    ):
        """Review without review_issues should not write feedback file."""
        record = make_record(
            outcome=CompletionOutcome.REVIEW_APPROVED,
            requested_actions=[
                RequestedAction.ADD_CODE_REVIEWED_LABEL,
                RequestedAction.REMOVE_CODE_REVIEW_LABEL,
            ],
            summary="Looks good",
            review_issues=None,  # No issues
        )
        worktree = worktree_with_completion(record)

        result = processor.process(
            worktree, issue_number=42, issue_title="Fix bug", pr_number=456
        )

        assert result.success
        # Feedback file should NOT exist
        sessions_dir = worktree / ".issue-orchestrator" / "sessions"
        if sessions_dir.exists():
            for session_dir in sessions_dir.iterdir():
                feedback_file = session_dir / "reviewer-feedback.json"
                assert not feedback_file.exists(), "Feedback file should not be written for approved reviews"


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
        assert call_args.kwargs["draft"] is True

    def test_push_failure_halts_pr_creation(
        self, processor, mock_git_adapter, mock_pr_adapter, worktree_with_completion
    ):
        """Push failure should stop later CREATE_PR actions."""
        mock_git_adapter.push.return_value = PushResult(
            success=False,
            branch="issue-123",
            remote="origin",
            message="pre-push hook failed",
        )
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

        assert not result.success
        assert any("Push failed" in err for err in result.errors)
        mock_pr_adapter.create_pr.assert_not_called()

    def test_create_pr_with_labels_applies_labels_to_pr(
        self, processor, mock_pr_adapter, mock_label_adapter, worktree_with_completion
    ):
        """PR labels from completion record should be applied to the created PR.

        This is critical for e2e test cleanup - PRs must be labeled so they
        can be identified and cleaned up by the test fixture.
        """
        record = make_record(
            outcome=CompletionOutcome.COMPLETED,
            requested_actions=[
                RequestedAction.PUSH_BRANCH,
                RequestedAction.CREATE_PR,
            ],
            summary="Implemented feature",
            implementation="Added the feature",
            pr_labels=["test-data", "e2e-test"],  # Labels to apply to PR
        )
        worktree = worktree_with_completion(record)

        result = processor.process(worktree, issue_number=123, issue_title="Add feature")

        assert result.success
        # PR was created with number 42 (from mock)
        assert result.pr_url == "https://github.com/owner/repo/pull/42"
        # Labels should be applied to the PR (number 42), not the issue (123)
        label_calls = mock_label_adapter.add_label.call_args_list
        assert len(label_calls) == 2
        assert label_calls[0] == ((42, "test-data"),)
        assert label_calls[1] == ((42, "e2e-test"),)

    def test_create_pr_without_labels_does_not_add_labels(
        self, processor, mock_pr_adapter, mock_label_adapter, worktree_with_completion
    ):
        """PR creation without pr_labels should not call add_label."""
        record = make_record(
            outcome=CompletionOutcome.COMPLETED,
            requested_actions=[
                RequestedAction.PUSH_BRANCH,
                RequestedAction.CREATE_PR,
            ],
            summary="Implemented feature",
            implementation="Added the feature",
            # No pr_labels field
        )
        worktree = worktree_with_completion(record)

        result = processor.process(worktree, issue_number=123, issue_title="Add feature")

        assert result.success
        # No labels should be added (no add_label calls)
        mock_label_adapter.add_label.assert_not_called()

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
        self, processor, mock_git_adapter, worktree_with_completion, monkeypatch
    ):
        """PUSH_BRANCH action should push via adapter."""
        monkeypatch.delenv("E2E_SKIP_PUSH_HOOKS", raising=False)
        record = make_record(
            outcome=CompletionOutcome.COMPLETED,
            requested_actions=[RequestedAction.PUSH_BRANCH],
            summary="Done",
        )
        worktree = worktree_with_completion(record)

        result = processor.process(worktree, issue_number=123, issue_title="Test")

        assert result.success
        mock_git_adapter.push.assert_called_once_with(worktree, skip_hooks=False)

    def test_push_failure_is_recorded(
        self, processor, mock_git_adapter, mock_pr_adapter, worktree_with_completion
    ):
        """Failed push should be recorded in result."""
        mock_git_adapter.push.return_value = PushResult(
            success=False,
            branch="issue-123",
            remote="origin",
            message="Remote rejected",
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
        mock_pr_adapter.add_comment.assert_called_once()
        comment = mock_pr_adapter.add_comment.call_args[0][1]
        assert "Orchestrator Processing Failed" in comment
        assert "Push failed" in comment

    def test_push_failure_emits_publish_failed_event(
        self, processor, mock_git_adapter, worktree_with_completion
    ):
        """On push failure a publish.failed trace event carries the real error."""
        mock_git_adapter.push.return_value = PushResult(
            success=False,
            branch="issue-123",
            remote="origin",
            message="git command timed out: pre-push hook stuck",
            retryable=False,
        )
        sink = InMemoryEventSink()
        processor.set_event_emitter(sink, EventContext())
        record = make_record(
            outcome=CompletionOutcome.COMPLETED,
            requested_actions=[RequestedAction.PUSH_BRANCH],
            summary="Done",
        )
        worktree = worktree_with_completion(record)

        processor.process(worktree, issue_number=123, issue_title="Test")

        publish_failed = [
            event
            for event in sink.events
            if str(event.name) == str(EventName.PUBLISH_FAILED)
        ]
        assert len(publish_failed) == 1
        payload = publish_failed[0].data
        assert payload["stage"] == "push_branch"
        assert "pre-push hook stuck" in payload["error"]
        assert payload["branch"] == "issue-123"
        assert payload["issue_number"] == 123

    def test_push_non_fast_forward_retries_after_rebase(
        self, processor, mock_git_adapter, worktree_with_completion
    ):
        """Non-fast-forward push should retry after rebase."""
        mock_git_adapter.push.side_effect = [
            PushResult(
                success=False,
                branch="issue-123",
                remote="origin",
                message="non-fast-forward",
            ),
            PushResult(
                success=True,
                branch="issue-123",
                remote="origin",
                message="Pushed",
            ),
        ]
        record = make_record(
            outcome=CompletionOutcome.COMPLETED,
            requested_actions=[RequestedAction.PUSH_BRANCH],
            summary="Done",
        )
        worktree = worktree_with_completion(record)

        result = processor.process(worktree, issue_number=123, issue_title="Test")

        assert result.success
        mock_git_adapter.rebase_on_branch.assert_called_once_with(worktree, "origin/main")
        assert mock_git_adapter.push.call_count == 2

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
        self, processor, mock_git_adapter, mock_pr_adapter, worktree_with_completion
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
        mock_pr_adapter.add_comment.assert_called_once()
        comment = mock_pr_adapter.add_comment.call_args[0][1]
        assert "Orchestrator Processing Failed" in comment
        assert "protected branch" in comment.lower()


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


class TestCompletionProcessorDirtyPolicy:
    """Tests for dirty-tree policy enforcement before push."""

    def test_push_rejected_when_tracked_dirty(
        self, mock_label_adapter, mock_pr_adapter, mock_git_adapter, event_bus, worktree_with_completion
    ):
        config = Config()
        config.validation.pre_push_dirty_check = "tracked"
        processor = CompletionProcessor(
            label_adapter=mock_label_adapter,
            pr_adapter=mock_pr_adapter,
            git_adapter=mock_git_adapter,
            event_bus=event_bus,
            session_output=FileSystemSessionOutput(),
            label_config={},
            config=config,
        )
        mock_git_adapter.has_tracked_changes.return_value = True
        mock_git_adapter.list_dirty_files.return_value = ["src/feature.py", "README.md"]
        record = make_record(
            outcome=CompletionOutcome.COMPLETED,
            requested_actions=[RequestedAction.PUSH_BRANCH],
            summary="Done",
        )
        worktree = worktree_with_completion(record)

        result = processor.process(worktree, issue_number=123, issue_title="Test")

        assert not result.success
        assert result.failure_kind == "validation_failed"
        assert "working tree is dirty" in result.message.lower()
        assert "dirty files: src/feature.py, readme.md." in result.message.lower()
        assert result.errors == [
            "Validation: Working tree is dirty; commit/add/stash before pushing. "
            "Override with validation.pre_push_dirty_check. "
            "Dirty files: src/feature.py, README.md."
        ]
        mock_git_adapter.push.assert_not_called()
        mock_label_adapter.add_label.assert_called_once_with(123, "validation-failed")
        mock_pr_adapter.add_comment.assert_called_once()

    def test_push_rejected_when_all_mode_and_untracked_present(
        self, mock_label_adapter, mock_pr_adapter, mock_git_adapter, event_bus, worktree_with_completion
    ):
        config = Config()
        config.validation.pre_push_dirty_check = "all"
        processor = CompletionProcessor(
            label_adapter=mock_label_adapter,
            pr_adapter=mock_pr_adapter,
            git_adapter=mock_git_adapter,
            event_bus=event_bus,
            session_output=FileSystemSessionOutput(),
            label_config={},
            config=config,
        )
        mock_git_adapter.has_uncommitted_changes.return_value = True
        mock_git_adapter.list_dirty_files.return_value = ["tmp.out"]
        record = make_record(
            outcome=CompletionOutcome.COMPLETED,
            requested_actions=[RequestedAction.PUSH_BRANCH],
            summary="Done",
        )
        worktree = worktree_with_completion(record)

        result = processor.process(worktree, issue_number=123, issue_title="Test")

        assert not result.success
        assert "working tree is dirty" in result.message.lower()
        mock_git_adapter.push.assert_not_called()

    def test_push_allows_runtime_only_dirty_files(
        self, mock_label_adapter, mock_pr_adapter, mock_git_adapter, event_bus, worktree_with_completion
    ):
        config = Config()
        config.validation.pre_push_dirty_check = "tracked"
        processor = CompletionProcessor(
            label_adapter=mock_label_adapter,
            pr_adapter=mock_pr_adapter,
            git_adapter=mock_git_adapter,
            event_bus=event_bus,
            session_output=FileSystemSessionOutput(),
            label_config={},
            config=config,
        )
        mock_git_adapter.has_tracked_changes.return_value = True
        mock_git_adapter.list_dirty_files.return_value = [".issue-orchestrator/session-latest.json"]
        record = make_record(
            outcome=CompletionOutcome.COMPLETED,
            requested_actions=[RequestedAction.PUSH_BRANCH],
            summary="Done",
        )
        worktree = worktree_with_completion(record)

        result = processor.process(worktree, issue_number=123, issue_title="Test")

        assert result.success
        mock_git_adapter.push.assert_called_once()

    def test_push_allowed_when_dirty_check_off(
        self, mock_label_adapter, mock_pr_adapter, mock_git_adapter, event_bus, worktree_with_completion
    ):
        config = Config()
        config.validation.pre_push_dirty_check = "off"
        processor = CompletionProcessor(
            label_adapter=mock_label_adapter,
            pr_adapter=mock_pr_adapter,
            git_adapter=mock_git_adapter,
            event_bus=event_bus,
            session_output=FileSystemSessionOutput(),
            label_config={},
            config=config,
        )
        mock_git_adapter.has_tracked_changes.return_value = True
        record = make_record(
            outcome=CompletionOutcome.COMPLETED,
            requested_actions=[RequestedAction.PUSH_BRANCH],
            summary="Done",
        )
        worktree = worktree_with_completion(record)

        result = processor.process(worktree, issue_number=123, issue_title="Test")

        assert result.success
        mock_git_adapter.push.assert_called_once()

    def test_failed_processing_emits_failed_event(
        self, processor, event_bus, mock_git_adapter, worktree_with_completion
    ):
        """Failed processing should emit failed event."""
        mock_git_adapter.push.return_value = PushResult(
            success=False,
            branch="issue-123",
            remote="origin",
            message="Rejected",
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


class TestCompletionProcessorPublishGate:
    """Tests for publish gate validation before publishing.

    Key invariant: Cannot publish (push/PR) without validation passing.
    """

    @pytest.fixture
    def mock_publish_gate(self):
        """Mock PublishGate for testing."""
        from unittest.mock import Mock
        from issue_orchestrator.control.validation import PublishGateResult

        gate = Mock()
        gate.check = Mock(return_value=PublishGateResult(
            allowed=True,
            reason="Validation passed",
        ))
        return gate

    @pytest.fixture
    def processor_with_gate(
        self,
        mock_label_adapter,
        mock_pr_adapter,
        mock_git_adapter,
        mock_publish_gate,
    ):
        """Processor with publish gate configured."""
        return CompletionProcessor(
            label_adapter=mock_label_adapter,
            pr_adapter=mock_pr_adapter,
            git_adapter=mock_git_adapter,
            publish_gate=mock_publish_gate,
            session_output=FileSystemSessionOutput(),
        )

    @pytest.fixture
    def mock_pre_publish_gate(self):
        gate = Mock()
        gate.check.return_value = PrePublishGateResult(
            allowed=True,
            reason="Pre-push hook passed",
            command="/tmp/hooks/pre-push",
            started_at=datetime.now(timezone.utc).isoformat(),
            ended_at=datetime.now(timezone.utc).isoformat(),
            exit_code=0,
            stdout="",
            stderr="",
            hook_path="/tmp/hooks/pre-push",
            head_sha="abc123",
            ran=True,
        )
        return gate

    def test_cannot_publish_without_validation_passing(
        self, processor_with_gate, mock_publish_gate, mock_git_adapter, worktree_with_completion
    ):
        """CRITICAL: Publish actions must be blocked when validation fails.

        This test proves the invariant: cannot publish without tests_passed.
        """
        from issue_orchestrator.control.validation import PublishGateResult

        # Configure gate to fail
        mock_publish_gate.check.return_value = PublishGateResult(
            allowed=False,
            reason="Validation failed: pyright found 3 errors",
        )

        record = make_record(
            outcome=CompletionOutcome.COMPLETED,
            requested_actions=[RequestedAction.PUSH_BRANCH, RequestedAction.CREATE_PR],
            summary="Done",
        )
        worktree = worktree_with_completion(record)

        result = processor_with_gate.process(worktree, issue_number=123, issue_title="Test")

        # Processing must fail
        assert not result.success
        assert "validation failed" in result.message.lower()
        # Push must NOT have been called
        mock_git_adapter.push.assert_not_called()

    def test_publish_allowed_when_validation_passes(
        self, processor_with_gate, mock_publish_gate, mock_git_adapter, worktree_with_completion
    ):
        """Publish actions proceed when validation passes."""
        from issue_orchestrator.control.validation import PublishGateResult

        mock_publish_gate.check.return_value = PublishGateResult(
            allowed=True,
            reason="Validation passed",
        )

        record = make_record(
            outcome=CompletionOutcome.COMPLETED,
            requested_actions=[RequestedAction.PUSH_BRANCH],
            summary="Done",
        )
        worktree = worktree_with_completion(record)

        result = processor_with_gate.process(worktree, issue_number=123, issue_title="Test")

        assert result.success
        mock_git_adapter.push.assert_called_once()

    def test_non_publish_actions_bypass_gate(
        self, processor_with_gate, mock_publish_gate, mock_label_adapter, worktree_with_completion
    ):
        """Non-publish actions (labels, comments) don't require validation."""
        from issue_orchestrator.control.validation import PublishGateResult

        # Gate would fail if checked, but shouldn't be checked for label-only actions
        mock_publish_gate.check.return_value = PublishGateResult(
            allowed=False,
            reason="Would fail",
        )

        record = make_record(
            outcome=CompletionOutcome.BLOCKED,
            requested_actions=[RequestedAction.ADD_BLOCKED_LABEL],
            summary="Blocked",
        )
        worktree = worktree_with_completion(record)

        result = processor_with_gate.process(worktree, issue_number=123, issue_title="Test")

        # Label actions should succeed without gate check
        assert result.success
        mock_label_adapter.add_label.assert_called_once()

    def test_validation_failed_label_added_on_gate_failure(
        self,
        processor_with_gate,
        mock_publish_gate,
        mock_label_adapter,
        mock_git_adapter,
        mock_pr_adapter,
        worktree_with_completion,
    ):
        """When validation fails, the validation-failed label should be added to the issue."""
        from issue_orchestrator.control.validation import PublishGateResult

        # Configure gate to fail
        mock_publish_gate.check.return_value = PublishGateResult(
            allowed=False,
            reason="Validation failed: tests failed",
        )

        record = make_record(
            outcome=CompletionOutcome.COMPLETED,
            requested_actions=[RequestedAction.PUSH_BRANCH, RequestedAction.CREATE_PR],
            summary="Done",
        )
        worktree = worktree_with_completion(record)

        result = processor_with_gate.process(worktree, issue_number=123, issue_title="Test")

        # Processing must fail
        assert not result.success
        assert "validation failed" in result.message.lower()

        # validation-failed label must be added
        mock_label_adapter.add_label.assert_called_once_with(123, "validation-failed")
        mock_pr_adapter.add_comment.assert_called_once()
        comment = mock_pr_adapter.add_comment.call_args[0][1]
        assert "Validation Failed" in comment
        assert "Validation failed: tests failed" in comment

    def test_validation_failure_captured_in_session_output(
        self, processor_with_gate, mock_publish_gate, tmp_path
    ):
        """Validation failure output should be written into session output."""
        from issue_orchestrator.control.validation import PublishGateResult, ValidationRecord, ValidationRecordStore
        from issue_orchestrator.domain.models import CompletionRecord
        from issue_orchestrator.execution.session_output_adapter import FileSystemSessionOutput

        worktree = tmp_path / "worktree"
        worktree.mkdir()
        record_dir = worktree / ".issue-orchestrator"
        record_dir.mkdir(parents=True, exist_ok=True)

        completion_record = CompletionRecord(
            session_id="issue-123",
            timestamp=datetime.now().isoformat(),
            outcome=CompletionOutcome.COMPLETED,
            summary="Done",
            requested_actions=[RequestedAction.PUSH_BRANCH, RequestedAction.CREATE_PR],
        )
        (record_dir / "completion.json").write_text(json.dumps(completion_record.to_dict()))

        session_output = FileSystemSessionOutput()
        run = session_output.start_run(worktree, "issue-123", issue_number=123)

        # Validation output is written directly to session output dir
        (run.run_dir / "validation-stdout.log").write_text("validation stdout")
        (run.run_dir / "validation-stderr.log").write_text("validation stderr")

        store = ValidationRecordStore(worktree)
        validation_record = ValidationRecord(
            schema_version=1,
            suite="publish_gate",
            head_sha="abc123",
            passed=False,
            exit_code=1,
            command="make validate",
            started_at=datetime.now(timezone.utc).isoformat(),
            ended_at=datetime.now(timezone.utc).isoformat(),
            timed_out=False,
            stdout_path=str((run.run_dir / "validation-stdout.log").relative_to(worktree)),
            stderr_path=str((run.run_dir / "validation-stderr.log").relative_to(worktree)),
        )
        store.write(validation_record)

        mock_publish_gate.check.return_value = PublishGateResult(
            allowed=False,
            reason="Validation failed",
            record=validation_record,
        )

        result = processor_with_gate.process(worktree, issue_number=123, issue_title="Test")

        assert not result.success
        run_dir = session_output.find_run_dir(worktree, session_name="issue-123")
        assert run_dir is not None
        assert (run_dir / "validation-stdout.log").read_text() == "validation stdout"
        assert (run_dir / "validation-stderr.log").read_text() == "validation stderr"
        assert (run_dir / "validation-record.json").exists()
        manifest = json.loads((run_dir / "manifest.json").read_text())
        assert manifest.get("validation_record_path") == str(run_dir / "validation-record.json")
        # Verify manifest is updated with validation_passed=False for UI status derivation
        assert manifest.get("validation_passed") is False
        assert manifest.get("validation_failure_reason") == "Validation failed"
        assert "ended_at" in manifest  # Must be set so UI shows correct status

    def test_pre_publish_gate_runs_before_push_and_keeps_hooks_enabled(
        self,
        mock_label_adapter,
        mock_pr_adapter,
        mock_git_adapter,
        mock_publish_gate,
        mock_pre_publish_gate,
        worktree_with_completion,
    ):
        processor = CompletionProcessor(
            label_adapter=mock_label_adapter,
            pr_adapter=mock_pr_adapter,
            git_adapter=mock_git_adapter,
            publish_gate=mock_publish_gate,
            pre_publish_gate=mock_pre_publish_gate,
            session_output=FileSystemSessionOutput(),
        )
        record = make_record(
            outcome=CompletionOutcome.COMPLETED,
            requested_actions=[RequestedAction.PUSH_BRANCH],
            summary="Done",
        )
        worktree = worktree_with_completion(record)

        result = processor.process(worktree, issue_number=123, issue_title="Test")

        assert result.success
        mock_pre_publish_gate.check.assert_called_once_with(worktree)
        mock_git_adapter.push.assert_called_once_with(worktree, skip_hooks=False)

    def test_pre_publish_gate_failure_adds_validation_failed_and_blocks_push(
        self,
        mock_label_adapter,
        mock_pr_adapter,
        mock_git_adapter,
        mock_publish_gate,
        worktree_with_completion,
    ):
        pre_publish_gate = Mock()
        pre_publish_gate.check.return_value = PrePublishGateResult(
            allowed=False,
            reason="ERROR: Test-skipping patterns detected",
            command="/tmp/hooks/pre-push",
            started_at=datetime.now(timezone.utc).isoformat(),
            ended_at=datetime.now(timezone.utc).isoformat(),
            exit_code=1,
            stdout="ERROR: Test-skipping patterns detected\n",
            stderr="",
            hook_path="/tmp/hooks/pre-push",
            head_sha="abc123",
            ran=True,
        )
        processor = CompletionProcessor(
            label_adapter=mock_label_adapter,
            pr_adapter=mock_pr_adapter,
            git_adapter=mock_git_adapter,
            publish_gate=mock_publish_gate,
            pre_publish_gate=pre_publish_gate,
            session_output=FileSystemSessionOutput(),
        )
        record = make_record(
            outcome=CompletionOutcome.COMPLETED,
            requested_actions=[RequestedAction.PUSH_BRANCH, RequestedAction.CREATE_PR],
            summary="Done",
        )
        worktree = worktree_with_completion(record)

        result = processor.process(worktree, issue_number=123, issue_title="Test")

        assert not result.success
        assert result.failure_kind == "validation_failed"
        mock_git_adapter.push.assert_not_called()
        mock_label_adapter.add_label.assert_called_once_with(123, "validation-failed")
        comment = mock_pr_adapter.add_comment.call_args[0][1]
        assert "Validation Failed" in comment
        assert "Test-skipping patterns detected" in comment

    def test_pre_publish_gate_failure_reroutes_back_into_review_exchange(
        self,
        tmp_path,
        mock_label_adapter,
        mock_pr_adapter,
        mock_git_adapter,
        mock_publish_gate,
    ):
        coder_prompt = tmp_path / "coder.md"
        reviewer_prompt = tmp_path / "reviewer.md"
        coder_prompt.write_text("Coder prompt")
        reviewer_prompt.write_text("Reviewer prompt")
        config = Config()
        config.review_enabled = True
        config.review_exchange_mode = "via-local-loop"
        config.code_review_agent = "agent:reviewer"
        config.agents = {
            "agent:coder": AgentConfig(prompt_path=coder_prompt, ai_system="claude-code"),
            "agent:reviewer": AgentConfig(prompt_path=reviewer_prompt, ai_system="codex"),
        }

        pre_publish_gate = Mock()
        pre_publish_gate.check.return_value = PrePublishGateResult(
            allowed=False,
            reason="ERROR: Test-skipping patterns detected",
            command="/tmp/hooks/pre-push",
            started_at=datetime.now(timezone.utc).isoformat(),
            ended_at=datetime.now(timezone.utc).isoformat(),
            exit_code=1,
            stdout="",
            stderr="validation stderr\n",
            hook_path="/tmp/hooks/pre-push",
            head_sha="abc123",
            ran=True,
        )
        processor = CompletionProcessor(
            label_adapter=mock_label_adapter,
            pr_adapter=mock_pr_adapter,
            git_adapter=mock_git_adapter,
            publish_gate=mock_publish_gate,
            pre_publish_gate=pre_publish_gate,
            session_output=FileSystemSessionOutput(),
            config=config,
        )

        worktree = tmp_path / "worktree"
        worktree.mkdir()
        record_dir = worktree / ".issue-orchestrator"
        record_dir.mkdir(parents=True, exist_ok=True)
        completion_record = CompletionRecord(
            session_id="issue-123",
            timestamp=datetime.now().isoformat(),
            outcome=CompletionOutcome.COMPLETED,
            summary="Done",
            requested_actions=[RequestedAction.PUSH_BRANCH, RequestedAction.CREATE_PR],
        )
        (record_dir / "completion.json").write_text(json.dumps(completion_record.to_dict()))
        processor.session_output.start_run(worktree, "issue-123", issue_number=123)
        review_exchange = processor._review_exchange  # noqa: SLF001
        processor._run_review_exchange_loop = MagicMock(  # noqa: SLF001
            return_value=ReviewExchangeOutcome(
                status="ok",
                rounds=1,
                reason="approved",
            )
        )

        with patch.object(
            review_exchange,
            "prepare_review_exchange",
            return_value=(
                SimpleNamespace(
                    ordered_actions=[RequestedAction.PUSH_BRANCH, RequestedAction.CREATE_PR]
                ),
                None,
                None,
                False,
                False,
                False,
            ),
        ):
            result = processor.process(
                worktree,
                issue_number=123,
                issue_title="Test Issue",
                agent_label="agent:coder",
            )

        assert result.success
        assert result.review_exchange_deferred is True
        assert result.validation_failed_rerouted is True
        assert result.actions_taken == [
            "Validation failed; returned to coder rework via review exchange",
            "Review exchange passed",
        ]
        assert result.errors == []
        mock_git_adapter.push.assert_not_called()
        mock_label_adapter.add_label.assert_not_called()
        mock_pr_adapter.add_comment.assert_not_called()
        validation_record_path = processor._run_review_exchange_loop.call_args.kwargs[
            "initial_validation_record_path"
        ]
        assert validation_record_path.exists()
        record_data = json.loads(validation_record_path.read_text())
        assert record_data["passed"] is False
        assert record_data["command"] == "/tmp/hooks/pre-push"

    def test_pre_publish_gate_failure_review_exchange_halt_avoids_validation_failed_label(
        self,
        tmp_path,
        mock_label_adapter,
        mock_pr_adapter,
        mock_git_adapter,
        mock_publish_gate,
    ):
        coder_prompt = tmp_path / "coder.md"
        reviewer_prompt = tmp_path / "reviewer.md"
        coder_prompt.write_text("Coder prompt")
        reviewer_prompt.write_text("Reviewer prompt")
        config = Config()
        config.review_enabled = True
        config.review_exchange_mode = "via-local-loop"
        config.code_review_agent = "agent:reviewer"
        config.agents = {
            "agent:coder": AgentConfig(prompt_path=coder_prompt, ai_system="claude-code"),
            "agent:reviewer": AgentConfig(prompt_path=reviewer_prompt, ai_system="codex"),
        }

        pre_publish_gate = Mock()
        pre_publish_gate.check.return_value = PrePublishGateResult(
            allowed=False,
            reason="ERROR: Test-skipping patterns detected",
            command="/tmp/hooks/pre-push",
            started_at=datetime.now(timezone.utc).isoformat(),
            ended_at=datetime.now(timezone.utc).isoformat(),
            exit_code=1,
            stdout="",
            stderr="validation stderr\n",
            hook_path="/tmp/hooks/pre-push",
            head_sha="abc123",
            ran=True,
        )
        processor = CompletionProcessor(
            label_adapter=mock_label_adapter,
            pr_adapter=mock_pr_adapter,
            git_adapter=mock_git_adapter,
            publish_gate=mock_publish_gate,
            pre_publish_gate=pre_publish_gate,
            session_output=FileSystemSessionOutput(),
            config=config,
        )

        worktree = tmp_path / "worktree"
        worktree.mkdir()
        record_dir = worktree / ".issue-orchestrator"
        record_dir.mkdir(parents=True, exist_ok=True)
        completion_record = CompletionRecord(
            session_id="issue-123",
            timestamp=datetime.now().isoformat(),
            outcome=CompletionOutcome.COMPLETED,
            summary="Done",
            requested_actions=[RequestedAction.PUSH_BRANCH, RequestedAction.CREATE_PR],
        )
        (record_dir / "completion.json").write_text(json.dumps(completion_record.to_dict()))
        processor.session_output.start_run(worktree, "issue-123", issue_number=123)
        review_exchange = processor._review_exchange  # noqa: SLF001
        processor._run_review_exchange_loop = MagicMock(  # noqa: SLF001
            return_value=ReviewExchangeOutcome(
                status="stopped",
                rounds=1,
                reason="max_no_progress",
            )
        )

        with patch.object(
            review_exchange,
            "prepare_review_exchange",
            return_value=(
                SimpleNamespace(
                    ordered_actions=[RequestedAction.PUSH_BRANCH, RequestedAction.CREATE_PR]
                ),
                None,
                None,
                False,
                False,
                False,
            ),
        ):
            result = processor.process(
                worktree,
                issue_number=123,
                issue_title="Test Issue",
                agent_label="agent:coder",
            )

        assert not result.success
        assert result.review_exchange_halted is True
        assert result.failure_kind is None
        assert result.errors == ["review_exchange: stopped (max_no_progress)"]
        assert result.actions_taken == []
        mock_git_adapter.push.assert_not_called()
        mock_label_adapter.add_label.assert_not_called()
        mock_pr_adapter.add_comment.assert_not_called()

    def test_reroute_pre_publish_validation_failure_requires_session_name(
        self,
        tmp_path,
        mock_label_adapter,
        mock_pr_adapter,
        mock_git_adapter,
        mock_publish_gate,
    ):
        processor = CompletionProcessor(
            label_adapter=mock_label_adapter,
            pr_adapter=mock_pr_adapter,
            git_adapter=mock_git_adapter,
            publish_gate=mock_publish_gate,
            session_output=FileSystemSessionOutput(),
        )
        record = make_record(
            outcome=CompletionOutcome.COMPLETED,
            requested_actions=[RequestedAction.PUSH_BRANCH, RequestedAction.CREATE_PR],
        )

        result = processor._reroute_pre_publish_validation_failure_if_possible(  # noqa: SLF001
            worktree=tmp_path,
            issue_number=123,
            issue_title="Test Issue",
            session_name=None,
            agent_label="agent:coder",
            record=record,
        )

        assert result is None

    def test_validation_reroute_budget_halts_after_max_attempts_on_same_sha(
        self,
        tmp_path,
        mock_label_adapter,
        mock_pr_adapter,
        mock_git_adapter,
        mock_publish_gate,
    ):
        """The reroute path must bound consecutive attempts on the same head_sha.

        Otherwise a permanently-failing validation forms an infinite loop:
        every tick re-enters the reroute, the predicate fix sends the
        exchange off, the exchange may eventually return ok-but-still-fails,
        and we go around again. Counter is keyed per (session, head_sha)
        so SHA advancing naturally resets the budget.
        """
        config = Config()
        config.review_exchange_max_rounds = 3  # tighten for the test
        processor = CompletionProcessor(
            label_adapter=mock_label_adapter,
            pr_adapter=mock_pr_adapter,
            git_adapter=mock_git_adapter,
            publish_gate=mock_publish_gate,
            session_output=FileSystemSessionOutput(),
            config=config,
        )

        worktree = tmp_path / "worktree"
        worktree.mkdir()
        run = processor.session_output.start_run(worktree, "issue-1", issue_number=1)
        validation_record = run.run_dir / "validation-record.json"
        validation_record.write_text(
            json.dumps({"passed": False, "head_sha": "deadbeef" * 5})
        )

        record = make_record(
            outcome=CompletionOutcome.COMPLETED,
            requested_actions=[RequestedAction.PUSH_BRANCH, RequestedAction.CREATE_PR],
        )

        # Stub the inner exchange call so we observe budget enforcement at
        # this layer specifically — the test should not depend on the
        # downstream exchange's own bounds firing.
        run_review_exchange_if_needed = MagicMock(
            return_value=("via-local-loop", None, False, True)  # deferred
        )
        processor._review_exchange.run_review_exchange_if_needed = (  # noqa: SLF001
            run_review_exchange_if_needed
        )

        # Three attempts within budget, all return success(deferred).
        for _ in range(3):
            result = processor._reroute_pre_publish_validation_failure_if_possible(  # noqa: SLF001
                worktree=worktree,
                issue_number=1,
                issue_title="Test",
                session_name=run.session_name,
                agent_label="agent:coder",
                record=record,
            )
            assert result is not None
            assert result.success is True
            assert result.review_exchange_halted is False

        # Fourth attempt exceeds the budget → halt with explicit failure.
        result = processor._reroute_pre_publish_validation_failure_if_possible(  # noqa: SLF001
            worktree=worktree,
            issue_number=1,
            issue_title="Test",
            session_name=run.session_name,
            agent_label="agent:coder",
            record=record,
        )
        assert result is not None
        assert result.success is False
        assert result.review_exchange_halted is True
        assert "budget is exhausted" in result.message
        # The exchange must not be invoked once the budget is exhausted.
        assert run_review_exchange_if_needed.call_count == 3

    def test_validation_reroute_budget_does_not_count_polling_ticks(
        self,
        tmp_path,
        mock_label_adapter,
        mock_pr_adapter,
        mock_git_adapter,
        mock_publish_gate,
    ):
        """While the background review-exchange job is still running,
        the reroute is just polling — no new attempt was made. Counting
        these polls would let a slow exchange exhaust the budget before
        it has a chance to finish, halting issues that are actually
        making progress in the background."""
        from issue_orchestrator.control.background_job_supervisor import (
            BackgroundJobSupervisor,
        )

        config = Config()
        config.review_exchange_max_rounds = 2
        # A fake runner that always reports the job as running, so
        # ``is_review_exchange_running`` returns True every tick.
        fake_runner = MagicMock()
        fake_runner.is_running.return_value = True
        fake_runner.submit.return_value = False
        fake_runner.take_failure.return_value = None
        fake_runner.drain_completed.return_value = []
        supervisor = BackgroundJobSupervisor(fake_runner)

        processor = CompletionProcessor(
            label_adapter=mock_label_adapter,
            pr_adapter=mock_pr_adapter,
            git_adapter=mock_git_adapter,
            publish_gate=mock_publish_gate,
            session_output=FileSystemSessionOutput(),
            config=config,
            background_job_supervisor=supervisor,
        )

        worktree = tmp_path / "worktree"
        worktree.mkdir()
        run = processor.session_output.start_run(worktree, "issue-1", issue_number=1)
        validation_record = run.run_dir / "validation-record.json"
        validation_record.write_text(json.dumps({"passed": False, "head_sha": "aaa"}))

        record = make_record(
            outcome=CompletionOutcome.COMPLETED,
            requested_actions=[RequestedAction.PUSH_BRANCH, RequestedAction.CREATE_PR],
        )
        # Stub the inner exchange call to mirror what the real one does on
        # a polling tick: returns deferred=True without doing new work.
        processor._review_exchange.run_review_exchange_if_needed = MagicMock(  # noqa: SLF001
            return_value=("via-local-loop", None, False, True)
        )

        # Many polling ticks well past the configured budget — none halt.
        for _ in range(10):
            result = processor._reroute_pre_publish_validation_failure_if_possible(  # noqa: SLF001
                worktree=worktree,
                issue_number=1,
                issue_title="Test",
                session_name=run.session_name,
                agent_label="agent:coder",
                record=record,
            )
            assert result is not None
            assert result.success is True
            assert result.review_exchange_halted is False

    def test_validation_reroute_budget_resets_when_sha_advances(
        self,
        tmp_path,
        mock_label_adapter,
        mock_pr_adapter,
        mock_git_adapter,
        mock_publish_gate,
    ):
        """SHA advancing means the coder made progress; budget should reset."""
        config = Config()
        config.review_exchange_max_rounds = 2
        processor = CompletionProcessor(
            label_adapter=mock_label_adapter,
            pr_adapter=mock_pr_adapter,
            git_adapter=mock_git_adapter,
            publish_gate=mock_publish_gate,
            session_output=FileSystemSessionOutput(),
            config=config,
        )

        worktree = tmp_path / "worktree"
        worktree.mkdir()
        run = processor.session_output.start_run(worktree, "issue-1", issue_number=1)
        validation_record = run.run_dir / "validation-record.json"

        record = make_record(
            outcome=CompletionOutcome.COMPLETED,
            requested_actions=[RequestedAction.PUSH_BRANCH, RequestedAction.CREATE_PR],
        )
        processor._review_exchange.run_review_exchange_if_needed = MagicMock(  # noqa: SLF001
            return_value=("via-local-loop", None, False, True)
        )

        # Two attempts on SHA "aaa" — within budget.
        validation_record.write_text(json.dumps({"passed": False, "head_sha": "aaa"}))
        for _ in range(2):
            result = processor._reroute_pre_publish_validation_failure_if_possible(  # noqa: SLF001
                worktree=worktree,
                issue_number=1,
                issue_title="Test",
                session_name=run.session_name,
                agent_label="agent:coder",
                record=record,
            )
            assert result is not None and result.success is True

        # SHA advances. Budget should reset, so two more attempts succeed.
        validation_record.write_text(json.dumps({"passed": False, "head_sha": "bbb"}))
        for _ in range(2):
            result = processor._reroute_pre_publish_validation_failure_if_possible(  # noqa: SLF001
                worktree=worktree,
                issue_number=1,
                issue_title="Test",
                session_name=run.session_name,
                agent_label="agent:coder",
                record=record,
            )
            assert result is not None and result.success is True

        # Now SHA "bbb"'s budget is at 2; a third attempt halts.
        result = processor._reroute_pre_publish_validation_failure_if_possible(  # noqa: SLF001
            worktree=worktree,
            issue_number=1,
            issue_title="Test",
            session_name=run.session_name,
            agent_label="agent:coder",
            record=record,
        )
        assert result is not None
        assert result.success is False
        assert result.review_exchange_halted is True


def test_cleanup_failure_posts_diagnostic_comment(
    tmp_path,
    processor,
    mock_pr_adapter,
):
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    completion_dir = worktree / ".issue-orchestrator"
    completion_dir.mkdir()

    record = make_record(CompletionOutcome.COMPLETED, [])
    record_path = worktree / COMPLETION_RECORD_PATH
    record_path.write_text(json.dumps(record.to_dict()))

    with patch.object(CompletionProcessor, "cleanup_record", return_value=False):
        with patch(
            "issue_orchestrator.control.completion_failure_reporting.write_issue_diagnostic"
        ) as mock_write:
            mock_write.return_value = DiagnosticReference(
                worktree_name="worktree",
                relative_path=".issue-orchestrator/diagnostics/diag.json",
            )

            processor.process(worktree, 123, "Test issue")

    mock_pr_adapter.add_comment.assert_called_once()
    comment = mock_pr_adapter.add_comment.call_args[0][1]
    assert "Diagnostic file" in comment
    assert "Worktree: `worktree`" in comment


class TestRunScopedArtifacts:
    def test_process_preserves_completion_record_in_run_dir(
        self,
        tmp_path,
        mock_label_adapter,
        mock_pr_adapter,
        mock_git_adapter,
        event_bus,
    ) -> None:
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        session_output = FileSystemSessionOutput()
        run = session_output.start_run(
            worktree,
            "coding-1",
            issue_number=123,
            agent_label="agent:web",
            completion_path=".issue-orchestrator/sessions/20260201-000000Z__coding-1/completion-agent_web.json",
        )
        completion_rel = f".issue-orchestrator/sessions/{run.run_dir.name}/completion-agent_web.json"
        completion_path = worktree / completion_rel
        record = make_record(
            outcome=CompletionOutcome.COMPLETED,
            requested_actions=[],
            implementation="Implemented the issue",
            problems="None",
        )
        completion_path.write_text(json.dumps(record.to_dict()))

        processor = CompletionProcessor(
            label_adapter=mock_label_adapter,
            pr_adapter=mock_pr_adapter,
            git_adapter=mock_git_adapter,
            session_output=session_output,
            event_bus=event_bus,
            label_config={},
        )

        result = processor.process(
            worktree,
            issue_number=123,
            issue_title="Test Issue",
            completion_path=completion_rel,
            agent_label="agent:web",
        )

        preserved_path = run.run_dir / "completion-record.json"
        manifest = json.loads((run.run_dir / "manifest.json").read_text())

        assert result.success is True
        assert result.completion_record_path == str(preserved_path)
        assert preserved_path.exists()
        assert not completion_path.exists()
        assert manifest["completion_record_path"] == str(preserved_path)

    def test_review_exchange_summary_is_stored_in_review_run_dir(
        self,
        tmp_path,
        mock_label_adapter,
        mock_pr_adapter,
        mock_git_adapter,
        event_bus,
    ) -> None:
        coder_prompt = tmp_path / "coder.md"
        reviewer_prompt = tmp_path / "reviewer.md"
        coder_prompt.write_text("Coder prompt")
        reviewer_prompt.write_text("Reviewer prompt")

        config = Config()
        config.review_enabled = True
        config.review_exchange_mode = "via-local-loop"
        config.code_review_agent = "agent:reviewer"
        config.agents = {
            "agent:coder": AgentConfig(prompt_path=coder_prompt, ai_system="claude-code"),
            "agent:reviewer": AgentConfig(prompt_path=reviewer_prompt, ai_system="codex"),
        }

        worktree = tmp_path / "worktree"
        worktree.mkdir()
        session_output = FileSystemSessionOutput()
        coding_run = session_output.start_run(
            worktree,
            "coding-1",
            issue_number=123,
            agent_label="agent:coder",
            completion_path=".issue-orchestrator/sessions/20260201-000000Z__coding-1/completion-agent_coder.json",
        )
        completion_rel = f".issue-orchestrator/sessions/{coding_run.run_dir.name}/completion-agent_coder.json"
        completion_path = worktree / completion_rel
        record = make_record(
            outcome=CompletionOutcome.COMPLETED,
            requested_actions=[RequestedAction.PUSH_BRANCH, RequestedAction.CREATE_PR],
            implementation="Implemented the issue",
            problems="None",
        )
        completion_path.write_text(json.dumps(record.to_dict()))

        review_run = session_output.start_run(
            worktree,
            "review-exchange-123-20260201T000000000000Z",
            issue_number=123,
            agent_label="agent:coder",
        )
        exchange_dir = review_run.run_dir / "review-exchange"
        exchange_dir.mkdir(parents=True, exist_ok=True)
        (review_run.run_dir / "validation-record.json").write_text(json.dumps({"passed": True}))

        processor = CompletionProcessor(
            label_adapter=mock_label_adapter,
            pr_adapter=mock_pr_adapter,
            git_adapter=mock_git_adapter,
            session_output=session_output,
            event_bus=event_bus,
            label_config={
                "code_reviewed": "code-reviewed",
                "code_review": "needs-code-review",
            },
            config=config,
        )
        processor._run_review_exchange_loop = MagicMock(  # noqa: SLF001
            return_value=ReviewExchangeOutcome(
                status="ok",
                rounds=1,
                reason="reviewer_ok",
                exchange_dir=exchange_dir,
                summary={
                    "completed_rounds": 1,
                    "status": "ok",
                    "response_text": "Looks good",
                    "timestamp": "2026-02-01T00:00:00Z",
                },
            )
        )

        result = processor.process(
            worktree,
            issue_number=123,
            issue_title="Test Issue",
            completion_path=completion_rel,
            agent_label="agent:coder",
        )

        assert result.success is True
        assert (review_run.run_dir / "review-exchange" / "summary.json").exists()
        assert not (coding_run.run_dir / "review-exchange" / "summary.json").exists()

    def test_review_exchange_preserves_completion_record_before_loop_starts(
        self,
        tmp_path,
        mock_label_adapter,
        mock_pr_adapter,
        mock_git_adapter,
        event_bus,
    ) -> None:
        coder_prompt = tmp_path / "coder.md"
        reviewer_prompt = tmp_path / "reviewer.md"
        coder_prompt.write_text("Coder prompt")
        reviewer_prompt.write_text("Reviewer prompt")

        config = Config()
        config.review_enabled = True
        config.review_exchange_mode = "via-local-loop"
        config.code_review_agent = "agent:reviewer"
        config.agents = {
            "agent:coder": AgentConfig(prompt_path=coder_prompt, ai_system="claude-code"),
            "agent:reviewer": AgentConfig(prompt_path=reviewer_prompt, ai_system="codex"),
        }

        worktree = tmp_path / "worktree"
        worktree.mkdir()
        session_output = FileSystemSessionOutput()
        coding_run = session_output.start_run(
            worktree,
            "coding-1",
            issue_number=123,
            agent_label="agent:coder",
            completion_path=".issue-orchestrator/sessions/20260201-000000Z__coding-1/completion-agent_coder.json",
        )
        completion_rel = f".issue-orchestrator/sessions/{coding_run.run_dir.name}/completion-agent_coder.json"
        completion_path = worktree / completion_rel
        record = make_record(
            outcome=CompletionOutcome.COMPLETED,
            requested_actions=[RequestedAction.PUSH_BRANCH, RequestedAction.CREATE_PR],
            implementation="Implemented the issue",
            problems="None",
        )
        completion_path.write_text(json.dumps(record.to_dict()))

        review_run = session_output.start_run(
            worktree,
            "review-exchange-123-20260201T000000000000Z",
            issue_number=123,
            agent_label="agent:coder",
        )
        exchange_dir = review_run.run_dir / "review-exchange"
        exchange_dir.mkdir(parents=True, exist_ok=True)

        processor = CompletionProcessor(
            label_adapter=mock_label_adapter,
            pr_adapter=mock_pr_adapter,
            git_adapter=mock_git_adapter,
            session_output=session_output,
            event_bus=event_bus,
            label_config={
                "code_reviewed": "code-reviewed",
                "code_review": "needs-code-review",
            },
            config=config,
        )

        def run_exchange(*args, **kwargs):  # noqa: ANN002, ANN003
            assert (coding_run.run_dir / "completion-record.json").exists()
            return ReviewExchangeOutcome(
                status="ok",
                rounds=1,
                reason="reviewer_ok",
                exchange_dir=exchange_dir,
                summary={
                    "completed_rounds": 1,
                    "status": "ok",
                    "response_text": "Looks good",
                    "timestamp": "2026-02-01T00:00:00Z",
                },
            )

        processor._run_review_exchange_loop = MagicMock(side_effect=run_exchange)  # noqa: SLF001

        result = processor.process(
            worktree,
            issue_number=123,
            issue_title="Test Issue",
            completion_path=completion_rel,
            agent_label="agent:coder",
        )

        assert result.success is True
        assert (coding_run.run_dir / "completion-record.json").exists()
