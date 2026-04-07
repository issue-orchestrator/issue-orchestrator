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


class TestOrchestratorWindowExcludesE2EEvents:
    """read_orchestrator_events_by_window must not return E2E run events."""

    def test_excludes_negative_key_events(self, tmp_path):
        """E2E events (negative issue_number) are excluded from orchestrator results."""
        from issue_orchestrator.execution.timeline_store import SqliteTimelineStore
        from issue_orchestrator.infra.e2e_timeline import read_orchestrator_events_by_window

        store = SqliteTimelineStore(db_path=tmp_path / "timeline.sqlite")

        e2e_key = TimelineKey.for_e2e_run(1).to_store_key()
        issue_key = TimelineKey.for_issue(42).to_store_key()

        store.append(e2e_key, TimelineRecord(
            event_id="e2e-evt", timestamp="2026-01-01T00:00:10Z",
            event="e2e.test_started", data={"nodeid": "test_a"},
            source_event="e2e.test_started",
        ))
        store.append(issue_key, TimelineRecord(
            event_id="orch-evt", timestamp="2026-01-01T00:00:15Z",
            event="session.started", data={"run_dir": "/tmp/fake"},
            source_event="session.started",
        ))

        results = read_orchestrator_events_by_window(
            tmp_path / "timeline.sqlite",
            started_at="2026-01-01T00:00:00Z",
            finished_at="2026-01-01T00:01:00Z",
        )

        assert len(results) == 1
        assert results[0].get("event") == "session.started"

    def test_returns_empty_for_nonexistent_db(self, tmp_path):
        """Returns empty list when the timeline DB does not exist."""
        from issue_orchestrator.infra.e2e_timeline import read_orchestrator_events_by_window

        results = read_orchestrator_events_by_window(
            tmp_path / "nonexistent.sqlite",
            started_at="2026-01-01T00:00:00Z",
            finished_at="2026-01-01T00:01:00Z",
        )
        assert results == []


class TestE2ERunDetailEndpoint:
    """Endpoint-level tests for GET /api/e2e-run-detail/{run_id}."""

    def _setup_orchestrator_with_timeline(self, store_key, records):
        """Set up a mock orchestrator whose timeline_reader returns the given records."""
        from unittest.mock import MagicMock
        from issue_orchestrator.entrypoints.web import app, set_orchestrator
        from fastapi.testclient import TestClient
        from pathlib import Path

        mock_orch = MagicMock()
        mock_orch.config.repo_root = Path("/tmp/nonexistent")

        mock_orch.deps.timeline_store.read.return_value = records

        return mock_orch, TestClient(app)

    def test_returns_404_when_no_events(self):
        """Endpoint returns 404 for runs with no shared timeline events."""
        from issue_orchestrator.entrypoints.web import app, set_orchestrator
        from unittest.mock import MagicMock
        from fastapi.testclient import TestClient
        from pathlib import Path

        mock_orch = MagicMock()
        mock_orch.config.repo_root = Path("/tmp/nonexistent")
        mock_orch.deps.timeline_store.read.return_value = []
        set_orchestrator(mock_orch)
        try:
            client = TestClient(app)
            response = client.get("/api/e2e-run-detail/99")
            assert response.status_code == 404
            assert response.json()["error"] == "not_found"
        finally:
            set_orchestrator(None)

    def test_returns_200_with_e2e_events(self):
        """Endpoint returns 200 with events from the shared timeline store."""
        from issue_orchestrator.entrypoints.web import set_orchestrator

        store_key = TimelineKey.for_e2e_run(42).to_store_key()
        records = [
            TimelineRecord(
                event_id="e1", timestamp="2026-01-01T00:00:00Z",
                event="e2e.run_started", data={"branch": "main"},
                source_event="e2e.run_started",
            ),
            TimelineRecord(
                event_id="e2", timestamp="2026-01-01T00:01:00Z",
                event="e2e.run_finished", data={"status": "passed", "duration_seconds": 60.0},
                source_event="e2e.run_finished",
            ),
        ]
        mock_orch, client = self._setup_orchestrator_with_timeline(store_key, records)
        set_orchestrator(mock_orch)
        try:
            response = client.get("/api/e2e-run-detail/42")
            assert response.status_code == 200
            payload = response.json()
            assert payload["title"] == "E2E Run #42"
            events = payload.get("events", [])
            assert len(events) >= 2
            phases = {e["phase"] for e in events}
            assert "setup" in phases
            assert "teardown" in phases
        finally:
            set_orchestrator(None)

    def test_test_events_carry_issue_numbers_for_navigation(self):
        """Test events expose issue_numbers for the frontend to render as links.

        New architecture: instead of nesting agent events as children,
        each test event carries the issue numbers it operated on. The
        frontend renders these as clickable links to the full dashboard
        issue detail view.
        """
        from issue_orchestrator.entrypoints.web import set_orchestrator

        store_key = TimelineKey.for_e2e_run(10).to_store_key()
        records = [
            TimelineRecord(
                event_id="e1", timestamp="2026-01-01T00:00:00Z",
                event="e2e.test_started", data={"nodeid": "test_a"},
                source_event="e2e.test_started",
            ),
            TimelineRecord(
                event_id="e2", timestamp="2026-01-01T00:01:00Z",
                event="e2e.test_completed",
                data={"nodeid": "test_a", "outcome": "passed", "duration_seconds": 55},
                source_event="e2e.test_completed",
            ),
            TimelineRecord(
                event_id="snap-s1", timestamp="2026-01-01T00:00:30Z",
                event="e2e.agent_snapshot",
                data={"event": "session.started", "timestamp": "2026-01-01T00:00:30Z",
                      "issue_number": 42, "phase": "in_progress", "step": "started",
                      "status": "started", "summary": "Agent launched",
                      "views": ["user", "ops", "debug"]},
                source_event="e2e.agent_snapshot",
            ),
        ]
        mock_orch, client = self._setup_orchestrator_with_timeline(store_key, records)
        set_orchestrator(mock_orch)
        try:
            response = client.get("/api/e2e-run-detail/10")
            assert response.status_code == 200
            payload = response.json()
            events = payload.get("events", [])
            test_started = next((e for e in events if e.get("event") == "e2e.test_started"), None)
            assert test_started is not None
            assert test_started.get("issue_numbers") == [42]
            test_completed = next((e for e in events if e.get("event") == "e2e.test_completed"), None)
            assert test_completed is not None
            assert test_completed.get("issue_numbers") == [42]
        finally:
            set_orchestrator(None)

    def test_returns_503_when_orchestrator_not_running(self):
        """Endpoint returns 503 when orchestrator is not set."""
        from issue_orchestrator.entrypoints.web import app, set_orchestrator
        from fastapi.testclient import TestClient

        set_orchestrator(None)
        client = TestClient(app)
        response = client.get("/api/e2e-run-detail/1")
        assert response.status_code == 503


class TestE2EAgentEventFiltering:
    """Agent events are filtered to story view before nesting."""

    def test_debug_only_events_filtered_out(self):
        """Events with views=['debug'] are excluded from user view."""
        from issue_orchestrator.view_models.issue_detail import _filter_events_by_view

        agent_events = [
            {"event": "claim.acquired", "views": ["debug"], "timestamp": "2026-01-01T00:00:10Z"},
            {"event": "session.started", "views": ["user", "ops", "debug"], "timestamp": "2026-01-01T00:00:20Z"},
            {"event": "apply.step_applied", "views": ["ops", "debug"], "timestamp": "2026-01-01T00:00:30Z"},
            {"event": "session.completed", "views": ["user", "ops", "debug"], "timestamp": "2026-01-01T00:00:40Z"},
        ]
        filtered = _filter_events_by_view(agent_events, "user")

        events = [e["event"] for e in filtered]
        assert "claim.acquired" not in events
        assert "apply.step_applied" not in events
        assert "session.started" in events
        assert "session.completed" in events

    def test_legacy_events_without_views_pass_through(self):
        """Events without views tag are included in all views."""
        from issue_orchestrator.view_models.issue_detail import _filter_events_by_view

        agent_events = [
            {"event": "session.started", "timestamp": "2026-01-01T00:00:10Z"},
            {"event": "review.approved", "timestamp": "2026-01-01T00:00:20Z"},
        ]
        filtered = _filter_events_by_view(agent_events, "user")
        assert len(filtered) == 2


    def test_story_projection_per_window_preserves_both_reviews(self):
        """Story projection runs per test window, not globally.

        Two tests each have a review.started event. If projection ran
        globally, consecutive review.started events could be collapsed
        across windows, dropping one test's review activity.
        """
        from issue_orchestrator.infra.e2e_db import nest_orchestrator_events
        from issue_orchestrator.view_models.issue_detail import _story_projection_events

        pytest_events = [
            {"event": "e2e.test_started", "timestamp": "2026-01-01T00:00:00Z", "nodeid": "test_a"},
            {"event": "e2e.test_completed", "timestamp": "2026-01-01T00:01:00Z", "nodeid": "test_a"},
            {"event": "e2e.test_started", "timestamp": "2026-01-01T00:02:00Z", "nodeid": "test_b"},
            {"event": "e2e.test_completed", "timestamp": "2026-01-01T00:03:00Z", "nodeid": "test_b"},
        ]
        agent_events = [
            {"event": "review.started", "timestamp": "2026-01-01T00:00:30Z",
             "phase": "reviewing", "step": "started", "status": "started"},
            {"event": "review.started", "timestamp": "2026-01-01T00:02:30Z",
             "phase": "reviewing", "step": "started", "status": "started"},
        ]
        nest_orchestrator_events(pytest_events, agent_events)

        # Apply story projection per window (not globally)
        for evt in pytest_events:
            children = evt.get("children")
            if children:
                evt["children"] = _story_projection_events(children, "user")

        # Both test windows should retain their review.started child
        test_a_children = pytest_events[0].get("children", [])
        test_b_children = pytest_events[2].get("children", [])
        assert len(test_a_children) == 1, f"test_a lost its review child: {test_a_children}"
        assert len(test_b_children) == 1, f"test_b lost its review child: {test_b_children}"
        assert test_a_children[0]["event"] == "review.started"
        assert test_b_children[0]["event"] == "review.started"


class TestE2ETimelineControlEndpoint:
    """Test /control/e2e/run/{run_id}/timeline returns phase_toc and cycles."""

    def test_returns_phase_toc_and_cycles(self, tmp_path):
        """Timeline endpoint includes phase_toc and cycles alongside events."""
        from fastapi.testclient import TestClient
        from issue_orchestrator.execution.timeline_store import SqliteTimelineStore
        from issue_orchestrator.entrypoints.control_api import control_app

        # Write E2E events to timeline store
        state_dir = tmp_path / ".issue-orchestrator" / "state"
        state_dir.mkdir(parents=True)
        store = SqliteTimelineStore(db_path=state_dir / "timeline.sqlite", instance_id="inst-1")

        e2e_key = TimelineKey.for_e2e_run(1).to_store_key()
        store.append(e2e_key, TimelineRecord(
            event_id="e1", timestamp="2026-01-01T00:00:00Z",
            event="e2e.run_started", data={"branch": "main"},
            source_event="e2e.run_started",
        ))
        store.append(e2e_key, TimelineRecord(
            event_id="e2", timestamp="2026-01-01T00:01:00Z",
            event="e2e.run_finished", data={"status": "passed", "duration_seconds": 60},
            source_event="e2e.run_finished",
        ))

        client = TestClient(control_app)
        response = client.get(
            "/control/e2e/run/1/timeline",
            params={"repo_root": str(tmp_path)},
        )
        assert response.status_code == 200
        payload = response.json()

        assert "events" in payload
        assert len(payload["events"]) == 2
        assert "phase_toc" in payload
        assert "cycles" in payload
        assert isinstance(payload["phase_toc"], list)
        assert isinstance(payload["cycles"], list)
        # phase_toc should have setup and teardown phases
        toc_phases = {item.get("phase") for item in payload["phase_toc"]}
        assert "setup" in toc_phases
        assert "teardown" in toc_phases

    def test_snapshotted_agent_events_attach_issue_numbers(self, tmp_path):
        """Snapshotted agent events annotate test events with issue_numbers
        for the navigation-based architecture (not nested as children)."""
        from fastapi.testclient import TestClient
        from issue_orchestrator.execution.timeline_store import SqliteTimelineStore
        from issue_orchestrator.entrypoints.control_api import control_app

        state_dir = tmp_path / ".issue-orchestrator" / "state"
        state_dir.mkdir(parents=True)
        store = SqliteTimelineStore(db_path=state_dir / "timeline.sqlite")

        e2e_key = TimelineKey.for_e2e_run(1).to_store_key()

        # Pytest events
        store.append(e2e_key, TimelineRecord(
            event_id="e1", timestamp="2026-01-01T00:00:00Z",
            event="e2e.test_started", data={"nodeid": "test_a"},
            source_event="e2e.test_started",
        ))
        store.append(e2e_key, TimelineRecord(
            event_id="e2", timestamp="2026-01-01T00:01:00Z",
            event="e2e.test_completed",
            data={"nodeid": "test_a", "outcome": "passed", "duration_seconds": 55},
            source_event="e2e.test_completed",
        ))

        # Snapshotted agent events for issue 42 (within the test window)
        store.append(e2e_key, TimelineRecord(
            event_id="snap-s1", timestamp="2026-01-01T00:00:30Z",
            event="e2e.agent_snapshot",
            data={"event": "session.started", "timestamp": "2026-01-01T00:00:30Z",
                  "issue_number": 42, "phase": "in_progress", "step": "started",
                  "status": "started", "summary": "Agent launched",
                  "views": ["user", "ops", "debug"]},
            source_event="e2e.agent_snapshot",
        ))
        store.append(e2e_key, TimelineRecord(
            event_id="snap-s2", timestamp="2026-01-01T00:00:50Z",
            event="e2e.agent_snapshot",
            data={"event": "session.completed", "timestamp": "2026-01-01T00:00:50Z",
                  "issue_number": 42, "phase": "in_progress", "step": "completed",
                  "status": "completed", "summary": "Code written",
                  "views": ["user", "ops", "debug"]},
            source_event="e2e.agent_snapshot",
        ))

        client = TestClient(control_app)
        response = client.get(
            "/control/e2e/run/1/timeline",
            params={"repo_root": str(tmp_path)},
        )
        assert response.status_code == 200
        payload = response.json()

        events = payload["events"]
        assert len(events) == 2
        test_started = events[0]
        assert test_started["event"] == "e2e.test_started"
        # Test event has issue_numbers annotation, not nested children
        assert test_started.get("issue_numbers") == [42]
        # No nested children — frontend opens issue detail via openIssueDetail
        assert test_started.get("children", []) == []

    def test_returns_empty_events_when_no_timeline(self, tmp_path):
        """Returns empty events list when no timeline DB exists."""
        from fastapi.testclient import TestClient
        from issue_orchestrator.entrypoints.control_api import control_app

        state_dir = tmp_path / ".issue-orchestrator" / "state"
        state_dir.mkdir(parents=True)
        # Create empty timeline DB
        from issue_orchestrator.execution.timeline_store import SqliteTimelineStore
        SqliteTimelineStore(db_path=state_dir / "timeline.sqlite")

        client = TestClient(control_app)
        response = client.get(
            "/control/e2e/run/999/timeline",
            params={"repo_root": str(tmp_path)},
        )
        assert response.status_code == 200
        assert response.json()["events"] == []


class TestControlIssueDetailEndpoint:
    """Control center serves issue detail from base repo or E2E worktree timeline."""

    def test_serves_issue_detail_from_base_repo(self, tmp_path):
        """Control endpoint reads from base repo timeline.sqlite."""
        from fastapi.testclient import TestClient
        from issue_orchestrator.execution.timeline_store import SqliteTimelineStore
        from issue_orchestrator.entrypoints.control_api import control_app

        state_dir = tmp_path / ".issue-orchestrator" / "state"
        state_dir.mkdir(parents=True)
        store = SqliteTimelineStore(db_path=state_dir / "timeline.sqlite")

        # Issue 42 has session events
        store.append(42, TimelineRecord(
            event_id="e1", timestamp="2026-01-01T00:00:00Z",
            event="session.started",
            data={"run_dir": "/tmp/fake-run", "logical_run": 1, "logical_cycle": 1,
                  "logical_phase": "coding", "timeline_schema_version": 4,
                  "views": ["user", "ops", "debug"]},
            source_event="session.started",
        ))

        client = TestClient(control_app)
        response = client.get(
            "/api/issue-detail/42",
            params={"repo_root": str(tmp_path)},
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["issue_number"] == 42
        assert "events" in payload
        assert "runs" in payload  # Same shape as dashboard issue detail

    def test_falls_back_to_e2e_worktree_timeline(self, tmp_path):
        """When base repo has no events, control endpoint reads from E2E worktree."""
        from fastapi.testclient import TestClient
        from issue_orchestrator.execution.timeline_store import SqliteTimelineStore
        from issue_orchestrator.entrypoints.control_api import control_app
        from issue_orchestrator.infra.e2e_worktree import get_e2e_worktree_path

        # Empty base repo timeline
        state_dir = tmp_path / ".issue-orchestrator" / "state"
        state_dir.mkdir(parents=True)
        SqliteTimelineStore(db_path=state_dir / "timeline.sqlite")

        # E2E worktree timeline has the issue events
        wt_path = get_e2e_worktree_path(tmp_path)
        wt_state = wt_path / ".issue-orchestrator" / "state"
        wt_state.mkdir(parents=True)
        wt_store = SqliteTimelineStore(db_path=wt_state / "timeline.sqlite")
        wt_store.append(99, TimelineRecord(
            event_id="e1", timestamp="2026-01-01T00:00:00Z",
            event="session.completed",
            data={"logical_run": 1, "logical_cycle": 1, "logical_phase": "coding",
                  "timeline_schema_version": 4, "views": ["user", "ops", "debug"]},
            source_event="session.completed",
        ))

        client = TestClient(control_app)
        response = client.get(
            "/api/issue-detail/99",
            params={"repo_root": str(tmp_path)},
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["issue_number"] == 99
        assert len(payload.get("events", [])) > 0

    def test_returns_404_when_no_events(self, tmp_path):
        """Control endpoint returns 404 when no events anywhere."""
        from fastapi.testclient import TestClient
        from issue_orchestrator.entrypoints.control_api import control_app

        client = TestClient(control_app)
        response = client.get(
            "/api/issue-detail/999",
            params={"repo_root": str(tmp_path)},
        )
        assert response.status_code == 404


class TestPruneWorktreeArtifacts:
    """prune_old_runs with e2e_worktree_path cleans worktree-local data."""

    def test_prunes_old_worktree_sessions_and_timeline(self, tmp_path):
        """Old session dirs and timeline events are removed when runs are pruned."""
        import time
        from issue_orchestrator.infra.e2e_db import E2EDB
        from issue_orchestrator.execution.timeline_store import SqliteTimelineStore

        # Set up E2E DB with 3 runs
        db = E2EDB(tmp_path / "e2e.db")
        for i in range(3):
            run_id = db.start_run(
                repo_root=str(tmp_path),
                orchestrator_id="test-orch",
                pytest_args=["tests/e2e"],
            )
            db.finish_run(run_id=run_id, status="passed", duration_seconds=10.0)
            time.sleep(0.05)  # Ensure distinct timestamps

        # Set up worktree with sessions and timeline
        wt = tmp_path / "e2e-worktree"
        wt_state = wt / ".issue-orchestrator" / "state"
        wt_state.mkdir(parents=True)
        wt_sessions = wt / ".issue-orchestrator" / "sessions"

        # Create 3 session dirs with distinct mtimes
        for i in range(3):
            session_dir = wt_sessions / f"session-{i}"
            session_dir.mkdir(parents=True)
            (session_dir / "terminal-recording.jsonl").write_text("data")

        # Backdate old session dirs
        import os
        old_time = time.time() - 86400  # 1 day ago
        for i in range(2):
            session_dir = wt_sessions / f"session-{i}"
            os.utime(session_dir, (old_time + i, old_time + i))

        # Write timeline events
        wt_store = SqliteTimelineStore(db_path=wt_state / "timeline.sqlite")
        wt_store.append(1, TimelineRecord(
            event_id="old", timestamp="2025-01-01T00:00:00Z",
            event="session.started", data={"run_dir": "/fake"},
            source_event="session.started",
        ))
        wt_store.append(2, TimelineRecord(
            event_id="new", timestamp="2099-01-01T00:00:00Z",
            event="session.started", data={"run_dir": "/fake"},
            source_event="session.started",
        ))

        # Prune to keep only 1 run
        db.prune_old_runs(1, e2e_worktree_path=wt)

        # Old session dirs should be gone, newest kept
        remaining_sessions = list(wt_sessions.iterdir())
        assert len(remaining_sessions) == 1
        assert remaining_sessions[0].name == "session-2"

        # Old timeline events should be pruned
        import sqlite3
        conn = sqlite3.connect(wt_state / "timeline.sqlite")
        rows = conn.execute("SELECT event_id FROM timeline_events").fetchall()
        conn.close()
        event_ids = {r[0] for r in rows}
        assert "old" not in event_ids
        assert "new" in event_ids

    def test_no_error_when_worktree_missing(self, tmp_path):
        """Pruning works without error when worktree path doesn't exist."""
        from issue_orchestrator.infra.e2e_db import E2EDB

        db = E2EDB(tmp_path / "e2e.db")
        run_id = db.start_run(
            repo_root=str(tmp_path),
            orchestrator_id="test-orch",
            pytest_args=["tests/e2e"],
        )
        db.finish_run(run_id=run_id, status="passed", duration_seconds=10.0)

        # Should not raise
        db.prune_old_runs(0, e2e_worktree_path=tmp_path / "nonexistent")


class TestCheckE2ECompletion:
    """Tests for _check_e2e_completion SSE broadcasting on worker exit."""

    def _run_check(self, tmp_path, finished_ids, run_status="passed"):
        """Set up mocks and call _check_e2e_completion."""
        from unittest.mock import MagicMock, patch
        from issue_orchestrator.infra.e2e_db import E2EDB

        mock_orch = MagicMock()
        mock_orch.config.repo_root = tmp_path

        mock_runner = MagicMock()
        mock_runner.cleanup_finished.return_value = finished_ids

        if finished_ids:
            # Create a real E2E DB with a finished run
            db_dir = tmp_path / ".issue-orchestrator"
            db_dir.mkdir(parents=True)
            db = E2EDB(db_dir / "e2e.db")
            orch_id = finished_ids[0]
            run_id = db.start_run(
                repo_root=str(tmp_path),
                orchestrator_id=orch_id,
                pytest_args=["tests/e2e"],
            )
            db.finish_run(run_id=run_id, status=run_status, duration_seconds=10.0)

        with patch("issue_orchestrator.infra.orchestrator.get_e2e_runner_manager", return_value=mock_runner):
            from issue_orchestrator.infra.orchestrator import Orchestrator
            Orchestrator._check_e2e_completion(mock_orch)

        return mock_orch

    def test_publishes_completed_event_on_passed_run(self, tmp_path):
        """When E2E worker finishes with passed status, E2E_COMPLETED is published."""
        from issue_orchestrator.events.catalog import EventName

        mock_orch = self._run_check(tmp_path, ["test-orch"], run_status="passed")
        mock_orch.deps.events.publish.assert_called_once()
        call_args = mock_orch.deps.events.publish.call_args[0][0]
        assert call_args.name == EventName.E2E_COMPLETED

    def test_publishes_failed_event_on_failed_run(self, tmp_path):
        """When E2E worker finishes with failed status, E2E_FAILED is published."""
        from issue_orchestrator.events.catalog import EventName

        mock_orch = self._run_check(tmp_path, ["test-orch"], run_status="failed")
        mock_orch.deps.events.publish.assert_called_once()
        call_args = mock_orch.deps.events.publish.call_args[0][0]
        assert call_args.name == EventName.E2E_FAILED

    def test_no_publish_when_no_workers_finished(self, tmp_path):
        """When no E2E workers have finished, nothing is published."""
        mock_orch = self._run_check(tmp_path, [])
        mock_orch.deps.events.publish.assert_not_called()
