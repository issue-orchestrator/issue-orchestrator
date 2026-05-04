"""Tests for the declarative `EVENT_SPEC` projection table.

Two guarantees:

1. **Exhaustiveness** — every `EventName` has a spec entry. New events
   cannot be added without specifying their projection.

2. **Parity with current projection helpers** — for every `EventName`,
   `EVENT_SPEC` produces the same `(phase, step, status, level)` that
   the legacy `_phase_for_event` / `_step_for_event` /
   `_status_for_event` / `_level_for_event` helpers produce today.

When the helpers are cut over to consult `EVENT_SPEC` (next PR), parity
is maintained tautologically. Until then, this test is what proves the
spec has not silently drifted from current behavior.
"""

from __future__ import annotations

import pytest

from issue_orchestrator.events.catalog import EventName
from issue_orchestrator.events.spec import EVENT_SPEC, EventSpec, spec_for
from issue_orchestrator.timeline import (
    _level_for_event,
    _phase_for_event,
    _status_for_event,
    _step_for_event,
)


def test_spec_covers_every_event_name() -> None:
    """Every EventName must appear in EVENT_SPEC."""
    catalog_names = set(EventName)
    spec_names = set(EVENT_SPEC.keys())
    missing = catalog_names - spec_names
    extra = spec_names - catalog_names
    assert not missing, f"EventName values without an EVENT_SPEC entry: {sorted(e.value for e in missing)}"
    assert not extra, f"EVENT_SPEC has entries for unknown EventName values: {sorted(e.value for e in extra)}"


@pytest.mark.parametrize("event", list(EventName), ids=lambda e: e.value)
def test_spec_matches_legacy_projection(event: EventName) -> None:
    """EVENT_SPEC encodes the same answers as the legacy helpers.

    A failure here means EVENT_SPEC has drifted from the projection
    helpers in `timeline.py` — fix one or the other before cutover.
    """
    spec = EVENT_SPEC[event]
    name = event.value
    assert spec.phase == _phase_for_event(name), f"phase drift for {name}"
    assert spec.step == _step_for_event(name), f"step drift for {name}"
    assert spec.status == _status_for_event(name), f"status drift for {name}"
    assert spec.level == _level_for_event(name), f"level drift for {name}"


def test_spec_for_accepts_enum_and_string() -> None:
    expected = EVENT_SPEC[EventName.SESSION_STARTED]
    assert spec_for(EventName.SESSION_STARTED) is expected
    assert spec_for("session.started") is expected


def test_spec_for_returns_none_for_unknown_string() -> None:
    """Unknown event names return None rather than raising — callers can
    decide whether to fall back to legacy projection during migration."""
    assert spec_for("nonsense.event") is None


def test_event_spec_is_frozen() -> None:
    """EventSpec instances are immutable."""
    spec = EVENT_SPEC[EventName.SESSION_STARTED]
    with pytest.raises(AttributeError):
        spec.phase = "other"  # type: ignore[misc]


def test_event_spec_field_types() -> None:
    """Every spec entry has non-empty string fields."""
    for event, spec in EVENT_SPEC.items():
        assert isinstance(spec, EventSpec), f"{event.value} value is not an EventSpec"
        assert spec.phase, f"{event.value} has empty phase"
        assert spec.step, f"{event.value} has empty step"
        assert spec.status, f"{event.value} has empty status"
        assert spec.level, f"{event.value} has empty level"
