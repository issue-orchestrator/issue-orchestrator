"""Unit tests for ActionApplier."""

import pytest
from unittest.mock import MagicMock, Mock
from pathlib import Path

from issue_orchestrator.control.action_applier import ActionApplier
from issue_orchestrator.control.actions import (
    ActionType,
    ActionResultType,
    AddLabelAction,
    RemoveLabelAction,
    SyncLabelsAction,
    LaunchSessionAction,
    StopSessionAction,
    QueueReviewAction,
    EscalateToHumanAction,
    CreateTriageIssueAction,
    CleanupSessionAction,
    RemoveWorktreeAction,
)
from issue_orchestrator.control.session_manager import SessionType
from issue_orchestrator.domain.models import Issue, Session, AgentConfig


@pytest.fixture
def mock_labels():
    """Create a mock LabelSet."""
    labels = MagicMock()
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
        action = AddLabelAction(issue_number=123, label="in-progress")

        result = applier.apply(action)

        assert result.success
        mock_labels.add_label.assert_called_once_with(123, "in-progress")

    def test_add_label_failure(self, applier, mock_labels):
        """Test label addition failure."""
        mock_labels.add_label.side_effect = Exception("API error")
        action = AddLabelAction(issue_number=123, label="in-progress")

        result = applier.apply(action)

        assert not result.success
        assert "API error" in result.error


class TestRemoveLabelAction:
    """Tests for REMOVE_LABEL action."""

    def test_remove_label_success(self, applier, mock_labels):
        """Test successful label removal."""
        action = RemoveLabelAction(issue_number=123, label="in-progress")

        result = applier.apply(action)

        assert result.success
        mock_labels.remove_label.assert_called_once_with(123, "in-progress")

    def test_remove_label_failure(self, applier, mock_labels):
        """Test label removal failure."""
        mock_labels.remove_label.side_effect = Exception("API error")
        action = RemoveLabelAction(issue_number=123, label="in-progress")

        result = applier.apply(action)

        assert not result.success
        assert "API error" in result.error


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
            session_type="issue",
            number=123,
        )

        result = applier.apply(action)

        assert result.success
        callback.assert_called_once_with("issue", 123)
        assert result.details["session_name"] == "issue-123"
        assert result.details["issue_number"] == 123

    def test_launch_session_callback_fails(self, applier):
        """Test launch session when callback returns None."""
        callback = MagicMock(return_value=None)
        applier.session_launcher = callback

        action = LaunchSessionAction(
            session_type="issue",
            number=123,
        )

        result = applier.apply(action)

        assert not result.success
        assert "Failed to launch" in result.error

    def test_launch_session_no_callback_no_command(self, applier):
        """Test launch session without callback or command fails."""
        action = LaunchSessionAction(
            session_type="issue",
            number=123,
        )

        result = applier.apply(action)

        assert not result.success
        assert "No session_launcher callback" in result.error

    def test_launch_session_fallback_with_command(self, applier, mock_sessions, tmp_path):
        """Test launch session fallback when command provided."""
        action = LaunchSessionAction(
            session_type="issue",
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
            session_type="issue",
            number=123,
            command="claude work",
            working_dir=str(tmp_path),
        )

        result = applier.apply(action)

        assert result.result_type == ActionResultType.SKIPPED
        assert "already running" in result.details.get("skip_reason", "")


class TestStopSessionAction:
    """Tests for STOP_SESSION action."""

    def test_stop_session_success(self, applier, mock_sessions):
        """Test successful session stop."""
        mock_sessions.exists.return_value = True

        action = StopSessionAction(
            session_type="issue",
            number=123,
        )

        result = applier.apply(action)

        assert result.success
        mock_sessions.stop.assert_called_once()

    def test_stop_session_not_running(self, applier, mock_sessions):
        """Test stopping non-existent session."""
        mock_sessions.exists.return_value = False

        action = StopSessionAction(
            session_type="issue",
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
            terminal_session_name="issue-123",
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
            terminal_session_name="issue-123",
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
