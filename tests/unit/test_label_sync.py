"""Unit tests for the LabelSync module."""

import pytest
from unittest.mock import MagicMock

from issue_orchestrator.control.label_sync import (
    LabelSync,
    LabelSyncResult,
)
from issue_orchestrator.control.label_projection import DesiredLabels
from issue_orchestrator.ports import NullEventSink, TraceEvent


class CollectingEventSink:
    """Event sink that collects events for test assertions."""

    def __init__(self):
        self.events: list[TraceEvent] = []

    def publish(self, event: TraceEvent) -> None:
        self.events.append(event)


class MockLabelSet:
    """Mock LabelSet for testing."""

    def __init__(self):
        self.labels: dict[int, set[str]] = {}
        self.add_calls: list[tuple[int, str]] = []
        self.remove_calls: list[tuple[int, str]] = []
        self.fail_on: set[str] = set()  # Labels that should fail

    def add_label(self, issue_number: int, label: str) -> None:
        self.add_calls.append((issue_number, label))
        if label in self.fail_on:
            raise Exception(f"Failed to add {label}")
        if issue_number not in self.labels:
            self.labels[issue_number] = set()
        self.labels[issue_number].add(label)

    def remove_label(self, issue_number: int, label: str) -> None:
        self.remove_calls.append((issue_number, label))
        if label in self.fail_on:
            raise Exception(f"Failed to remove {label}")
        if issue_number in self.labels:
            self.labels[issue_number].discard(label)

    def has_label(self, issue_number: int, label: str) -> bool:
        return issue_number in self.labels and label in self.labels[issue_number]


class TestLabelSyncResult:
    """Test the LabelSyncResult dataclass."""

    def test_success_true_when_no_errors(self):
        """Test success is True when no errors."""
        result = LabelSyncResult(
            issue_number=123,
            added=frozenset({"in-progress"}),
            removed=frozenset(),
            errors={},
        )
        assert result.success is True

    def test_success_false_when_errors(self):
        """Test success is False when there are errors."""
        result = LabelSyncResult(
            issue_number=123,
            added=frozenset(),
            removed=frozenset(),
            errors={"label": "failed"},
        )
        assert result.success is False

    def test_changed_true_when_labels_added(self):
        """Test changed is True when labels were added."""
        result = LabelSyncResult(
            issue_number=123,
            added=frozenset({"in-progress"}),
            removed=frozenset(),
            errors={},
        )
        assert result.changed is True

    def test_changed_true_when_labels_removed(self):
        """Test changed is True when labels were removed."""
        result = LabelSyncResult(
            issue_number=123,
            added=frozenset(),
            removed=frozenset({"blocked"}),
            errors={},
        )
        assert result.changed is True

    def test_changed_false_when_no_changes(self):
        """Test changed is False when nothing changed."""
        result = LabelSyncResult(
            issue_number=123,
            added=frozenset(),
            removed=frozenset(),
            errors={},
        )
        assert result.changed is False


class TestLabelSync:
    """Test the LabelSync class."""

    @pytest.fixture
    def mock_labels(self):
        return MockLabelSet()

    @pytest.fixture
    def collecting_sink(self):
        return CollectingEventSink()

    @pytest.fixture
    def sync(self, mock_labels, collecting_sink):
        return LabelSync(labels=mock_labels, events=collecting_sink)

    def test_sync_adds_missing_labels(self, sync, mock_labels):
        """Test that sync adds missing labels."""
        result = sync.sync(
            issue_number=123,
            current=set(),
            desired=DesiredLabels.add("in-progress"),
        )

        assert result.added == frozenset({"in-progress"})
        assert result.removed == frozenset()
        assert result.success
        assert (123, "in-progress") in mock_labels.add_calls

    def test_sync_removes_existing_labels(self, sync, mock_labels):
        """Test that sync removes existing labels."""
        mock_labels.labels[123] = {"blocked", "in-progress"}

        result = sync.sync(
            issue_number=123,
            current={"blocked", "in-progress"},
            desired=DesiredLabels.remove("blocked"),
        )

        assert result.removed == frozenset({"blocked"})
        assert (123, "blocked") in mock_labels.remove_calls

    def test_sync_does_not_add_existing_labels(self, sync, mock_labels):
        """Test that sync doesn't re-add existing labels."""
        result = sync.sync(
            issue_number=123,
            current={"in-progress"},
            desired=DesiredLabels.add("in-progress"),
        )

        assert result.added == frozenset()
        assert len(mock_labels.add_calls) == 0

    def test_sync_does_not_remove_missing_labels(self, sync, mock_labels):
        """Test that sync doesn't try to remove missing labels."""
        result = sync.sync(
            issue_number=123,
            current=set(),
            desired=DesiredLabels.remove("blocked"),
        )

        assert result.removed == frozenset()
        assert len(mock_labels.remove_calls) == 0

    def test_sync_handles_add_errors(self, sync, mock_labels):
        """Test that sync handles add errors gracefully."""
        mock_labels.fail_on.add("will-fail")

        result = sync.sync(
            issue_number=123,
            current=set(),
            desired=DesiredLabels.add("will-fail", "will-succeed"),
        )

        assert "will-succeed" in result.added
        assert "will-fail" not in result.added
        assert "will-fail" in result.errors
        assert not result.success

    def test_sync_handles_remove_errors(self, sync, mock_labels):
        """Test that sync handles remove errors gracefully."""
        mock_labels.fail_on.add("will-fail")

        result = sync.sync(
            issue_number=123,
            current={"will-fail", "will-succeed"},
            desired=DesiredLabels.remove("will-fail", "will-succeed"),
        )

        assert "will-succeed" in result.removed
        assert "will-fail" not in result.removed
        assert "will-fail" in result.errors

    def test_sync_emits_event_on_change(self, sync, collecting_sink):
        """Test that sync emits event when labels change."""
        sync.sync(
            issue_number=123,
            current=set(),
            desired=DesiredLabels.add("in-progress"),
        )

        assert len(collecting_sink.events) == 1
        event = collecting_sink.events[0]
        assert event.name == "labels.synced"
        assert event.data["issue_number"] == 123
        assert "in-progress" in event.data["added"]

    def test_sync_no_event_when_no_change(self, sync, collecting_sink):
        """Test that sync doesn't emit event when nothing changes."""
        sync.sync(
            issue_number=123,
            current={"in-progress"},
            desired=DesiredLabels.add("in-progress"),
        )

        assert len(collecting_sink.events) == 0

    def test_sync_add_convenience(self, sync, mock_labels):
        """Test sync_add convenience method."""
        result = sync.sync_add(123, "in-progress", "bug")

        assert result.added == frozenset({"in-progress", "bug"})

    def test_sync_remove_convenience(self, sync, mock_labels):
        """Test sync_remove convenience method."""
        result = sync.sync_remove(123, "blocked")

        assert result.removed == frozenset({"blocked"})

    def test_remove_blocked_labels(self, sync, mock_labels):
        """Test remove_blocked_labels removes all blocked-* labels."""
        mock_labels.labels[123] = {"blocked-tests", "blocked-needs-human", "in-progress"}

        result = sync.remove_blocked_labels(
            issue_number=123,
            current={"blocked-tests", "blocked-needs-human", "in-progress"},
        )

        assert "blocked-tests" in result.removed
        assert "blocked-needs-human" in result.removed
        assert "in-progress" not in result.removed

    def test_remove_blocked_labels_noop_when_none(self, sync, mock_labels):
        """Test remove_blocked_labels is no-op when no blocked labels."""
        result = sync.remove_blocked_labels(
            issue_number=123,
            current={"in-progress", "bug"},
        )

        assert not result.changed
        assert result.removed == frozenset()
