"""Tests for the declarative `EVENT_SPEC` projection table.

Three guarantees:

1. **`PublicEventName` matches `VIEW_REGISTRY`** — the enum is the
   codification of "events that surface in user or ops view." If a
   new event is added to the registry with `_user`/`_ops`, its name
   must also be in `PublicEventName` (or the test fails). This makes
   `VIEW_REGISTRY` the single source of truth and `PublicEventName`
   its typed projection.

2. **Exhaustiveness** — every `PublicEventName` value has a spec
   entry. Adding a public event without specifying its projection
   is a CI failure.

3. **Privacy** — `EVENT_SPEC` does not contain entries for any event
   that is not in `PublicEventName`. Private/debug events live
   outside the spec contract.
"""

from __future__ import annotations

import pytest

from issue_orchestrator.events.catalog import EventName, PublicEventName
from issue_orchestrator.events.spec import EVENT_SPEC, EventSpec, spec_for
from issue_orchestrator.events.view_registry import VIEW_REGISTRY


def _public_names_from_view_registry() -> set[str]:
    """Events that fan out to at least one user-or-ops ViewEvent."""
    public: set[str] = set()
    for internal_name, view_events in VIEW_REGISTRY.items():
        for ve in view_events:
            if "user" in ve.views or "ops" in ve.views:
                public.add(internal_name)
                break
    return public


def test_public_event_name_matches_view_registry() -> None:
    """`PublicEventName` enumerates exactly the user/ops events from
    `VIEW_REGISTRY`. Drift in either direction fails: adding a `_user`
    entry to the registry without updating the enum, or vice versa.
    """
    registry_public = _public_names_from_view_registry()
    enum_public = {e.value for e in PublicEventName}
    missing_in_enum = registry_public - enum_public
    extra_in_enum = enum_public - registry_public
    assert not missing_in_enum, (
        "Events surfaced in user/ops view but missing from PublicEventName: "
        f"{sorted(missing_in_enum)}"
    )
    assert not extra_in_enum, (
        "PublicEventName entries with no user/ops fan-out in VIEW_REGISTRY: "
        f"{sorted(extra_in_enum)}"
    )


def test_spec_covers_every_public_event_name() -> None:
    """Every `PublicEventName` must have an `EVENT_SPEC` entry."""
    enum_values = set(PublicEventName)
    spec_keys = set(EVENT_SPEC.keys())
    missing = enum_values - spec_keys
    extra = spec_keys - enum_values
    assert not missing, f"PublicEventName values without an EVENT_SPEC entry: {sorted(e.value for e in missing)}"
    assert not extra, f"EVENT_SPEC has entries outside PublicEventName: {sorted(e.value for e in extra)}"


def test_public_event_name_is_subset_of_event_name() -> None:
    """Every `PublicEventName` value matches an `EventName` string value.

    Without this guarantee, `spec_for(EventName.X)` could fail to
    find an entry for an event that is conceptually public.
    """
    event_name_values = {e.value for e in EventName}
    public_values = {e.value for e in PublicEventName}
    extras = public_values - event_name_values
    assert not extras, (
        "PublicEventName values not present in EventName: "
        f"{sorted(extras)}"
    )


def test_spec_for_accepts_enum_and_string() -> None:
    expected = EVENT_SPEC[PublicEventName.SESSION_STARTED]
    assert spec_for(PublicEventName.SESSION_STARTED) is expected
    assert spec_for(EventName.SESSION_STARTED) is expected
    assert spec_for("session.started") is expected


def test_spec_for_returns_none_for_private_event() -> None:
    """Private (debug-only) events return None — they have no
    user-facing projection contract."""
    assert spec_for(EventName.CLAIM_ACQUIRED) is None
    assert spec_for("claim.acquired") is None
    assert spec_for(EventName.TICK_STARTED) is None


def test_spec_for_returns_none_for_unknown_string() -> None:
    """Strings not matching any catalogued event return None."""
    assert spec_for("nonsense.event") is None


def test_event_spec_is_frozen() -> None:
    """EventSpec instances are immutable."""
    spec = EVENT_SPEC[PublicEventName.SESSION_STARTED]
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
