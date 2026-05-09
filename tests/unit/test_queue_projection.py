"""Unit tests for queue_projection.py module.

Tests the queue projection logic that computes and caches available issues,
detects changes, and emits events.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from issue_orchestrator.control.queue_projection import QueueChange, QueueProjection
from issue_orchestrator.domain.models import OrchestratorState, Issue, AgentConfig
from issue_orchestrator.events import EventName
from issue_orchestrator.infra.config import Config
from issue_orchestrator.ports.event_sink import InMemoryEventSink


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def sample_config():
    """Create a minimal config for testing."""
    return Config(
        repo="owner/repo",
        repo_root=Path("/tmp/test"),
        worktree_base=Path("/tmp"),
        agents={"agent:backend": AgentConfig(prompt_path=Path("/tmp/prompt.txt"))},
        max_concurrent_sessions=3,
    )


@pytest.fixture
def sample_event_sink():
    """Create an in-memory event sink for testing."""
    return InMemoryEventSink()


@pytest.fixture
def mock_repository_host():
    """Create a mock repository host."""
    return MagicMock()


@pytest.fixture
def queue_projection(sample_config, mock_repository_host, sample_event_sink):
    """Create a QueueProjection instance for testing."""
    return QueueProjection(sample_config, mock_repository_host, sample_event_sink)


# =============================================================================
# Tests for QueueChange dataclass
# =============================================================================


class TestQueueChange:
    """Tests for the QueueChange dataclass."""

    def test_queue_change_creation(self):
        """QueueChange can be created with added, removed, and total."""
        issue1 = Issue(number=1, title="New issue", labels=["agent:backend"], body="")
        issue2 = Issue(number=2, title="Another new", labels=["agent:backend"], body="")

        change = QueueChange(
            added=[issue1, issue2],
            removed=[3, 4],
            total=10,
        )

        assert len(change.added) == 2
        assert change.added[0].number == 1
        assert change.added[1].number == 2
        assert change.removed == [3, 4]
        assert change.total == 10

    def test_queue_change_with_empty_added(self):
        """QueueChange with empty added list."""
        change = QueueChange(added=[], removed=[5], total=9)

        assert change.added == []
        assert change.removed == [5]
        assert change.total == 9

    def test_queue_change_with_empty_removed(self):
        """QueueChange with empty removed list."""
        issue = Issue(number=1, title="New", labels=["agent:backend"], body="")
        change = QueueChange(added=[issue], removed=[], total=1)

        assert len(change.added) == 1
        assert change.removed == []
        assert change.total == 1

    def test_queue_change_with_empty_lists(self):
        """QueueChange with both empty lists (no changes)."""
        change = QueueChange(added=[], removed=[], total=5)

        assert change.added == []
        assert change.removed == []
        assert change.total == 5


# =============================================================================
# Tests for QueueProjection.compute_queue()
# =============================================================================


class TestComputeQueue:
    """Tests for computing the in-scope issue snapshot from repository state."""

    def test_compute_queue_calls_fetch_all_issues(self, queue_projection, mock_repository_host):
        """compute_queue delegates to fetch_all_issues."""
        expected_issues = [
            Issue(number=1, title="Issue 1", labels=["agent:backend"], body=""),
            Issue(number=2, title="Issue 2", labels=["agent:backend"], body=""),
        ]

        with patch(
            "issue_orchestrator.infra.audit.fetch_all_issues",
            return_value=expected_issues,
        ) as mock_fetch_all:
            state = OrchestratorState()
            result = queue_projection.compute_queue(state)

            assert result == expected_issues
            mock_fetch_all.assert_called_once()
            # Verify it was called with the right arguments
            call_args = mock_fetch_all.call_args
            # noqa: SLF001 - Verifying correct config was passed to auditor
            assert call_args[0][0] == queue_projection._config  # noqa: SLF001
            assert call_args[0][1] == mock_repository_host

    def test_compute_queue_returns_empty_list(self, queue_projection):
        """compute_queue returns empty list when no issues available."""
        with patch(
            "issue_orchestrator.infra.audit.fetch_all_issues",
            return_value=[],
        ):
            state = OrchestratorState()
            result = queue_projection.compute_queue(state)

            assert result == []

    def test_compute_queue_returns_multiple_issues(self, queue_projection):
        """compute_queue returns multiple issues in correct order."""
        issues = [
            Issue(number=1, title="Issue 1", labels=["agent:backend"], body=""),
            Issue(number=2, title="Issue 2", labels=["agent:backend"], body=""),
            Issue(number=3, title="Issue 3", labels=["agent:backend"], body=""),
        ]

        with patch(
            "issue_orchestrator.infra.audit.fetch_all_issues",
            return_value=issues,
        ):
            state = OrchestratorState()
            result = queue_projection.compute_queue(state)

            assert len(result) == 3
            assert result[0].number == 1
            assert result[1].number == 2
            assert result[2].number == 3


# =============================================================================
# Tests for QueueProjection.update_and_emit()
# =============================================================================


class TestUpdateAndEmit:
    """Tests for updating queue cache and emitting events."""

    def test_no_change_returns_none(self, queue_projection, sample_event_sink):
        """No change to queue returns None and doesn't emit event."""
        issue = Issue(number=1, title="Issue 1", labels=["agent:backend"], body="")
        state = OrchestratorState()
        state.cached_queue_issues = [issue]

        with patch(
            "issue_orchestrator.infra.audit.fetch_all_issues",
            return_value=[issue],
        ):
            result = queue_projection.update_and_emit(state)

            assert result is None
            assert not sample_event_sink.has_event(str(EventName.QUEUE_CHANGED))

    def test_update_persists_snapshot_when_store_is_configured(
        self,
        sample_config,
        mock_repository_host,
        sample_event_sink,
    ):
        """Queue projection persists the owner-managed snapshot when wired with a store."""
        issue = Issue(number=1, title="Issue 1", labels=["agent:backend"], body="")
        state = OrchestratorState(queue_delta_watermark="2026-05-09T15:00:00Z")
        queue_cache_store = MagicMock()
        projection = QueueProjection(
            sample_config,
            mock_repository_host,
            sample_event_sink,
            queue_cache_store,
        )

        with patch(
            "issue_orchestrator.infra.audit.fetch_all_issues",
            return_value=[issue],
        ):
            projection.update_and_emit(state)

        queue_cache_store.save_snapshot.assert_called_once_with(
            state.cached_scope_issues,
            "2026-05-09T15:00:00Z",
            repo="owner/repo",
        )

    def test_added_issues_emits_event(self, queue_projection, sample_event_sink):
        """Adding new issues to queue emits event with added info."""
        old_issue = Issue(number=1, title="Old", labels=["agent:backend"], body="")
        new_issue = Issue(number=2, title="New", labels=["agent:backend"], body="")
        state = OrchestratorState()
        state.cached_queue_issues = [old_issue]

        with patch(
            "issue_orchestrator.infra.audit.fetch_all_issues",
            return_value=[old_issue, new_issue],
        ):
            result = queue_projection.update_and_emit(state)

            assert result is not None
            assert len(result.added) == 1
            assert result.added[0].number == 2
            assert result.removed == []
            assert result.total == 2

            # Verify event was emitted
            assert sample_event_sink.has_event(str(EventName.QUEUE_CHANGED))
            event = sample_event_sink.last_event(str(EventName.QUEUE_CHANGED))
            assert event is not None
            assert len(event.data["added"]) == 1
            assert event.data["added"][0]["number"] == 2
            assert event.data["removed"] == []
            assert event.data["total"] == 2

    def test_removed_issues_emits_event(self, queue_projection, sample_event_sink):
        """Removing issues from queue emits event with removed info."""
        old_issue = Issue(number=1, title="Old", labels=["agent:backend"], body="")
        state = OrchestratorState()
        state.cached_queue_issues = [old_issue]

        with patch(
            "issue_orchestrator.infra.audit.fetch_all_issues",
            return_value=[],
        ):
            result = queue_projection.update_and_emit(state)

            assert result is not None
            assert result.added == []
            assert result.removed == [1]
            assert result.total == 0

            # Verify event was emitted
            assert sample_event_sink.has_event(str(EventName.QUEUE_CHANGED))
            event = sample_event_sink.last_event(str(EventName.QUEUE_CHANGED))
            assert event is not None
            assert event.data["added"] == []
            assert event.data["removed"] == [{"number": 1, "issue_key": "1"}]
            assert event.data["total"] == 0

    def test_queue_changed_both_added_and_removed(self, queue_projection, sample_event_sink):
        """Queue change with both added and removed issues."""
        issue1 = Issue(number=1, title="Issue 1", labels=["agent:backend"], body="")
        issue2 = Issue(number=2, title="Issue 2", labels=["agent:backend"], body="")
        issue3 = Issue(number=3, title="Issue 3", labels=["agent:backend"], body="")
        state = OrchestratorState()
        state.cached_queue_issues = [issue1, issue2]

        with patch(
            "issue_orchestrator.infra.audit.fetch_all_issues",
            return_value=[issue1, issue3],
        ):
            result = queue_projection.update_and_emit(state)

            assert result is not None
            assert len(result.added) == 1
            assert result.added[0].number == 3
            assert result.removed == [2]
            assert result.total == 2

    def test_state_cached_queue_issues_updated(self, queue_projection):
        """State.cached_queue_issues is updated after compute_queue."""
        issue1 = Issue(number=1, title="Old", labels=["agent:backend"], body="")
        issue2 = Issue(number=2, title="New", labels=["agent:backend"], body="")
        state = OrchestratorState()
        state.cached_queue_issues = [issue1]

        with patch(
            "issue_orchestrator.infra.audit.fetch_all_issues",
            return_value=[issue1, issue2],
        ):
            queue_projection.update_and_emit(state)

            assert len(state.cached_queue_issues) == 2
            assert len(state.cached_scope_issues) == 2
            assert state.cached_queue_issues[0].number == 1
            assert state.cached_queue_issues[1].number == 2

    def test_refresh_removes_issue_that_fell_out_of_scope(self, queue_projection, sample_event_sink):
        blocked_issue = Issue(
            number=4057,
            title="Publish failed",
            labels=["agent:backend", "publish-failed"],
            body="",
        )
        state = OrchestratorState()
        state.cached_scope_issues = [blocked_issue]
        state.cached_queue_issues = [blocked_issue]

        with patch(
            "issue_orchestrator.infra.audit.fetch_all_issues",
            return_value=[],
        ):
            result = queue_projection.update_and_emit(state)

        assert result is not None
        assert result.added == []
        assert result.removed == [4057]
        assert state.cached_scope_issues == []
        assert state.cached_queue_issues == []
        event = sample_event_sink.last_event(str(EventName.QUEUE_CHANGED))
        assert event is not None
        assert event.data["removed"] == [{"number": 4057, "issue_key": "4057"}]

    def test_refresh_uses_cached_queue_issues_when_scope_cache_is_cold(
        self,
        queue_projection,
        sample_event_sink,
    ):
        blocked_issue = Issue(
            number=4057,
            title="Publish failed",
            labels=["agent:backend", "publish-failed"],
            body="",
        )
        state = OrchestratorState()
        state.cached_scope_issues = []
        state.cached_queue_issues = [blocked_issue]

        with patch(
            "issue_orchestrator.infra.audit.fetch_all_issues",
            return_value=[],
        ):
            result = queue_projection.update_and_emit(state)

        assert result is not None
        assert result.removed == [4057]
        assert state.cached_scope_issues == []
        assert state.cached_queue_issues == []
        event = sample_event_sink.last_event(str(EventName.QUEUE_CHANGED))
        assert event is not None
        assert event.data["removed"] == [{"number": 4057, "issue_key": "4057"}]

    def test_failed_this_cycle_cleared_on_queue_refresh(self, queue_projection):
        """failed_this_cycle is cleared when queue changes."""
        issue = Issue(number=1, title="Issue", labels=["agent:backend"], body="")
        state = OrchestratorState()
        state.cached_queue_issues = [issue]
        state.failed_this_cycle = {1, 2, 3}  # Some failed issues

        with patch(
            "issue_orchestrator.infra.audit.fetch_all_issues",
            return_value=[issue],
        ):
            queue_projection.update_and_emit(state)

            # failed_this_cycle should be cleared
            assert len(state.failed_this_cycle) == 0

    def test_failed_this_cycle_cleared_on_any_update(self, queue_projection):
        """failed_this_cycle is cleared whenever update_and_emit is called successfully."""
        issue = Issue(number=1, title="Issue", labels=["agent:backend"], body="")
        state = OrchestratorState()
        state.cached_queue_issues = [issue]
        state.failed_this_cycle = {5, 6, 7}

        with patch(
            "issue_orchestrator.infra.audit.fetch_all_issues",
            return_value=[issue],
        ):
            result = queue_projection.update_and_emit(state)

            # failed_this_cycle is cleared on any successful update, even when queue is stable
            assert len(state.failed_this_cycle) == 0
            # But no change event is emitted
            assert result is None

    def test_exception_handling_logs_warning_returns_none(
        self, queue_projection, sample_event_sink
    ):
        """Exception during queue update returns None and logs warning."""
        state = OrchestratorState()
        state.cached_queue_issues = []

        with patch(
            "issue_orchestrator.infra.audit.fetch_all_issues",
            side_effect=Exception("Test error"),
        ):
            with patch("issue_orchestrator.control.queue_projection.logger") as mock_logger:
                result = queue_projection.update_and_emit(state)

                assert result is None
                mock_logger.warning.assert_called_once()
                assert "Failed to update queue cache" in mock_logger.warning.call_args[0][0]

    def test_exception_does_not_emit_event(self, queue_projection, sample_event_sink):
        """Exception prevents event from being emitted."""
        state = OrchestratorState()
        state.cached_queue_issues = []

        with patch(
            "issue_orchestrator.infra.audit.fetch_all_issues",
            side_effect=Exception("Test error"),
        ):
            queue_projection.update_and_emit(state)

            # No QUEUE_CHANGED event should be emitted on error
            assert not sample_event_sink.has_event(str(EventName.QUEUE_CHANGED))

    def test_event_contains_issue_details(self, queue_projection, sample_event_sink):
        """Emitted event contains issue number and title."""
        new_issue = Issue(
            number=42,
            title="Important issue",
            labels=["agent:backend"],
            body="",
        )
        state = OrchestratorState()
        state.cached_queue_issues = []

        with patch(
            "issue_orchestrator.infra.audit.fetch_all_issues",
            return_value=[new_issue],
        ):
            queue_projection.update_and_emit(state)

            event = sample_event_sink.last_event(str(EventName.QUEUE_CHANGED))
            assert event is not None
            added = event.data["added"]
            assert len(added) == 1
            assert added[0]["number"] == 42
            assert added[0]["title"] == "Important issue"

    def test_state_cache_empty_initially(self, queue_projection, sample_event_sink):
        """Starting with empty cache, adding issues returns correct change."""
        issue1 = Issue(number=1, title="First", labels=["agent:backend"], body="")
        issue2 = Issue(number=2, title="Second", labels=["agent:backend"], body="")
        state = OrchestratorState()
        # Initially empty
        assert state.cached_queue_issues == []

        with patch(
            "issue_orchestrator.infra.audit.fetch_all_issues",
            return_value=[issue1, issue2],
        ):
            result = queue_projection.update_and_emit(state)

            assert result is not None
            assert len(result.added) == 2
            assert result.removed == []
            assert result.total == 2

    def test_multiple_adds_and_removes(self, queue_projection, sample_event_sink):
        """Multiple additions and removals are tracked correctly."""
        issues_old = [
            Issue(number=1, title="I1", labels=["agent:backend"], body=""),
            Issue(number=2, title="I2", labels=["agent:backend"], body=""),
            Issue(number=3, title="I3", labels=["agent:backend"], body=""),
        ]
        issues_new = [
            Issue(number=2, title="I2", labels=["agent:backend"], body=""),
            Issue(number=4, title="I4", labels=["agent:backend"], body=""),
            Issue(number=5, title="I5", labels=["agent:backend"], body=""),
        ]
        state = OrchestratorState()
        state.cached_queue_issues = issues_old

        with patch(
            "issue_orchestrator.infra.audit.fetch_all_issues",
            return_value=issues_new,
        ):
            result = queue_projection.update_and_emit(state)

            assert result is not None
            assert len(result.added) == 2  # 4 and 5 added
            assert set(i.number for i in result.added) == {4, 5}
            assert set(result.removed) == {1, 3}
            assert result.total == 3
