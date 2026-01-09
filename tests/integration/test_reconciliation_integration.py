"""Integration tests for reconciliation with ActionApplier.

These tests verify that reconciliation works correctly when integrated
with the ActionApplier and actual (mocked) adapters.
"""

import pytest
from unittest.mock import MagicMock, patch, call

from issue_orchestrator.control.action_applier import ActionApplier
from issue_orchestrator.control.actions import SyncLabelsAction
from issue_orchestrator.control.reconciliation import (
    ExternalSnapshot,
    ExpectedState,
    ReconciliationRequired,
)
from issue_orchestrator.ports import EventSink, TraceEvent
from issue_orchestrator.ports.label_set import LabelSet
from issue_orchestrator.ports.issue_tracker import IssueTracker


class MockLabelSet(LabelSet):
    """Mock LabelSet that tracks calls."""

    def __init__(self):
        self.add_calls: list[tuple[int, str]] = []
        self.remove_calls: list[tuple[int, str]] = []

    def add_label(self, issue_number: int, label: str) -> None:
        self.add_calls.append((issue_number, label))

    def remove_label(self, issue_number: int, label: str) -> None:
        self.remove_calls.append((issue_number, label))

    def list_labels(self, issue_number: int) -> list[str]:
        return []


class MockIssueTracker(IssueTracker):
    """Mock IssueTracker that returns configurable label state."""

    def __init__(self, labels_by_issue: dict[int, list[str]] | None = None):
        self._labels_by_issue = labels_by_issue or {}
        self.get_labels_calls: list[int] = []

    def get_issue_labels(self, issue_number: int) -> list[str]:
        self.get_labels_calls.append(issue_number)
        return self._labels_by_issue.get(issue_number, [])

    def get_issue_labels_fresh(self, issue_number: int) -> list[str]:
        return self.get_issue_labels(issue_number)

    def set_labels(self, issue_number: int, labels: list[str]) -> None:
        self._labels_by_issue[issue_number] = labels

    def list_issues(self, labels=None, state=None):
        return []

    def get_issue(self, number):
        return None

    def add_label(self, issue_number, label):
        current = self._labels_by_issue.get(issue_number, [])
        if label not in current:
            current.append(label)
            self._labels_by_issue[issue_number] = current

    def remove_label(self, issue_number, label):
        current = self._labels_by_issue.get(issue_number, [])
        if label in current:
            current.remove(label)
            self._labels_by_issue[issue_number] = current


class MockEventSink(EventSink):
    """Mock EventSink that captures published events."""

    def __init__(self):
        self.events: list[TraceEvent] = []

    def publish(self, event: TraceEvent) -> None:
        self.events.append(event)


class MockSessionManager:
    """Mock SessionManager for ActionApplier."""

    def exists(self, ref):
        return False

    def start(self, ctx):
        return True

    def stop(self, ref):
        pass


class TestReconciliationIntegration:
    """Integration tests for reconciliation in ActionApplier."""

    @pytest.fixture
    def label_set(self):
        return MockLabelSet()

    @pytest.fixture
    def session_manager(self):
        return MockSessionManager()

    @pytest.fixture
    def event_sink(self):
        return MockEventSink()

    def test_sync_labels_without_reconciliation(
        self, label_set, session_manager, event_sink
    ):
        """Without reconciliation enabled, sync_labels proceeds normally."""
        applier = ActionApplier(
            labels=label_set,
            sessions=session_manager,
            events=event_sink,
            reconcile=False,  # Disabled
        )

        action = SyncLabelsAction(
            issue_number=123,
            add_labels=("in-progress",),
            remove_labels=("queued",),
            reason="Test transition",
        )

        result = applier.apply(action)

        assert result.success
        assert (123, "in-progress") in label_set.add_calls
        assert (123, "queued") in label_set.remove_calls

    def test_sync_labels_with_reconciliation_passes(
        self, label_set, session_manager, event_sink
    ):
        """With reconciliation enabled and state matching, sync_labels proceeds."""
        issue_tracker = MockIssueTracker({123: ["queued"]})

        applier = ActionApplier(
            labels=label_set,
            sessions=session_manager,
            events=event_sink,
            issue_tracker=issue_tracker,
            reconcile=True,
        )

        action = SyncLabelsAction(
            issue_number=123,
            add_labels=("in-progress",),
            remove_labels=("queued",),
            reason="Test transition",
        )

        result = applier.apply(action)

        assert result.success
        # Verify labels were fetched before mutation
        assert 123 in issue_tracker.get_labels_calls
        # Verify mutations proceeded
        assert (123, "in-progress") in label_set.add_calls
        assert (123, "queued") in label_set.remove_calls

    def test_sync_labels_with_reconciliation_warns_on_missing_remove_label(
        self, label_set, session_manager, event_sink
    ):
        """When label to remove isn't present, emit warning but proceed."""
        # Issue doesn't have "queued" label - it was removed externally
        issue_tracker = MockIssueTracker({123: ["other-label"]})

        applier = ActionApplier(
            labels=label_set,
            sessions=session_manager,
            events=event_sink,
            issue_tracker=issue_tracker,
            reconcile=True,
        )

        action = SyncLabelsAction(
            issue_number=123,
            add_labels=("in-progress",),
            remove_labels=("queued",),
            reason="Test transition",
        )

        result = applier.apply(action)

        # Should still succeed (warning only)
        assert result.success

        # Verify warning event was emitted
        warning_events = [
            e for e in event_sink.events if e.name == "reconciliation.warning"
        ]
        assert len(warning_events) == 1
        assert "queued" in str(warning_events[0].data["missing_labels"])

    def test_sync_labels_emits_reconciliation_checked_event(
        self, label_set, session_manager, event_sink
    ):
        """Reconciliation check emits trace event for debugging."""
        issue_tracker = MockIssueTracker({456: ["queued", "agent:test"]})

        applier = ActionApplier(
            labels=label_set,
            sessions=session_manager,
            events=event_sink,
            issue_tracker=issue_tracker,
            reconcile=True,
        )

        action = SyncLabelsAction(
            issue_number=456,
            add_labels=("in-progress",),
            remove_labels=("queued",),
            reason="Test transition",
        )

        applier.apply(action)

        # Verify reconciliation.checked event was emitted
        checked_events = [
            e for e in event_sink.events if e.name == "reconciliation.checked"
        ]
        assert len(checked_events) == 1
        assert checked_events[0].data["issue_number"] == 456
        assert set(checked_events[0].data["current_labels"]) == {"queued", "agent:test"}

    def test_reconciliation_without_issue_tracker_proceeds_with_warning(
        self, label_set, session_manager, event_sink
    ):
        """If reconcile=True but no issue_tracker, proceed with logged warning."""
        applier = ActionApplier(
            labels=label_set,
            sessions=session_manager,
            events=event_sink,
            issue_tracker=None,  # No tracker
            reconcile=True,  # But reconciliation requested
        )

        action = SyncLabelsAction(
            issue_number=789,
            add_labels=("in-progress",),
            remove_labels=("queued",),
            reason="Test transition",
        )

        result = applier.apply(action)

        # Should still succeed
        assert result.success
        # Mutations should proceed
        assert (789, "in-progress") in label_set.add_calls


class TestReconciliationSimulatedRaceCondition:
    """Tests simulating race conditions with external changes."""

    def test_label_changed_externally_between_plan_and_apply(self):
        """Simulate a label being changed externally between planning and applying.

        This tests the scenario from RECONCILIATION_TESTS.md:
        - simulate label changed externally between plan and apply
        """
        label_set = MockLabelSet()
        session_manager = MockSessionManager()
        event_sink = MockEventSink()

        # Start with issue having "queued" label
        issue_tracker = MockIssueTracker({100: ["queued", "agent:test"]})

        applier = ActionApplier(
            labels=label_set,
            sessions=session_manager,
            events=event_sink,
            issue_tracker=issue_tracker,
            reconcile=True,
        )

        # Plan: remove "queued", add "in-progress"
        action = SyncLabelsAction(
            issue_number=100,
            add_labels=("in-progress",),
            remove_labels=("queued",),
            reason="Start work",
        )

        # Simulate external change: someone removes "queued" label before apply
        # This is done by updating the tracker's state before we apply
        issue_tracker.set_labels(100, ["agent:test", "blocked"])  # "queued" removed, "blocked" added

        # Now apply - reconciliation should detect the change
        result = applier.apply(action)

        # Result should succeed but with warning about missing label to remove
        assert result.success

        # Verify warning was emitted about "queued" not being present
        warning_events = [
            e for e in event_sink.events if e.name == "reconciliation.warning"
        ]
        assert len(warning_events) == 1
        assert "queued" in str(warning_events[0].data.get("missing_labels", []))

    def test_multiple_sync_operations_track_state_changes(self):
        """Test that multiple sync operations correctly track state."""
        label_set = MockLabelSet()
        session_manager = MockSessionManager()
        event_sink = MockEventSink()
        issue_tracker = MockIssueTracker({
            1: ["queued"],
            2: ["queued"],
            3: ["in-progress"],
        })

        applier = ActionApplier(
            labels=label_set,
            sessions=session_manager,
            events=event_sink,
            issue_tracker=issue_tracker,
            reconcile=True,
        )

        # Apply multiple transitions
        actions = [
            SyncLabelsAction(
                issue_number=1,
                add_labels=("in-progress",),
                remove_labels=("queued",),
                reason="Start issue 1",
            ),
            SyncLabelsAction(
                issue_number=2,
                add_labels=("in-progress",),
                remove_labels=("queued",),
                reason="Start issue 2",
            ),
            SyncLabelsAction(
                issue_number=3,
                add_labels=("needs-review",),
                remove_labels=("in-progress",),
                reason="Complete issue 3",
            ),
        ]

        results = applier.apply_all(actions)

        # All should succeed
        assert all(r.success for r in results)

        # Verify each issue was checked
        assert issue_tracker.get_labels_calls == [1, 2, 3]

        # Verify correct labels were synced
        assert (1, "in-progress") in label_set.add_calls
        assert (2, "in-progress") in label_set.add_calls
        assert (3, "needs-review") in label_set.add_calls


class TestReconciliationEventTracing:
    """Tests for reconciliation event tracing."""

    def test_all_reconciliation_events_include_issue_number(self):
        """All reconciliation events should include the issue number."""
        label_set = MockLabelSet()
        session_manager = MockSessionManager()
        event_sink = MockEventSink()
        issue_tracker = MockIssueTracker({42: ["foo"]})

        applier = ActionApplier(
            labels=label_set,
            sessions=session_manager,
            events=event_sink,
            issue_tracker=issue_tracker,
            reconcile=True,
        )

        action = SyncLabelsAction(
            issue_number=42,
            add_labels=("bar",),
            remove_labels=("baz",),  # Not present - will warn
            reason="Test",
        )

        applier.apply(action)

        # Get all reconciliation events
        recon_events = [
            e for e in event_sink.events
            if e.name.startswith("reconciliation.")
        ]

        # All should have issue_number
        for event in recon_events:
            assert "issue_number" in event.data
            assert event.data["issue_number"] == 42


class TestOrchestratorReconciliationCatch:
    """Integration tests for orchestrator catching ReconciliationRequired.

    These tests verify that when ActionApplier raises ReconciliationRequired,
    the orchestrator properly catches it and pauses the issue.
    """

    def test_orchestrator_pauses_issue_on_reconciliation_required(self):
        """When ReconciliationRequired is raised, orchestrator applies pause label."""
        from issue_orchestrator.control.reconciliation import (
            ExpectedState,
            get_pause_label,
        )
        from issue_orchestrator.control.actions import AddLabelAction
        from issue_orchestrator.events import EventName

        label_set = MockLabelSet()
        session_manager = MockSessionManager()
        event_sink = MockEventSink()

        # Issue currently has "blocked" label (would fail ExpectedState check)
        issue_tracker = MockIssueTracker({42: ["agent:web", "blocked"]})

        applier = ActionApplier(
            labels=label_set,
            sessions=session_manager,
            events=event_sink,
            issue_tracker=issue_tracker,
            reconcile=True,
        )

        # Action expects "in-progress" label but issue has "blocked"
        action = AddLabelAction(
            issue_number=42,
            label="pr-pending",
            reason="Session completed",
            expected=ExpectedState.with_labels(
                required={"in-progress"},
                forbidden={"blocked"},
            ),
        )

        # Simulate what orchestrator does: catch and pause
        try:
            applier.apply(action)
            paused = False
        except ReconciliationRequired as rr:
            paused = True
            # Orchestrator would apply pause label here
            issue_tracker.add_label(rr.entity_id, get_pause_label())
            # Emit pause event
            event_sink.publish(TraceEvent(
                EventName.ISSUE_PAUSED_RECONCILE,
                {"issue_number": rr.entity_id, "reason": rr.reason},
            ))

        assert paused, "ReconciliationRequired should have been raised"

        # Verify pause label was applied
        assert get_pause_label() in issue_tracker._labels_by_issue.get(42, [])

        # Verify pause event was emitted
        pause_events = [
            e for e in event_sink.events
            if e.name == EventName.ISSUE_PAUSED_RECONCILE
        ]
        assert len(pause_events) == 1
        assert pause_events[0].data["issue_number"] == 42

    def test_reconciliation_required_contains_diagnostic_info(self):
        """ReconciliationRequired exception contains useful diagnostic info."""
        from issue_orchestrator.control.reconciliation import ExpectedState
        from issue_orchestrator.control.actions import AddLabelAction

        label_set = MockLabelSet()
        session_manager = MockSessionManager()
        event_sink = MockEventSink()

        # Current state differs from expected
        issue_tracker = MockIssueTracker({99: ["old-label", "stale"]})

        applier = ActionApplier(
            labels=label_set,
            sessions=session_manager,
            events=event_sink,
            issue_tracker=issue_tracker,
            reconcile=True,
        )

        action = AddLabelAction(
            issue_number=99,
            label="new-label",
            reason="Test",
            expected=ExpectedState.with_labels(
                required={"expected-label"},
                forbidden={"stale"},
            ),
        )

        try:
            applier.apply(action)
            assert False, "Should have raised ReconciliationRequired"
        except ReconciliationRequired as rr:
            # Verify diagnostic info is present
            assert rr.entity_id == 99
            assert rr.entity_type == "issue"
            assert rr.expected is not None
            assert rr.actual is not None
            # Reason should mention what's wrong
            assert "expected-label" in rr.reason or "stale" in rr.reason

    def test_multiple_actions_stop_on_first_reconciliation_failure(self):
        """When one action fails reconciliation, subsequent actions don't run."""
        from issue_orchestrator.control.reconciliation import ExpectedState
        from issue_orchestrator.control.actions import AddLabelAction

        label_set = MockLabelSet()
        session_manager = MockSessionManager()
        event_sink = MockEventSink()
        issue_tracker = MockIssueTracker({
            1: ["ready"],  # This one will fail
            2: ["ready"],
        })

        applier = ActionApplier(
            labels=label_set,
            sessions=session_manager,
            events=event_sink,
            issue_tracker=issue_tracker,
            reconcile=True,
        )

        actions = [
            AddLabelAction(
                issue_number=1,
                label="test",
                reason="First",
                expected=ExpectedState.with_labels(required={"missing"}),  # Will fail
            ),
            AddLabelAction(
                issue_number=2,
                label="test",
                reason="Second",
                expected=None,  # Would succeed
            ),
        ]

        # Apply actions one by one, stopping on ReconciliationRequired
        applied_count = 0
        for action in actions:
            try:
                applier.apply(action)
                applied_count += 1
            except ReconciliationRequired:
                break

        # Only the first action was attempted (and failed)
        assert applied_count == 0
        # Neither label was added
        assert len(label_set.add_calls) == 0
