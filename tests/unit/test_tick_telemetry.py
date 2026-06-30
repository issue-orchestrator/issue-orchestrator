"""Tests for slow-tick telemetry."""

from issue_orchestrator.control.tick_telemetry import report_slow_tick
from issue_orchestrator.domain.models import OrchestratorState
from issue_orchestrator.events import EventContext, EventName
from issue_orchestrator.ports import InMemoryEventSink


def _report(tick_elapsed: float, active_elapsed: float) -> InMemoryEventSink:
    events = InMemoryEventSink()
    report_slow_tick(events, EventContext(), OrchestratorState(), tick_elapsed, active_elapsed)
    return events


def test_no_event_for_fast_tick():
    assert not _report(tick_elapsed=2.0, active_elapsed=1.0).events


def test_emits_event_attributed_to_active_sessions_for_slow_publish():
    """A 153.9s tick spent almost entirely in active-session handling (the
    synchronous publish that froze the loop) is attributed to active_sessions."""
    events = _report(tick_elapsed=153.9, active_elapsed=153.0)
    slow = events.get_events(EventName.TICK_SLOW)
    assert len(slow) == 1
    payload = slow[0].data
    assert payload["duration_seconds"] == 153.9
    assert payload["dominant_phase"] == "active_sessions"


def test_attributes_to_planning_when_active_was_cheap():
    """A slow tick where active handling was cheap is attributed to planning."""
    events = _report(tick_elapsed=30.0, active_elapsed=1.0)
    payload = events.get_events(EventName.TICK_SLOW)[0].data
    assert payload["dominant_phase"] == "planning"
