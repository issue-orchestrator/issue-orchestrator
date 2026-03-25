"""Tests for E2E timeline convergence — E2E events flowing through shared timeline infrastructure."""

import pytest

from issue_orchestrator.domain.timeline_key import TimelineKey
from issue_orchestrator.infra.e2e_db import nest_orchestrator_events
from issue_orchestrator.ports.timeline_store import TimelineRecord
from issue_orchestrator.timeline import (
    TimelineStream,
    _e2e_phase,
    _e2e_status,
    _e2e_level,
    _e2e_summary,
    _phase_for_event,
    _step_for_event,
    _status_for_event,
    _level_for_event,
    _parent_key,
)


class TestE2EPhaseDerivation:
    """E2E events map to domain-specific phases through the shared pipeline."""

    def test_run_started_is_setup(self):
        assert _phase_for_event("e2e.run_started") == "setup"

    def test_tests_collected_is_setup(self):
        assert _phase_for_event("e2e.tests_collected") == "setup"

    def test_test_started_is_execution(self):
        assert _phase_for_event("e2e.test_started") == "execution"

    def test_test_completed_is_execution(self):
        assert _phase_for_event("e2e.test_completed") == "execution"

    def test_retry_started_is_retry(self):
        assert _phase_for_event("e2e.retry_started") == "retry"

    def test_run_finished_is_teardown(self):
        assert _phase_for_event("e2e.run_finished") == "teardown"

    def test_run_canceled_is_teardown(self):
        assert _phase_for_event("e2e.run_canceled") == "teardown"

    def test_run_error_is_teardown(self):
        assert _phase_for_event("e2e.run_error") == "teardown"


class TestE2EStepDerivation:
    def test_strips_e2e_prefix(self):
        assert _step_for_event("e2e.run_started") == "run_started"
        assert _step_for_event("e2e.test_completed") == "test_completed"


class TestE2EStatusDerivation:
    def test_run_error_is_error(self):
        assert _status_for_event("e2e.run_error") == "error"

    def test_run_canceled_is_error(self):
        assert _status_for_event("e2e.run_canceled") == "error"

    def test_test_completed_passed(self):
        assert _status_for_event("e2e.test_completed", {"outcome": "passed"}) == "completed"

    def test_test_completed_failed(self):
        assert _status_for_event("e2e.test_completed", {"outcome": "failed"}) == "error"

    def test_test_completed_skipped(self):
        assert _status_for_event("e2e.test_completed", {"outcome": "skipped"}) == "skipped"

    def test_run_finished_is_completed(self):
        assert _status_for_event("e2e.run_finished") == "completed"

    def test_test_started_is_active(self):
        assert _status_for_event("e2e.test_started") == "active"


class TestE2ELevelDerivation:
    def test_run_error_is_error_level(self):
        assert _level_for_event("e2e.run_error") == "error"

    def test_run_canceled_is_warning(self):
        assert _level_for_event("e2e.run_canceled") == "warning"

    def test_test_completed_is_detail(self):
        assert _level_for_event("e2e.test_completed") == "detail"

    def test_run_started_is_info(self):
        assert _level_for_event("e2e.run_started") == "info"


class TestE2EParentKey:
    def test_negative_issue_number_produces_e2e_parent_key(self):
        assert _parent_key(-42, {}) == "e2e-run-42"

    def test_positive_issue_number_unchanged(self):
        assert _parent_key(123, {}) == "issue:123"


class TestE2ETimelineStreamRoundtrip:
    """E2E events stored as TimelineRecords can be read back via TimelineStream."""

    def _make_record(self, event_name: str, data: dict | None = None) -> TimelineRecord:
        return TimelineRecord(
            event_id="evt1",
            timestamp="2026-01-01T00:00:00Z",
            event=event_name,
            data=data or {},
            source_event=event_name,
        )

    def test_e2e_run_started_roundtrip(self):
        record = self._make_record("e2e.run_started", {"branch": "main"})
        stream = TimelineStream.from_records(-42, [record])

        assert len(stream.events) == 1
        event = stream.events[0]
        assert event.phase == "setup"
        assert event.step == "run_started"
        assert event.status == "active"
        assert event.level == "info"
        assert event.parent_key == "e2e-run-42"
        assert "main" in (event.summary or "")

    def test_e2e_test_completed_passed(self):
        record = self._make_record("e2e.test_completed", {
            "nodeid": "tests/e2e/test_foo.py::test_bar",
            "outcome": "passed",
            "duration_seconds": 2.5,
        })
        stream = TimelineStream.from_records(-1, [record])
        event = stream.events[0]
        assert event.phase == "execution"
        assert event.status == "completed"
        assert "passed" in (event.summary or "")
        assert "2.5s" in (event.summary or "")

    def test_e2e_test_completed_failed(self):
        record = self._make_record("e2e.test_completed", {
            "nodeid": "tests/e2e/test_foo.py::test_bar",
            "outcome": "failed",
        })
        stream = TimelineStream.from_records(-1, [record])
        event = stream.events[0]
        assert event.status == "error"

    def test_e2e_run_finished(self):
        record = self._make_record("e2e.run_finished", {
            "status": "passed",
            "duration_seconds": 120.5,
        })
        stream = TimelineStream.from_records(-1, [record])
        event = stream.events[0]
        assert event.phase == "teardown"
        assert event.status == "completed"

    def test_e2e_run_error(self):
        record = self._make_record("e2e.run_error", {
            "error": "Something went wrong",
        })
        stream = TimelineStream.from_records(-1, [record])
        event = stream.events[0]
        assert event.status == "error"
        assert event.level == "error"
        assert "Something went wrong" in (event.summary or "")


class TestSourceEventSemantics:
    """Verify completion events derive from their own name, not the paired started event.

    In the legacy e2e_run_events table, source_event is a *pairing* concept:
    e2e.test_completed stores source_event=e2e.test_started.  In timeline.sqlite,
    source_event is the *canonical name* used for derivation.  If we wrote the
    pairing value, completions would render as started events (bug).
    """

    def test_test_completed_with_pairing_source_event_renders_wrong(self):
        """Demonstrates the bug: pairing source_event causes wrong derivation."""
        record = TimelineRecord(
            event_id="evt1",
            timestamp="2026-01-01T00:00:00Z",
            event="e2e.test_completed",
            data={"nodeid": "test_a", "outcome": "passed", "duration_seconds": 1.0},
            source_event="e2e.test_started",  # Pairing value — wrong for timeline
        )
        stream = TimelineStream.from_records(-1, [record])
        event = stream.events[0]
        # With pairing source_event, derivation uses "e2e.test_started" -> wrong
        assert event.step == "test_started"  # Bug: should be "test_completed"
        assert event.status == "active"  # Bug: should be "completed"

    def test_test_completed_with_correct_source_event(self):
        """With source_event=event_name, derivation is correct."""
        record = TimelineRecord(
            event_id="evt1",
            timestamp="2026-01-01T00:00:00Z",
            event="e2e.test_completed",
            data={"nodeid": "test_a", "outcome": "passed", "duration_seconds": 1.0},
            source_event="e2e.test_completed",  # Own name — correct for timeline
        )
        stream = TimelineStream.from_records(-1, [record])
        event = stream.events[0]
        assert event.step == "test_completed"
        assert event.status == "completed"

    def test_run_finished_with_pairing_source_event_renders_wrong(self):
        """Demonstrates the bug: pairing source_event on run_finished."""
        record = TimelineRecord(
            event_id="evt1",
            timestamp="2026-01-01T00:00:00Z",
            event="e2e.run_finished",
            data={"status": "passed", "duration_seconds": 60.0},
            source_event="e2e.run_started",  # Pairing value — wrong
        )
        stream = TimelineStream.from_records(-1, [record])
        event = stream.events[0]
        assert event.step == "run_started"  # Bug
        assert event.status == "active"  # Bug

    def test_run_finished_with_correct_source_event(self):
        """With source_event=event_name, run_finished derives correctly."""
        record = TimelineRecord(
            event_id="evt1",
            timestamp="2026-01-01T00:00:00Z",
            event="e2e.run_finished",
            data={"status": "passed", "duration_seconds": 60.0},
            source_event="e2e.run_finished",
        )
        stream = TimelineStream.from_records(-1, [record])
        event = stream.events[0]
        assert event.step == "run_finished"
        assert event.status == "completed"
        assert event.phase == "teardown"


class TestE2ETimelineStoreIntegration:
    """E2E events can be written to and read from SqliteTimelineStore."""

    @pytest.fixture
    def store(self, tmp_path):
        from issue_orchestrator.execution.timeline_store import SqliteTimelineStore
        return SqliteTimelineStore(db_path=tmp_path / "timeline.sqlite")

    def test_write_and_read_e2e_events(self, store):
        key = TimelineKey.for_e2e_run(42)
        store_key = key.to_store_key()

        records = [
            TimelineRecord(
                event_id="evt1",
                timestamp="2026-01-01T00:00:00Z",
                event="e2e.run_started",
                data={"branch": "main", "e2e_run_id": 42},
                source_event="e2e.run_started",
            ),
            TimelineRecord(
                event_id="evt2",
                timestamp="2026-01-01T00:00:10Z",
                event="e2e.test_completed",
                data={"nodeid": "test_a", "outcome": "passed", "duration_seconds": 5.0, "e2e_run_id": 42},
                source_event="e2e.test_completed",
            ),
            TimelineRecord(
                event_id="evt3",
                timestamp="2026-01-01T00:01:00Z",
                event="e2e.run_finished",
                data={"status": "passed", "duration_seconds": 60.0, "e2e_run_id": 42},
                source_event="e2e.run_finished",
            ),
        ]
        for record in records:
            store.append(store_key, record)

        read_back = store.read(store_key)
        assert len(read_back) == 3
        assert read_back[0].event == "e2e.run_started"
        assert read_back[2].event == "e2e.run_finished"

    def test_e2e_and_issue_events_isolated(self, store):
        """E2E events don't appear in issue timelines and vice versa."""
        issue_key = TimelineKey.for_issue(123).to_store_key()
        e2e_key = TimelineKey.for_e2e_run(42).to_store_key()

        store.append(issue_key, TimelineRecord(
            event_id="issue-evt", timestamp="2026-01-01T00:00:00Z",
            event="session.started", data={"run_dir": "/tmp/fake-run"},
            source_event="session.started",
        ))
        store.append(e2e_key, TimelineRecord(
            event_id="e2e-evt", timestamp="2026-01-01T00:00:00Z",
            event="e2e.run_started", data={"branch": "main"},
            source_event="e2e.run_started",
        ))

        issue_records = store.read(issue_key)
        e2e_records = store.read(e2e_key)

        assert len(issue_records) == 1
        assert issue_records[0].event == "session.started"
        assert len(e2e_records) == 1
        assert e2e_records[0].event == "e2e.run_started"

    def test_full_pipeline_through_timeline_stream(self, store):
        """E2E events written to store flow through TimelineStream correctly."""
        key = TimelineKey.for_e2e_run(7)
        store_key = key.to_store_key()

        store.append(store_key, TimelineRecord(
            event_id="e1", timestamp="2026-01-01T00:00:00Z",
            event="e2e.run_started", data={"branch": "feature-x"},
            source_event="e2e.run_started",
        ))
        store.append(store_key, TimelineRecord(
            event_id="e2", timestamp="2026-01-01T00:00:30Z",
            event="e2e.test_completed",
            data={"nodeid": "test_a", "outcome": "failed", "duration_seconds": 25.0},
            source_event="e2e.test_completed",
        ))

        records = store.read(store_key)
        stream = TimelineStream.from_records(store_key, records)

        assert len(stream.events) == 2
        assert stream.events[0].phase == "setup"
        assert stream.events[0].parent_key == "e2e-run-7"
        assert stream.events[1].phase == "execution"
        assert stream.events[1].status == "error"

        # Verify dict serialization works
        timeline_dict = stream.to_dict()
        assert len(timeline_dict["events"]) == 2
        assert timeline_dict["events"][0]["phase"] == "setup"


class TestNestOrchestratorEvents:
    """Orchestrator events are nested as children under E2E test time windows."""

    def test_orch_event_nested_under_matching_test_window(self):
        """An orchestrator event within a test window becomes a child of that test."""
        pytest_events = [
            {"event": "e2e.test_started", "timestamp": "2026-01-01T00:00:00Z", "nodeid": "test_a"},
            {"event": "e2e.test_completed", "timestamp": "2026-01-01T00:00:30Z", "nodeid": "test_a",
             "status": "completed"},
        ]
        orch_events = [
            {"event": "session.started", "timestamp": "2026-01-01T00:00:10Z",
             "step": "started", "status": "active", "summary": "Agent launched"},
        ]
        nest_orchestrator_events(pytest_events, orch_events)

        # The test_started event gets the child (it's the window opener)
        assert len(pytest_events[0]["children"]) == 1
        assert pytest_events[0]["children"][0]["event"] == "session.started"

    def test_orch_events_outside_window_attach_to_nearest(self):
        """Orchestrator events outside test windows attach to nearest preceding event."""
        pytest_events = [
            {"event": "e2e.run_started", "timestamp": "2026-01-01T00:00:00Z"},
            {"event": "e2e.test_started", "timestamp": "2026-01-01T00:01:00Z", "nodeid": "test_a"},
            {"event": "e2e.test_completed", "timestamp": "2026-01-01T00:02:00Z", "nodeid": "test_a"},
        ]
        orch_events = [
            {"event": "tick.started", "timestamp": "2026-01-01T00:00:30Z",
             "step": "tick_started", "status": "active"},
        ]
        nest_orchestrator_events(pytest_events, orch_events)

        # Event at :30 is before the test window (:01:00-:02:00), so attaches to run_started
        assert len(pytest_events[0]["children"]) == 1
        assert pytest_events[0]["children"][0]["event"] == "tick.started"

    def test_no_orch_events_means_no_children(self):
        """When there are no orchestrator events, no children are added."""
        pytest_events = [
            {"event": "e2e.test_started", "timestamp": "2026-01-01T00:00:00Z", "nodeid": "test_a"},
            {"event": "e2e.test_completed", "timestamp": "2026-01-01T00:00:30Z", "nodeid": "test_a"},
        ]
        nest_orchestrator_events(pytest_events, [])
        # children lists are initialized but empty
        assert pytest_events[0].get("children", []) == []

    def test_multiple_tests_get_correct_children(self):
        """Each test window gets its own orchestrator events."""
        pytest_events = [
            {"event": "e2e.test_started", "timestamp": "2026-01-01T00:00:00Z", "nodeid": "test_a"},
            {"event": "e2e.test_completed", "timestamp": "2026-01-01T00:01:00Z", "nodeid": "test_a"},
            {"event": "e2e.test_started", "timestamp": "2026-01-01T00:02:00Z", "nodeid": "test_b"},
            {"event": "e2e.test_completed", "timestamp": "2026-01-01T00:03:00Z", "nodeid": "test_b"},
        ]
        orch_events = [
            {"event": "session.started", "timestamp": "2026-01-01T00:00:30Z",
             "step": "started", "status": "active", "summary": "Agent A"},
            {"event": "review.approved", "timestamp": "2026-01-01T00:02:30Z",
             "step": "approved", "status": "completed", "summary": "Review OK"},
        ]
        nest_orchestrator_events(pytest_events, orch_events)

        # test_a's window gets session.started
        assert len(pytest_events[0]["children"]) == 1
        assert pytest_events[0]["children"][0]["event"] == "session.started"
        # test_b's window gets review.approved
        assert len(pytest_events[2]["children"]) == 1
        assert pytest_events[2]["children"][0]["event"] == "review.approved"

    def test_shared_pipeline_events_nest_correctly(self):
        """Events from TimelineStream.to_dict() can be nested with orchestrator events."""
        # Simulate what the shared endpoint does: read E2E events from store,
        # convert to dicts, then nest orchestrator events
        records = [
            TimelineRecord(
                event_id="e1", timestamp="2026-01-01T00:00:00Z",
                event="e2e.test_started", data={"nodeid": "test_a"},
                source_event="e2e.test_started",
            ),
            TimelineRecord(
                event_id="e2", timestamp="2026-01-01T00:01:00Z",
                event="e2e.test_completed",
                data={"nodeid": "test_a", "outcome": "passed", "duration_seconds": 55.0},
                source_event="e2e.test_completed",
            ),
        ]
        stream = TimelineStream.from_records(-1, records)
        events = [evt.to_dict() for evt in stream.events]

        # Orchestrator events that happened during the test
        orch_events = [
            {"event": "session.completed", "timestamp": "2026-01-01T00:00:45Z",
             "step": "completed", "status": "completed", "summary": "Code written"},
        ]
        nest_orchestrator_events(events, orch_events)

        # The test_started event dict should have the orchestrator event as a child
        assert len(events[0].get("children", [])) == 1
        assert events[0]["children"][0]["event"] == "session.completed"
