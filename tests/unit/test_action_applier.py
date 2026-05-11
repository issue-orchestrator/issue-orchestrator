"""Unit tests for ActionApplier."""

import logging
import pytest
from unittest.mock import MagicMock, Mock
from pathlib import Path

from issue_orchestrator.control.action_applier import ActionApplier
from issue_orchestrator.control.claim_gate import ClaimGate, ClaimLostError
from issue_orchestrator.control.actions import (
    ActionType,
    ActionResultType,
    AddLabelAction,
    RemoveLabelAction,
    AddCommentAction,
    SyncLabelsAction,
    LaunchSessionAction,
    LaunchValidationRetryAction,
    StopSessionAction,
    QueueReviewAction,
    EscalateToHumanAction,
    CreateTriageIssueAction,
    CleanupSessionAction,
    RemoveWorktreeAction,
    ReconcileHistoryEntryAction,
    SupersedePullRequestAction,
    CloseIssueAction,
)
from issue_orchestrator.control.session_history import SessionHistoryOwner
from issue_orchestrator.control.session_manager import SessionType
from issue_orchestrator.domain.models import Issue, Session, AgentConfig, SessionHistoryEntry
from issue_orchestrator.events import EventName
from issue_orchestrator.ports.claim_manager import ClaimManager


@pytest.fixture
def mock_labels():
    """Create a mock LabelSet."""
    labels = MagicMock()
    labels.has_label.return_value = False
    return labels


@pytest.fixture
def mock_sessions():
    """Create a mock SessionManager."""
    sessions = MagicMock()
    sessions.exists.return_value = False
    sessions.start.return_value = True
    return sessions


@pytest.fixture
def mock_events():
    """Create a mock EventSink."""
    events = MagicMock()
    return events


@pytest.fixture
def mock_repository_host():
    """Create a mock RepositoryHost."""
    repo = MagicMock()
    repo.create_issue.return_value = 123
    return repo


@pytest.fixture
def mock_fresh_issue_reader():
    """Create a mock FreshIssueReader."""
    reader = MagicMock()
    reader.read_issue_labels.return_value = []
    return reader


@pytest.fixture
def mock_worktree_manager():
    """Create a mock WorktreeManager."""
    wm = MagicMock()
    return wm


@pytest.fixture
def applier(
    mock_labels,
    mock_sessions,
    mock_events,
    mock_repository_host,
    mock_worktree_manager,
    mock_fresh_issue_reader,
):
    """Create an ActionApplier with mocks."""
    return ActionApplier(
        labels=mock_labels,
        sessions=mock_sessions,
        events=mock_events,
        repository_host=mock_repository_host,
        worktree_manager=mock_worktree_manager,
        fresh_issue_reader=mock_fresh_issue_reader,
        reconcile=False,
    )


class TestAddLabelAction:
    """Tests for ADD_LABEL action."""

    def test_add_label_success(self, applier, mock_labels):
        """Test successful label addition."""
        action = AddLabelAction(
            issue_number=123,
            label="in-progress",
            reason="launch session",
        )

        result = applier.apply(action)

        assert result.success
        mock_labels.add_label.assert_called_once_with(123, "in-progress")

    def test_add_label_logs_reason_on_success(self, applier, caplog):
        """Successful label mutations should include the triggering reason."""
        action = AddLabelAction(
            issue_number=123,
            label="publish-failed",
            reason="publish blocked: branch missing",
        )

        with caplog.at_level(logging.INFO):
            result = applier.apply(action)

        assert result.success
        assert any(
            "Label mutation: op=add outcome=applied label=publish-failed reason=publish blocked: branch missing"
            in message
            for message in caplog.messages
        )

    def test_add_label_failure(self, applier, mock_labels):
        """Test label addition failure."""
        mock_labels.add_label.side_effect = Exception("API error")
        action = AddLabelAction(issue_number=123, label="in-progress")

        result = applier.apply(action)

        assert not result.success
        assert "API error" in result.error

    def test_add_label_noop_when_already_present(self, applier, mock_labels):
        """Skip add_label mutation when label is already present."""
        mock_labels.has_label.return_value = True
        action = AddLabelAction(
            issue_number=123,
            label="in-progress",
            reason="launch session",
        )

        result = applier.apply(action)

        assert result.success
        assert result.details["no_op"] is True
        mock_labels.add_label.assert_not_called()

    def test_add_label_raises_when_claim_lost(self, applier, mock_labels, mock_events):
        """Claim verification blocks external mutation when ownership is lost."""
        claim_manager = MagicMock(spec=ClaimManager)
        claim_manager.check_winner.return_value = False

        applier.claim_gate = ClaimGate(claim_manager, mock_events)
        applier.lease_id_lookup = lambda issue_number: "lease-123" if issue_number == 123 else None
        action = AddLabelAction(issue_number=123, label="in-progress")

        with pytest.raises(ClaimLostError, match="Claim lost for issue #123 before add_label"):
            applier.apply(action)

        claim_manager.check_winner.assert_called_once_with(123, "lease-123")
        mock_labels.add_label.assert_not_called()


class TestRemoveLabelAction:
    """Tests for REMOVE_LABEL action."""

    def test_remove_label_success(self, applier, mock_labels):
        """Test successful label removal."""
        mock_labels.has_label.return_value = True
        action = RemoveLabelAction(
            issue_number=123,
            label="in-progress",
            reason="session completed",
        )

        result = applier.apply(action)

        assert result.success
        mock_labels.remove_label.assert_called_once_with(123, "in-progress")

    def test_remove_label_logs_reason_on_failure(self, applier, mock_labels, caplog):
        """Failed label removals should log the reason and error detail."""
        mock_labels.has_label.return_value = True
        mock_labels.remove_label.side_effect = Exception("API error")
        action = RemoveLabelAction(
            issue_number=123,
            label="pr-pending",
            reason="rework needed for PR #77",
        )

        with caplog.at_level(logging.INFO):
            result = applier.apply(action)

        assert not result.success
        assert any(
            "Label mutation: op=remove outcome=failed label=pr-pending reason=rework needed for PR #77 detail=API error"
            in message
            for message in caplog.messages
        )

    def test_remove_label_failure(self, applier, mock_labels):
        """Test label removal failure."""
        mock_labels.has_label.return_value = True
        mock_labels.remove_label.side_effect = Exception("API error")
        action = RemoveLabelAction(issue_number=123, label="in-progress")

        result = applier.apply(action)

        assert not result.success
        assert "API error" in result.error

    def test_remove_label_noop_when_already_absent(self, applier, mock_labels):
        """Skip remove_label mutation when label is already absent."""
        applier.reconcile = True
        applier.fresh_issue_reader.read_issue_labels.return_value = []
        mock_labels.has_label.return_value = False
        action = RemoveLabelAction(issue_number=123, label="in-progress")

        result = applier.apply(action)

        assert result.success
        assert result.details["no_op"] is True
        mock_labels.remove_label.assert_not_called()


class TestReconcileHistoryEntryAction:
    """Tests for RECONCILE_HISTORY_ENTRY action."""

    def test_reconcile_history_entry_mutates_history_and_emits_event(self, applier, mock_events):
        """History reconciliation is applied through the history owner."""
        entry = SessionHistoryEntry(
            issue_number=228,
            title="Shared cache read misses",
            agent_type="agent:backend",
            status="completed",
            runtime_minutes=0,
            pr_url="https://github.com/test/repo/pull/318",
            status_reason="Recovered awaiting merge state on startup",
        )
        applier.history_owner = SessionHistoryOwner([entry])
        action = ReconcileHistoryEntryAction(
            issue_number=228,
            pr_number=318,
            pr_url="https://github.com/test/repo/pull/318",
            status="merged",
            source="pull_request",
            issue_key="M1-228",
            reason="PR merged; awaiting merge reconciled",
        )

        result = applier.apply(action)

        assert result.success
        assert entry.status == "merged"
        assert entry.status_reason == "PR merged; awaiting merge reconciled"
        published = [call.args[0] for call in mock_events.publish.call_args_list]
        history_events = [
            evt for evt in published
            if evt.name == EventName.HISTORY_RECONCILED.value
        ]
        assert len(history_events) == 1
        event = history_events[0]
        assert event.data["issue_number"] == 228
        assert event.data["issue_key"] == "M1-228"
        assert event.data["previous_status"] == "completed"
        assert event.data["status"] == "merged"

        # When the awaiting-merge reconciliation lands on the "merged"
        # terminal state, the orchestrator must also publish REVIEW_MERGED
        # so the user-facing timeline carries a "PR merged" event. Without
        # this, the dashboard sees only a debug-only HISTORY_RECONCILED
        # record after a successful merge.
        merged_events = [
            evt for evt in published
            if evt.name == EventName.REVIEW_MERGED.value
        ]
        assert len(merged_events) == 1, (
            "Expected exactly one REVIEW_MERGED event for a merged-status "
            f"reconciliation; saw events: {[e.name for e in published]}"
        )
        merged = merged_events[0]
        assert merged.data["issue_number"] == 228
        assert merged.data["pr_number"] == 318
        assert merged.data["pr_url"] == "https://github.com/test/repo/pull/318"
        assert merged.data["issue_key"] == "M1-228"
        assert merged.data["source"] == "pull_request"

    def test_reconcile_history_entry_closed_does_not_emit_review_merged(
        self,
        applier,
        mock_events,
    ):
        """Closed (not merged) PRs must not produce a REVIEW_MERGED event.

        Closed-without-merge is a different end state: the user-facing
        timeline should not show a "PR merged" event when the PR was
        actually abandoned. HISTORY_RECONCILED still fires (so the
        orchestrator records the reconciliation) but REVIEW_MERGED must
        not.
        """
        entry = SessionHistoryEntry(
            issue_number=229,
            title="Abandoned PR",
            agent_type="agent:backend",
            status="completed",
            runtime_minutes=0,
            pr_url="https://github.com/test/repo/pull/319",
            status_reason="Recovered awaiting merge state on startup",
        )
        applier.history_owner = SessionHistoryOwner([entry])
        action = ReconcileHistoryEntryAction(
            issue_number=229,
            pr_number=319,
            pr_url="https://github.com/test/repo/pull/319",
            status="closed",
            source="pull_request",
            issue_key="M1-229",
            reason="PR closed without merge; awaiting merge reconciled",
        )

        result = applier.apply(action)

        assert result.success
        published = [call.args[0] for call in mock_events.publish.call_args_list]
        names = [evt.name for evt in published]
        assert EventName.HISTORY_RECONCILED.value in names
        assert EventName.REVIEW_MERGED.value not in names, (
            "REVIEW_MERGED leaked from a closed-without-merge reconciliation; "
            f"events published: {names}"
        )

    def test_reconcile_history_entry_requires_history_owner(self, applier):
        """Applying a history reconciliation without an owner fails loudly."""
        action = ReconcileHistoryEntryAction(
            issue_number=228,
            pr_number=318,
            pr_url="https://github.com/test/repo/pull/318",
            status="closed",
            source="pull_request",
            reason="PR closed; awaiting merge reconciled",
        )

        result = applier.apply(action)

        assert not result.success
        assert result.error == "Session history owner is not configured"

    def test_reconcile_history_entry_noops_without_event_when_latest_match_terminal(
        self,
        applier,
        mock_events,
    ):
        """An already-terminal latest history entry is idempotent and quiet."""
        entry = SessionHistoryEntry(
            issue_number=228,
            title="Shared cache read misses",
            agent_type="agent:backend",
            status="merged",
            runtime_minutes=0,
            pr_url="https://github.com/test/repo/pull/318",
            status_reason="PR merged; awaiting merge reconciled",
        )
        applier.history_owner = SessionHistoryOwner([entry])
        action = ReconcileHistoryEntryAction(
            issue_number=228,
            pr_number=318,
            pr_url="https://github.com/test/repo/pull/318",
            status="merged",
            source="pull_request",
            reason="PR merged; awaiting merge reconciled",
        )

        result = applier.apply(action)

        assert result.success
        assert result.details["no_op"] is True
        assert result.details["noop_reason"] == "not_reconcilable"
        assert result.details["current_status"] == "merged"
        event_names = [call.args[0].name for call in mock_events.publish.call_args_list]
        assert EventName.HISTORY_RECONCILED.value not in event_names

    def test_reconcile_history_entry_warns_when_matching_history_entry_missing(
        self,
        applier,
        caplog,
    ):
        """A missing history entry is a visible no-op, not a silent idempotency path."""
        applier.history_owner = SessionHistoryOwner([])
        action = ReconcileHistoryEntryAction(
            issue_number=228,
            pr_number=318,
            pr_url="https://github.com/test/repo/pull/318",
            status="closed",
            source="pull_request",
            reason="PR closed; awaiting merge reconciled",
        )

        with caplog.at_level(logging.WARNING):
            result = applier.apply(action)

        assert result.success
        assert result.details["no_op"] is True
        assert result.details["noop_reason"] == "missing"
        assert "history reconciliation missing entry" in caplog.text

    def test_reconcile_history_entry_releases_pair_on_merged(
        self, applier, mock_events,
    ):
        """PR-merge release boundary (ADR 0026 / B2 review feedback,
        PR #6212 finding 1). When awaiting-merge reconciliation flips
        the entry to ``merged``, the persistent exchange pair must be
        released — otherwise a successfully merged issue keeps its
        coder/reviewer subprocesses alive until orchestrator shutdown
        even though no more exchanges can occur for it.
        """
        from unittest.mock import MagicMock

        entry = SessionHistoryEntry(
            issue_number=228,
            title="Shared cache read misses",
            agent_type="agent:backend",
            status="completed",
            runtime_minutes=0,
            pr_url="https://github.com/test/repo/pull/318",
            status_reason="Recovered awaiting merge state on startup",
        )
        applier.history_owner = SessionHistoryOwner([entry])
        applier.pair_registry = MagicMock(name="pair_registry")
        action = ReconcileHistoryEntryAction(
            issue_number=228,
            pr_number=318,
            pr_url="https://github.com/test/repo/pull/318",
            status="merged",
            source="pull_request",
            reason="PR merged; awaiting merge reconciled",
        )

        result = applier.apply(action)

        assert result.success
        applier.pair_registry.release.assert_called_once_with(
            228, reason="issue-completed",
        )

    def test_reconcile_history_entry_releases_pair_on_closed(
        self, applier,
    ):
        """An issue closed without merge (e.g. abandoned PR) is also
        terminal — the pair has nothing left to do, so the same
        ``issue-completed`` release fires."""
        from unittest.mock import MagicMock

        entry = SessionHistoryEntry(
            issue_number=228,
            title="Shared cache read misses",
            agent_type="agent:backend",
            status="completed",
            runtime_minutes=0,
            pr_url="https://github.com/test/repo/pull/318",
            status_reason="Recovered awaiting merge state on startup",
        )
        applier.history_owner = SessionHistoryOwner([entry])
        applier.pair_registry = MagicMock(name="pair_registry")
        action = ReconcileHistoryEntryAction(
            issue_number=228,
            pr_number=318,
            pr_url="https://github.com/test/repo/pull/318",
            status="closed",
            source="pull_request",
            reason="PR closed without merge",
        )

        applier.apply(action)

        applier.pair_registry.release.assert_called_once_with(
            228, reason="issue-completed",
        )

    def test_reconcile_history_entry_does_not_release_on_noop_path(
        self, applier,
    ):
        """If the history entry is already terminal (idempotent
        no-op), the reconcile action returns early without firing the
        HISTORY_RECONCILED event — and must NOT call release a second
        time. Otherwise an already-released pair would receive a
        second release call (idempotent, but noisy in logs)."""
        from unittest.mock import MagicMock

        entry = SessionHistoryEntry(
            issue_number=228,
            title="Shared cache read misses",
            agent_type="agent:backend",
            status="merged",  # already terminal
            runtime_minutes=0,
            pr_url="https://github.com/test/repo/pull/318",
            status_reason="PR merged; awaiting merge reconciled",
        )
        applier.history_owner = SessionHistoryOwner([entry])
        applier.pair_registry = MagicMock(name="pair_registry")
        action = ReconcileHistoryEntryAction(
            issue_number=228,
            pr_number=318,
            pr_url="https://github.com/test/repo/pull/318",
            status="merged",
            source="pull_request",
            reason="PR merged; awaiting merge reconciled",
        )

        applier.apply(action)

        applier.pair_registry.release.assert_not_called()


class TestSyncLabelsAction:
    """Tests for SYNC_LABELS action."""

    def test_sync_labels_add_and_remove(self, applier, mock_labels):
        """Test syncing labels - adding and removing."""
        action = SyncLabelsAction(
            issue_number=123,
            add_labels=("in-progress",),
            remove_labels=("ready",),
        )

        result = applier.apply(action)

        assert result.success
        mock_labels.add_label.assert_called_once_with(123, "in-progress")
        mock_labels.remove_label.assert_called_once_with(123, "ready")

    def test_sync_labels_partial_failure(self, applier, mock_labels):
        """Test sync labels with partial failure."""
        mock_labels.remove_label.side_effect = Exception("API error")
        action = SyncLabelsAction(
            issue_number=123,
            add_labels=("in-progress",),
            remove_labels=("ready",),
        )

        result = applier.apply(action)

        assert not result.success
        assert "remove ready" in result.error

    def test_sync_labels_contributes_to_mutation_summary(self, applier):
        """SYNC_LABELS should increment add/remove counters in batch summary."""
        action = SyncLabelsAction(
            issue_number=123,
            add_labels=("in-progress",),
            remove_labels=("ready",),
        )

        applier.apply_all([action])

        summary_events = [
            call.args[0]
            for call in applier.events.publish.call_args_list
            if getattr(call.args[0], "name", None) == str(EventName.LABEL_MUTATION_SUMMARY)
        ]
        assert len(summary_events) == 1
        payload = summary_events[0].data
        assert payload["label_add_attempted"] == 1
        assert payload["label_remove_attempted"] == 1
        assert payload["label_mutation_applied"] == 2
        assert payload["label_mutation_failed"] == 0


class TestLaunchSessionAction:
    """Tests for LAUNCH_SESSION action."""

    def test_launch_session_with_callback(self, applier, mock_sessions, tmp_path):
        """Test launching session via callback."""
        # Create a mock session launcher callback
        mock_session = MagicMock()
        mock_session.terminal_id = "issue-123"
        mock_session.issue.number = 123

        callback = MagicMock(return_value=mock_session)
        applier.session_launcher = callback

        action = LaunchSessionAction(
            session_type=SessionType.ISSUE,
            number=123,
        )

        result = applier.apply(action)

        assert result.success
        callback.assert_called_once_with(SessionType.ISSUE, 123)
        assert result.details["session_name"] == "issue-123"
        assert result.details["issue_number"] == 123

    def test_launch_session_callback_fails(self, applier):
        """Test launch session when callback returns None."""
        callback = MagicMock(return_value=None)
        applier.session_launcher = callback

        action = LaunchSessionAction(
            session_type=SessionType.ISSUE,
            number=123,
        )

        result = applier.apply(action)

        assert not result.success
        assert "Failed to launch" in result.error

    def test_launch_session_no_callback_no_command(self, applier):
        """Test launch session without callback or command fails."""
        action = LaunchSessionAction(
            session_type=SessionType.ISSUE,
            number=123,
        )

        result = applier.apply(action)

        assert not result.success
        assert "No session_launcher callback" in result.error

    def test_launch_session_fallback_with_command(self, applier, mock_sessions, tmp_path):
        """Test launch session fallback when command provided."""
        action = LaunchSessionAction(
            session_type=SessionType.ISSUE,
            number=123,
            command="claude work",
            working_dir=str(tmp_path),
            title="Issue #123",
        )

        result = applier.apply(action)

        assert result.success
        mock_sessions.start.assert_called_once()

    def test_launch_session_already_running(self, applier, mock_sessions, tmp_path):
        """Test launch session when already running."""
        mock_sessions.exists.return_value = True

        action = LaunchSessionAction(
            session_type=SessionType.ISSUE,
            number=123,
            command="claude work",
            working_dir=str(tmp_path),
        )

        result = applier.apply(action)

        assert result.result_type == ActionResultType.SKIPPED
        assert "already running" in result.details.get("skip_reason", "")


class TestLaunchValidationRetryAction:
    """Tests for LAUNCH_VALIDATION_RETRY action."""

    def test_launch_validation_retry_with_callback(self, applier):
        """Test launching validation retry via callback."""
        mock_session = MagicMock()
        mock_session.terminal_id = "issue-123"
        mock_session.issue.number = 123
        callback = MagicMock(return_value=mock_session)
        applier.validation_retry_launcher = callback

        result = applier.apply(LaunchValidationRetryAction(issue_number=123, retry_count=1))

        assert result.success
        callback.assert_called_once_with(123)
        assert result.details["session_name"] == "issue-123"

    def test_launch_validation_retry_without_callback_fails(self, applier):
        """Validation retry launch requires an orchestrator callback."""
        result = applier.apply(LaunchValidationRetryAction(issue_number=123, retry_count=1))

        assert not result.success
        assert "No validation_retry_launcher callback configured" in result.error


class TestStopSessionAction:
    """Tests for STOP_SESSION action."""

    def test_stop_session_success(self, applier, mock_sessions):
        """Test successful session stop."""
        mock_sessions.exists.return_value = True

        action = StopSessionAction(
            session_type=SessionType.ISSUE,
            number=123,
        )

        result = applier.apply(action)

        assert result.success
        mock_sessions.stop.assert_called_once()

    def test_stop_issue_session_releases_review_exchange_lifecycle(
        self, applier, mock_sessions
    ):
        """Stopping an issue must also cancel hidden review-exchange work."""
        mock_sessions.exists.return_value = True
        pair_registry = Mock()
        job_supervisor = Mock()
        job_supervisor.cancel_matching.return_value = ["review-exchange:123:issue-123"]
        applier.pair_registry = pair_registry
        applier.background_job_supervisor = job_supervisor

        action = StopSessionAction(
            session_type=SessionType.ISSUE,
            number=123,
        )

        result = applier.apply(action)

        assert result.success
        pair_registry.release.assert_called_once_with(123, reason="session-stopped")
        job_supervisor.cancel_matching.assert_called_once()
        predicate = job_supervisor.cancel_matching.call_args.args[0]
        assert predicate("review-exchange:123:issue-123")
        assert not predicate("review-exchange:124:issue-124")

    def test_stop_session_not_running(self, applier, mock_sessions):
        """Test stopping non-existent session."""
        mock_sessions.exists.return_value = False

        action = StopSessionAction(
            session_type=SessionType.ISSUE,
            number=123,
        )

        result = applier.apply(action)

        assert result.result_type == ActionResultType.SKIPPED


class TestQueueReviewAction:
    """Tests for QUEUE_REVIEW action."""

    def test_queue_review_success(self, applier, mock_events):
        """Test successful review queue."""
        action = QueueReviewAction(
            issue_number=123,
            pr_number=456,
            pr_url="https://github.com/owner/repo/pull/456",
            branch_name="123-feature",
        )

        result = applier.apply(action)

        assert result.success
        mock_events.publish.assert_called()


class TestAddCommentAction:
    """Tests for ADD_COMMENT action."""

    def test_pr_comment_event_keeps_full_excerpt(self, applier, mock_repository_host, mock_events):
        """Review comment event should not hard-truncate excerpt content."""
        mock_repository_host.add_comment.return_value = "https://github.com/owner/repo/pull/1#issuecomment-1"
        long_comment = "A" * 300
        action = AddCommentAction(number=1, comment=long_comment, is_pr=True)

        result = applier.apply(action)

        assert result.success
        assert mock_events.publish.call_count >= 1
        review_events = [
            call.args[0]
            for call in mock_events.publish.call_args_list
            if getattr(call.args[0], "name", None) == str(EventName.REVIEW_COMMENT_ADDED)
        ]
        assert review_events
        payload = review_events[-1].data
        assert payload.get("comment_excerpt") == long_comment


class TestSupersedePullRequestAction:
    """Tests for SUPERSEDE_PR action."""

    def test_supersede_pr_comments_then_closes_pr(self, applier, mock_repository_host):
        """Superseding a PR is an ActionApplier-owned GitHub mutation."""
        mock_repository_host.add_comment.return_value = "https://github.com/owner/repo/pull/376#issuecomment-1"
        action = SupersedePullRequestAction(
            issue_number=559,
            pr_number=376,
            comment="Superseded by reset and retry from scratch.",
        )

        result = applier.apply(action)

        assert result.success
        assert result.details["pr_number"] == 376
        mock_repository_host.add_comment.assert_called_once_with(376, action.comment)
        mock_repository_host.close_pr.assert_called_once_with(376)

    def test_supersede_pr_fails_when_close_fails(self, applier, mock_repository_host):
        """Scratch reset callers must see PR supersession failures."""
        mock_repository_host.close_pr.side_effect = RuntimeError("GitHub refused")
        action = SupersedePullRequestAction(
            issue_number=559,
            pr_number=376,
            comment="Superseded by reset and retry from scratch.",
        )

        result = applier.apply(action)

        assert not result.success
        assert "GitHub refused" in (result.error or "")


class TestCloseIssueAction:
    """Tests for CLOSE_ISSUE action."""

    def test_close_issue_updates_repository_state(self, applier, mock_repository_host):
        """Closing an issue is an ActionApplier-owned GitHub mutation."""
        action = CloseIssueAction(issue_number=559)

        result = applier.apply(action)

        assert result.success
        assert result.details["issue_number"] == 559
        assert result.details["state"] == "closed"
        mock_repository_host.update_issue_state.assert_called_once_with(559, "closed")

    def test_close_issue_fails_when_repository_update_fails(
        self,
        applier,
        mock_repository_host,
    ):
        """Close failures are returned to callers."""
        mock_repository_host.update_issue_state.side_effect = RuntimeError("GitHub refused")
        action = CloseIssueAction(issue_number=559)

        result = applier.apply(action)

        assert not result.success
        assert "GitHub refused" in (result.error or "")


class TestEscalateToHumanAction:
    """Tests for ESCALATE_TO_HUMAN action."""

    def test_escalate_success(self, applier, mock_labels, mock_events):
        """Test successful escalation adds label to PR number."""
        action = EscalateToHumanAction(
            issue_number=123,
            pr_number=456,
            escalation_reason="Max rework cycles exceeded",
            rework_cycles=3,
            needs_human_label="needs-human",
            needs_rework_label="needs-rework",
            max_rework_cycles=2,
        )

        result = applier.apply(action)

        assert result.success
        # Should add needs-human to PR number (456), not issue number
        mock_labels.add_label.assert_called_with(456, "needs-human")
        # Should try to remove needs-rework
        mock_labels.remove_label.assert_called_with(456, "needs-rework")

    def test_escalate_posts_comment(self, applier, mock_labels, mock_events, mock_repository_host):
        """Test escalation posts explanatory comment."""
        action = EscalateToHumanAction(
            issue_number=123,
            pr_number=456,
            escalation_reason="Max rework cycles exceeded",
            rework_cycles=3,
            needs_human_label="needs-human",
            needs_rework_label="needs-rework",
            max_rework_cycles=2,
        )

        result = applier.apply(action)

        assert result.success
        mock_repository_host.add_comment.assert_called_once()
        call_args = mock_repository_host.add_comment.call_args
        assert call_args[0][0] == 456  # PR number
        assert "Escalated to Human Review" in call_args[0][1]

    def test_escalate_failure(self, applier, mock_labels):
        """Test escalation failure when add_label fails."""
        mock_labels.add_label.side_effect = Exception("API error")
        action = EscalateToHumanAction(
            issue_number=123,
            pr_number=456,
            escalation_reason="Max rework cycles exceeded",
            rework_cycles=3,
        )

        result = applier.apply(action)

        assert not result.success

    def test_escalate_releases_pair_with_correct_reason(
        self, applier, mock_labels,
    ):
        """ADR 0026 / B2: escalation kills the pair, full stop.

        ``_apply_escalate_to_human`` must call
        ``pair_registry.release(issue_number, reason="escalated-to-human")``
        when a registry is wired. Without this, an escalated issue's
        agent processes leak until orchestrator shutdown — defeating
        the lifecycle contract that escalation is a terminal boundary.
        """
        from unittest.mock import MagicMock

        applier.pair_registry = MagicMock(name="pair_registry")
        action = EscalateToHumanAction(
            issue_number=123,
            pr_number=456,
            escalation_reason="Max rework cycles exceeded",
            rework_cycles=3,
            needs_human_label="needs-human",
            needs_rework_label="needs-rework",
            max_rework_cycles=2,
        )

        result = applier.apply(action)

        assert result.success
        applier.pair_registry.release.assert_called_once_with(
            123, reason="escalated-to-human",
        )

    def test_escalate_releases_pair_before_label_mutations(
        self, applier, mock_labels,
    ):
        """The release must run BEFORE label mutations.

        If a label mutation fails partway, the contract still holds
        ("escalation kills the pair, full stop"). This test pins
        ordering: ``release`` is called before ``labels.add_label``.
        """
        from unittest.mock import MagicMock, call

        # Use one parent mock so we can compare call ordering across
        # the two collaborators.
        parent = MagicMock()
        applier.pair_registry = parent.pair_registry
        applier.labels = parent.labels  # type: ignore[assignment]

        action = EscalateToHumanAction(
            issue_number=123,
            pr_number=456,
            escalation_reason="Max rework cycles exceeded",
            rework_cycles=3,
            needs_human_label="needs-human",
            needs_rework_label="needs-rework",
            max_rework_cycles=2,
        )

        applier.apply(action)

        ordered_calls = parent.method_calls
        release_index = next(
            (i for i, c in enumerate(ordered_calls)
             if c == call.pair_registry.release(123, reason="escalated-to-human")),
            None,
        )
        first_label_index = next(
            (i for i, c in enumerate(ordered_calls)
             if c[0].startswith("labels.add_label")),
            None,
        )
        assert release_index is not None, (
            "pair_registry.release was never called on escalation"
        )
        assert first_label_index is not None, (
            "labels.add_label was never called on escalation"
        )
        assert release_index < first_label_index, (
            "pair_registry.release must run BEFORE label mutations on "
            "escalation; otherwise a partial-failure escalation can "
            "leave the agent processes alive after the lifecycle has "
            f"moved on. Call order was: {ordered_calls}"
        )

    def test_escalate_releases_pair_even_when_label_add_fails(
        self, applier, mock_labels,
    ):
        """Release-before-label means release fires on the failure
        path too: ``add_label`` raising must not prevent the pair
        from being terminated."""
        from unittest.mock import MagicMock

        applier.pair_registry = MagicMock(name="pair_registry")
        mock_labels.add_label.side_effect = Exception("API error")
        action = EscalateToHumanAction(
            issue_number=123,
            pr_number=456,
            escalation_reason="Max rework cycles exceeded",
            rework_cycles=3,
            needs_human_label="needs-human",
            needs_rework_label="needs-rework",
            max_rework_cycles=2,
        )

        result = applier.apply(action)

        # Action itself fails because label mutation failed — but the
        # release call must still have happened first.
        assert not result.success
        applier.pair_registry.release.assert_called_once_with(
            123, reason="escalated-to-human",
        )

    def test_escalate_skips_release_when_no_pair_registry(
        self, applier, mock_labels,
    ):
        """When ``pair_registry`` is None (not wired), escalation
        proceeds without raising. Sanity guard for environments that
        run without the persistent-pair feature configured."""
        # The default applier fixture leaves pair_registry unset.
        # Confirm it's not present so the test below is meaningful.
        assert getattr(applier, "pair_registry", None) is None

        action = EscalateToHumanAction(
            issue_number=123,
            pr_number=456,
            escalation_reason="Max rework cycles exceeded",
            rework_cycles=3,
            needs_human_label="needs-human",
            needs_rework_label="needs-rework",
            max_rework_cycles=2,
        )

        result = applier.apply(action)
        assert result.success


class TestCreateTriageIssueAction:
    """Tests for CREATE_TRIAGE_ISSUE action."""

    def test_create_triage_issue_success(self, applier, mock_repository_host, mock_events):
        """Test successful triage issue creation."""
        mock_repository_host.create_issue.return_value = {"number": 100, "html_url": "https://github.com/owner/repo/issues/100"}

        action = CreateTriageIssueAction(
            title="Batch Review: 5 PRs",
            body="Review these PRs...",
            labels=("agent:triage",),
            pr_count=5,
        )

        result = applier.apply(action)

        assert result.success
        assert result.details["issue_number"] == 100
        mock_repository_host.create_issue.assert_called_once()

    def test_create_triage_issue_no_repo_host(self, applier):
        """Test triage issue creation without repository host."""
        applier.repository_host = None

        action = CreateTriageIssueAction(
            title="Batch Review: 5 PRs",
            body="Review these PRs...",
            labels=("agent:triage",),
            pr_count=5,
        )

        result = applier.apply(action)

        assert not result.success
        assert "No repository_host" in result.error


class TestCleanupSessionAction:
    """Tests for CLEANUP_SESSION action."""

    def test_cleanup_full(self, applier, mock_sessions, mock_worktree_manager, tmp_path):
        """Test full cleanup - close tab and remove worktree."""
        mock_sessions.exists.return_value = True

        action = CleanupSessionAction(
            issue_number=123,
            pr_number=456,
            terminal_id="issue-123",
            worktree_path=str(tmp_path),
            close_tabs=True,
            remove_worktrees=True,
        )

        result = applier.apply(action)

        assert result.success
        mock_sessions.stop.assert_called_once()
        mock_worktree_manager.remove.assert_called_once()

    def test_cleanup_tabs_only(self, applier, mock_sessions, mock_worktree_manager, tmp_path):
        """Test cleanup with only tab closing."""
        mock_sessions.exists.return_value = True

        action = CleanupSessionAction(
            issue_number=123,
            pr_number=456,
            terminal_id="issue-123",
            worktree_path=str(tmp_path),
            close_tabs=True,
            remove_worktrees=False,
        )

        result = applier.apply(action)

        assert result.success
        mock_sessions.stop.assert_called_once()
        mock_worktree_manager.remove.assert_not_called()


class TestRemoveWorktreeAction:
    """Tests for REMOVE_WORKTREE action."""

    def test_remove_worktree_success(self, applier, mock_worktree_manager, tmp_path):
        """Test successful worktree removal."""
        action = RemoveWorktreeAction(worktree_path=str(tmp_path))

        result = applier.apply(action)

        assert result.success
        mock_worktree_manager.remove.assert_called_once()

    def test_remove_worktree_no_manager(self, applier, tmp_path):
        """Test worktree removal without manager."""
        applier.worktree_manager = None
        action = RemoveWorktreeAction(worktree_path=str(tmp_path))

        result = applier.apply(action)

        assert not result.success
        assert "No worktree_manager" in result.error


class TestApplyAll:
    """Tests for apply_all method."""

    def test_apply_all_success(self, applier, mock_labels):
        """Test applying multiple actions."""
        actions = [
            AddLabelAction(issue_number=1, label="a"),
            AddLabelAction(issue_number=2, label="b"),
        ]

        results = applier.apply_all(actions)

        assert len(results) == 2
        assert all(r.success for r in results)

    def test_apply_all_partial_failure(self, applier, mock_labels):
        """Test applying multiple actions with partial failure."""
        mock_labels.add_label.side_effect = [None, Exception("fail"), None]

        actions = [
            AddLabelAction(issue_number=1, label="a"),
            AddLabelAction(issue_number=2, label="b"),
            AddLabelAction(issue_number=3, label="c"),
        ]

        results = applier.apply_all(actions)

        assert len(results) == 3
        assert results[0].success
        assert not results[1].success
        assert results[2].success

    def test_apply_all_emits_label_mutation_summary(self, applier, mock_labels, caplog):
        """Emit summary event/log with attempted/applied/noop/failed mutation counters."""
        # add #1 => no-op, remove #2 => applied, add #3 => failure, remove #4 => applied
        mock_labels.has_label.side_effect = [True, False, False, True]
        mock_labels.add_label.side_effect = Exception("boom")
        actions = [
            AddLabelAction(issue_number=1, label="already-present"),
            RemoveLabelAction(issue_number=1, label="already-absent"),
            AddLabelAction(issue_number=2, label="fails"),
            RemoveLabelAction(issue_number=2, label="removed"),
        ]

        with caplog.at_level(logging.INFO):
            applier.apply_all(actions)

        summary_events = [
            call.args[0]
            for call in applier.events.publish.call_args_list
            if getattr(call.args[0], "name", None) == str(EventName.LABEL_MUTATION_SUMMARY)
        ]
        assert len(summary_events) == 1
        payload = summary_events[0].data
        assert payload["label_add_attempted"] == 2
        assert payload["label_remove_attempted"] == 2
        assert payload["label_mutation_attempted"] == 4
        assert payload["label_mutation_applied"] == 2
        assert payload["label_mutation_noop"] == 1
        assert payload["label_mutation_failed"] == 1
        assert payload["noop_ratio"] == 0.25
        assert payload["failure_ratio"] == 0.25
        assert len(payload["per_issue"]) == 2

        assert any(
            "label_mutations attempted=4 applied=2 noop=1 failed=1" in message
            for message in caplog.messages
        )

    def test_apply_all_skips_label_mutation_summary_when_no_label_actions(self, applier):
        """Avoid summary event noise when batch does no label mutations."""
        actions = [StopSessionAction(session_type=SessionType.ISSUE, number=99)]

        applier.apply_all(actions)

        summary_events = [
            call.args[0]
            for call in applier.events.publish.call_args_list
            if getattr(call.args[0], "name", None) == str(EventName.LABEL_MUTATION_SUMMARY)
        ]
        assert summary_events == []


class TestReconciliation:
    """Tests for reconciliation behavior."""

    def test_reconciliation_disabled(self, applier, mock_labels):
        """Test that reconciliation is skipped when disabled."""
        applier.reconcile = False

        action = SyncLabelsAction(
            issue_number=123,
            add_labels=("in-progress",),
            remove_labels=("ready",),
        )

        result = applier.apply(action)

        assert result.success
        # Should not call fresh_issue_reader for reconciliation
        applier.fresh_issue_reader.read_issue_labels.assert_not_called()

    def test_reconciliation_enabled(self, applier, mock_labels):
        """Test that reconciliation checks labels when enabled."""
        applier.reconcile = True
        applier.fresh_issue_reader.read_issue_labels.return_value = ["ready", "other"]

        action = SyncLabelsAction(
            issue_number=123,
            add_labels=("in-progress",),
            remove_labels=("ready",),
        )

        result = applier.apply(action)

        assert result.success
        applier.fresh_issue_reader.read_issue_labels.assert_called_once_with(123)


class TestExpectedStateEnforcement:
    """Tests for ExpectedState enforcement via _require_expected."""

    @pytest.fixture
    def applier_with_reconcile(
        self,
        mock_labels,
        mock_sessions,
        mock_events,
        mock_repository_host,
        mock_worktree_manager,
        mock_fresh_issue_reader,
    ):
        """Create an ActionApplier with reconciliation enabled."""
        return ActionApplier(
            labels=mock_labels,
            sessions=mock_sessions,
            events=mock_events,
            repository_host=mock_repository_host,
            worktree_manager=mock_worktree_manager,
            fresh_issue_reader=mock_fresh_issue_reader,
            reconcile=True,
        )

    def test_add_label_with_expected_passes_when_satisfied(
        self,
        applier_with_reconcile,
        mock_labels,
        mock_fresh_issue_reader,
    ):
        """Test AddLabelAction proceeds when ExpectedState is satisfied."""
        from issue_orchestrator.control.reconciliation import ExpectedState

        # Current labels satisfy the expected state
        mock_fresh_issue_reader.read_issue_labels.return_value = ["agent:web", "in-progress"]

        action = AddLabelAction(
            issue_number=123,
            label="pr-pending",
            reason="Session completed",
            expected=ExpectedState.with_labels(
                required={"in-progress"},
                forbidden={"io:needs-reconcile"},
            ),
        )

        result = applier_with_reconcile.apply(action)

        assert result.success
        mock_labels.add_label.assert_called_once_with(123, "pr-pending")

    def test_add_label_with_expected_raises_when_missing_required(
        self,
        applier_with_reconcile,
        mock_labels,
        mock_fresh_issue_reader,
    ):
        """Test AddLabelAction raises ReconciliationRequired when required label missing."""
        from issue_orchestrator.control.reconciliation import ExpectedState, ReconciliationRequired

        # Current labels don't have required "in-progress"
        mock_fresh_issue_reader.read_issue_labels.return_value = ["agent:web"]

        action = AddLabelAction(
            issue_number=42,
            label="pr-pending",
            reason="Session completed",
            expected=ExpectedState.with_labels(
                required={"in-progress"},
            ),
        )

        with pytest.raises(ReconciliationRequired) as exc_info:
            applier_with_reconcile.apply(action)

        assert exc_info.value.entity_id == 42
        assert "in-progress" in exc_info.value.reason
        mock_labels.add_label.assert_not_called()

    def test_add_label_with_expected_raises_when_has_forbidden(
        self,
        applier_with_reconcile,
        mock_labels,
        mock_fresh_issue_reader,
    ):
        """Test AddLabelAction raises ReconciliationRequired when forbidden label present."""
        from issue_orchestrator.control.reconciliation import ExpectedState, ReconciliationRequired

        # Current labels have forbidden "io:needs-reconcile"
        mock_fresh_issue_reader.read_issue_labels.return_value = ["in-progress", "io:needs-reconcile"]

        action = AddLabelAction(
            issue_number=99,
            label="pr-pending",
            reason="Session completed",
            expected=ExpectedState.with_labels(
                forbidden={"io:needs-reconcile"},
            ),
        )

        with pytest.raises(ReconciliationRequired) as exc_info:
            applier_with_reconcile.apply(action)

        assert exc_info.value.entity_id == 99
        assert "forbidden" in exc_info.value.reason.lower()
        mock_labels.add_label.assert_not_called()

    def test_add_label_without_expected_proceeds_normally(
        self,
        applier_with_reconcile,
        mock_labels,
        mock_fresh_issue_reader,
    ):
        """Test AddLabelAction without ExpectedState doesn't check constraints."""
        # Even if labels would fail constraints, no ExpectedState means no check
        mock_fresh_issue_reader.read_issue_labels.return_value = ["blocked"]

        action = AddLabelAction(
            issue_number=123,
            label="test-label",
            reason="Test",
            expected=None,  # No ExpectedState
        )

        result = applier_with_reconcile.apply(action)

        assert result.success
        mock_labels.add_label.assert_called_once()

    def test_expected_not_enforced_when_reconcile_disabled(
        self,
        mock_labels,
        mock_sessions,
        mock_events,
        mock_repository_host,
        mock_worktree_manager,
        mock_fresh_issue_reader,
    ):
        """Test ExpectedState is not enforced when reconcile=False."""
        from issue_orchestrator.control.reconciliation import ExpectedState

        applier = ActionApplier(
            labels=mock_labels,
            sessions=mock_sessions,
            events=mock_events,
            repository_host=mock_repository_host,
            worktree_manager=mock_worktree_manager,
            fresh_issue_reader=mock_fresh_issue_reader,
            reconcile=False,  # Disabled
        )

        # ExpectedState would fail if checked, but reconcile=False
        action = AddLabelAction(
            issue_number=123,
            label="test-label",
            reason="Test",
            expected=ExpectedState.with_labels(required={"nonexistent-label"}),
        )

        result = applier.apply(action)

        assert result.success
        mock_labels.add_label.assert_called_once()

    def test_expected_raises_when_labels_cannot_be_fetched(
        self,
        applier_with_reconcile,
        mock_labels,
        mock_fresh_issue_reader,
    ):
        """Test raises ReconciliationRequired when labels cannot be fetched."""
        from issue_orchestrator.control.reconciliation import ExpectedState, ReconciliationRequired

        # Simulate API failure
        mock_fresh_issue_reader.read_issue_labels.return_value = None

        action = AddLabelAction(
            issue_number=123,
            label="test-label",
            reason="Test",
            expected=ExpectedState.with_labels(required={"in-progress"}),
        )

        with pytest.raises(ReconciliationRequired) as exc_info:
            applier_with_reconcile.apply(action)

        assert exc_info.value.entity_id == 123
        assert "fetch" in exc_info.value.reason.lower()
        mock_labels.add_label.assert_not_called()


class TestClaimGateAudit:
    """Structural test: all GitHub-write action types must verify claim ownership.

    This test ensures that no new action type that writes to GitHub can be added
    without also adding ClaimGate verification. If this test fails, it means a
    new action type was added that mutates GitHub state without checking whether
    the orchestrator still owns the claim.
    """

    # Action types that write to GitHub on a CLAIMED issue and must verify ownership
    GITHUB_WRITE_ACTIONS = {
        ActionType.ADD_LABEL,
        ActionType.REMOVE_LABEL,
        ActionType.SYNC_LABELS,
        ActionType.ADD_COMMENT,
        ActionType.SUPERSEDE_PR,
        ActionType.CLOSE_ISSUE,
        ActionType.ESCALATE_TO_HUMAN,
        ActionType.QUEUE_REVIEW,
    }

    # Action types that legitimately skip claim verification:
    # - LAUNCH_SESSION: does its own claim acquisition in session_launcher
    # - LAUNCH_VALIDATION_RETRY: does its own claim acquisition in session_launcher
    # - STOP_SESSION: local terminal operation (killing sessions)
    # - CREATE_WORKTREE / REMOVE_WORKTREE: local filesystem only
    # - QUEUE_REWORK / QUEUE_TRIAGE: local state operations
    # - CREATE_TRIAGE_ISSUE: creates a NEW issue, not modifying a claimed one
    # - CLEANUP_SESSION: post-completion cleanup
    # - RECONCILE_HISTORY_ENTRY: local session history mutation + event only
    # - CREATE_PR: not implemented in action_applier
    EXEMPT_ACTIONS = {
        ActionType.LAUNCH_SESSION,
        ActionType.LAUNCH_VALIDATION_RETRY,
        ActionType.STOP_SESSION,
        ActionType.CREATE_WORKTREE,
        ActionType.REMOVE_WORKTREE,
        ActionType.QUEUE_REWORK,
        ActionType.QUEUE_TRIAGE,
        ActionType.CREATE_TRIAGE_ISSUE,
        ActionType.CLEANUP_SESSION,
        ActionType.RECONCILE_HISTORY_ENTRY,
        ActionType.CREATE_PR,
    }

    def test_all_action_types_accounted_for(self):
        """Every ActionType must be in either GITHUB_WRITE_ACTIONS or EXEMPT_ACTIONS."""
        all_types = set(ActionType)
        accounted = self.GITHUB_WRITE_ACTIONS | self.EXEMPT_ACTIONS
        unaccounted = all_types - accounted

        assert not unaccounted, (
            f"New ActionType(s) {unaccounted} not classified for ClaimGate audit. "
            f"If these write to GitHub on a claimed issue, add to GITHUB_WRITE_ACTIONS. "
            f"If not, add to EXEMPT_ACTIONS with a comment explaining why."
        )

    def test_github_write_actions_call_verify(self):
        """Verify that action handler source code calls _verify_claim_before_write."""
        import inspect
        from issue_orchestrator.control.action_applier import ActionApplier

        # Map action types to handler method names
        handler_map = {
            ActionType.ADD_LABEL: "_apply_add_label",
            ActionType.REMOVE_LABEL: "_apply_remove_label",
            ActionType.SYNC_LABELS: "_apply_sync_labels",
            ActionType.ADD_COMMENT: "_apply_add_comment",
            ActionType.SUPERSEDE_PR: "_apply_supersede_pr",
            ActionType.CLOSE_ISSUE: "_apply_close_issue",
            ActionType.ESCALATE_TO_HUMAN: "_apply_escalate",
            ActionType.QUEUE_REVIEW: "_apply_queue_review",
        }

        for action_type in self.GITHUB_WRITE_ACTIONS:
            handler_name = handler_map.get(action_type)
            assert handler_name, f"No handler mapping for {action_type}"

            # noqa: SLF001 - Inspecting handler source to verify ClaimGate wiring
            handler = getattr(ActionApplier, handler_name, None)
            assert handler, f"Handler {handler_name} not found on ActionApplier"

            source = inspect.getsource(handler)
            assert "_verify_claim_before_write" in source, (
                f"Handler {handler_name} for {action_type} does not call "
                f"_verify_claim_before_write — all GitHub writes on claimed "
                f"issues must verify claim ownership first"
            )
