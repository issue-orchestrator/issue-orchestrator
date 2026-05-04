"""Pin current TimelineEvent projection behavior per EventName.

This test snapshots `_phase_for_event` / `_step_for_event` /
`_status_for_event` / `_level_for_event` for every value of `EventName`
against `tests/fixtures/timeline/projection_baseline.json`.

Purpose: when projection logic is refactored (e.g. consolidated behind a
declarative `EVENT_SPEC`), this test fails the moment any single mapping
drifts — pointing at the *exact* event whose phase/step/status/level
changed. Goldens at the timeline level can only say "the timeline is
wrong"; this test says "session.timeout used to map to status=failed and
now maps to status=completed."

When an intentional behavior change is made, regenerate the baseline:

    python -m tests.unit.test_timeline_projection_pinning --regenerate
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from issue_orchestrator.events.catalog import EventName
from issue_orchestrator.timeline import (
    _level_for_event,
    _phase_for_event,
    _status_for_event,
    _step_for_event,
)

_BASELINE_PATH = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "timeline"
    / "projection_baseline.json"
)


def _load_baseline() -> dict[str, dict[str, str]]:
    with _BASELINE_PATH.open() as fp:
        return json.load(fp)


def _project(event_name: str) -> dict[str, str]:
    return {
        "phase": _phase_for_event(event_name),
        "step": _step_for_event(event_name),
        "status": _status_for_event(event_name),
        "level": _level_for_event(event_name),
    }


@pytest.fixture(scope="module")
def baseline() -> dict[str, dict[str, str]]:
    return _load_baseline()


def test_baseline_covers_every_event_name(baseline: dict[str, dict[str, str]]) -> None:
    """Every EventName must have a baseline entry.

    If this fails because a new EventName was added, regenerate the
    baseline (see module docstring) — adding an event without a
    pinned projection means the timeline rendering for that event is
    untested.
    """
    catalog_names = {ev.value for ev in EventName}
    baseline_names = set(baseline.keys())
    missing_in_baseline = catalog_names - baseline_names
    extra_in_baseline = baseline_names - catalog_names
    assert not missing_in_baseline, (
        "EventName values without a pinned projection: "
        f"{sorted(missing_in_baseline)}"
    )
    assert not extra_in_baseline, (
        "Baseline has entries for unknown event names "
        f"(was the EventName removed?): {sorted(extra_in_baseline)}"
    )


@pytest.mark.parametrize("event", list(EventName), ids=lambda e: e.value)
def test_projection_matches_baseline(
    event: EventName, baseline: dict[str, dict[str, str]]
) -> None:
    """Per-event projection matches the pinned baseline.

    A failure here points at exactly one event whose phase, step,
    status, or level changed. The error message names the field.
    """
    expected = baseline[event.value]
    actual = _project(event.value)
    assert actual == expected, (
        f"projection drifted for {event.value!r}: "
        f"expected {expected}, got {actual}"
    )


if __name__ == "__main__":
    # Regeneration helper — invoked manually when projection behavior
    # is intentionally changed. Not part of the test run.
    import sys

    if "--regenerate" in sys.argv:
        snapshot = {ev.value: _project(ev.value) for ev in EventName}
        _BASELINE_PATH.write_text(json.dumps(snapshot, indent=2, sort_keys=True) + "\n")
        print(f"Regenerated {_BASELINE_PATH} ({len(snapshot)} entries)")
    else:
        print("Run with --regenerate to refresh the baseline fixture.")
