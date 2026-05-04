"""Tests for `project_timeline()` — the canonical record→event projection.

These tests pin the contract:
  - Input order = output order
  - Pure and deterministic
  - Every record produces exactly one TimelineEvent
  - Spec is consulted for public events; legacy fallback for everything else

These are unit-layer assertions on the function in isolation. The
sequence-dependent and full-timeline-shape assertions live in the
golden tests added in the next PR — but those goldens target this same
function, so any regression localizes here first.
"""

from __future__ import annotations

import pytest

from issue_orchestrator.events.catalog import EventName, PublicEventName
from issue_orchestrator.events.spec import EVENT_SPEC
from issue_orchestrator.ports.timeline_store import TimelineRecord
from issue_orchestrator.timeline import TimelineEvent, project_timeline


def _record(event_name: str, **data: object) -> TimelineRecord:
    return TimelineRecord(
        event_id=f"e-{event_name}",
        timestamp="2026-05-03T00:00:00Z",
        event=event_name,
        data=dict(data),
    )


def test_project_timeline_preserves_record_order() -> None:
    """Output order matches input order — no sorting, no shuffling."""
    records = [
        _record(EventName.SESSION_STARTED.value, issue_number=42),
        _record(EventName.SESSION_COMPLETED.value, issue_number=42),
        _record(EventName.REVIEW_STARTED.value, issue_number=42),
        _record(EventName.REVIEW_APPROVED.value, issue_number=42),
    ]
    events = project_timeline(records, issue_number=42)
    assert [e.event for e in events] == [r.event for r in records]


def test_project_timeline_one_record_yields_one_event() -> None:
    """The projection is 1:1 — no fan-out, no filtering."""
    records = [_record(EventName.SESSION_STARTED.value, issue_number=1) for _ in range(5)]
    events = project_timeline(records, issue_number=1)
    assert len(events) == len(records)


def test_project_timeline_empty_input_yields_empty_output() -> None:
    assert project_timeline([], issue_number=1) == []


def test_project_timeline_is_deterministic() -> None:
    """Calling the projection twice with the same inputs produces equal outputs."""
    records = [
        _record(EventName.SESSION_STARTED.value, issue_number=7),
        _record(EventName.SESSION_COMPLETED.value, issue_number=7),
    ]
    a = project_timeline(records, issue_number=7)
    b = project_timeline(records, issue_number=7)
    assert a == b


def test_project_timeline_uses_spec_for_public_event() -> None:
    """A catalogued public event is projected per `EVENT_SPEC`."""
    spec = EVENT_SPEC[PublicEventName.REVIEW_APPROVED]
    [event] = project_timeline(
        [_record(EventName.REVIEW_APPROVED.value, issue_number=99)],
        issue_number=99,
    )
    assert event.phase == spec.phase
    assert event.step == spec.step
    assert event.status == spec.status
    assert event.level == spec.level


def test_project_timeline_returns_typed_events() -> None:
    """Every output is a `TimelineEvent` (not a dict)."""
    [event] = project_timeline(
        [_record(EventName.SESSION_STARTED.value, issue_number=1)],
        issue_number=1,
    )
    assert isinstance(event, TimelineEvent)


def test_project_timeline_for_e2e_run_uses_negative_issue_number() -> None:
    """Negative issue numbers are e2e runs; parent_key reflects this."""
    records = [_record("e2e.run_started", branch="main")]
    [event] = project_timeline(records, issue_number=-42)
    assert event.parent_key == "e2e-run-42"


def test_project_timeline_falls_back_for_private_event() -> None:
    """Private/debug events are projected via legacy fallback (no spec)."""
    [event] = project_timeline(
        [_record(EventName.CLAIM_ACQUIRED.value, issue_number=1)],
        issue_number=1,
    )
    # Behavior pinned by test_timeline_projection_pinning.py — here we
    # only assert the event still projects to *something* sensible.
    assert event.event == EventName.CLAIM_ACQUIRED.value
    assert event.phase  # non-empty
    assert event.step  # non-empty


def test_project_timeline_keyword_only_issue_number() -> None:
    """`issue_number` is keyword-only — protects against parameter swap."""
    with pytest.raises(TypeError):
        project_timeline([], 1)  # type: ignore[misc]
