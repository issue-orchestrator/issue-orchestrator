"""External-timeline golden tests.

Where `test_golden_timeline_internal.py` asserts on the output of
`project_timeline()` directly with raw internal event names, this
module asserts on the **post-fan-out user-facing timeline** — the
events with display names (`agent.coding_started`, not
`session.started`), narratives enriched with dynamic data, and
filtered to the requested view tier.

Two layers, one fixture: each YAML in `tests/fixtures/timeline/golden/`
optionally carries an `external_timeline:` block alongside its
`internal_timeline:`. When present, this test runs:

  internal records
      ─┐
       ├── apply view-registry fan-out via the canonical
       │   `produce_external_records()` (the same function the
       │   production `DefaultTimelineWriter` calls — no test-side
       │   re-implementation of writer policy)
       ├── project_timeline()
       ├── filter to `external_view` (default 'user')
       └── assert ordered match against `external_timeline:`

Bisection between the two layers:

  - internal pass + external pass  → projection + fan-out both correct
  - internal pass + external fail  → fan-out / view-registry / narrative bug
  - internal fail + external fail  → projection bug (cascades to external)
  - internal fail + external pass  → fixture authoring bug (shouldn't happen)

Routing through `produce_external_records()` ensures this harness
cannot drift from production policy — including subtle conditionals
like the rework-cycle phase-override guard in
`events/fan_out_pipeline.py`. See `test_fan_out_pipeline.py` for
focused unit coverage of that policy.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from issue_orchestrator.events.fan_out_pipeline import produce_external_records
from issue_orchestrator.ports.timeline_store import TimelineRecord
from issue_orchestrator.timeline import TimelineEvent, project_timeline

_GOLDEN_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "timeline" / "golden"


def _discover_fixtures_with_external() -> list[Path]:
    out: list[Path] = []
    for path in sorted(_GOLDEN_DIR.glob("*.yaml")):
        with path.open() as fp:
            fixture = yaml.safe_load(fp)
        if "external_timeline" in fixture:
            out.append(path)
    return out


def _apply_fan_out(records_data: list[dict[str, Any]]) -> list[TimelineRecord]:
    """Run each fixture record through the production fan-out function.

    Logical-semantics enrichment (logical_run / logical_cycle /
    restart_pending) is intentionally omitted — those fields depend on
    state-machine context that fixtures do not simulate. Tests that
    need to exercise enrichment-driven branches (rework-cycle phase
    promotion, restart boundaries) supply the relevant fields directly
    in the fixture's `data:` block; `produce_external_records()` then
    enforces the production policy on top.
    """
    out: list[TimelineRecord] = []
    for i, entry in enumerate(records_data):
        out.extend(
            produce_external_records(
                internal_event_name=entry["event"],
                enriched_data=entry.get("data", {}),
                base_event_id=f"i-{i:04d}",
                timestamp_iso=entry["timestamp"],
            )
        )
    return out


def _filter_to_view(events: list[TimelineEvent], view: str) -> list[TimelineEvent]:
    return [e for e in events if e.views and view in e.views]


def _assert_event_matches(
    index: int, actual: TimelineEvent, expected: dict[str, Any]
) -> None:
    actual_dict = actual.to_dict()
    for key, expected_value in expected.items():
        assert key in actual_dict, (
            f"row {index} ({actual.event}): expected field {key!r} "
            f"missing from projected event (got keys: {sorted(actual_dict)})"
        )
        actual_value = actual_dict[key]
        assert actual_value == expected_value, (
            f"row {index} ({actual.event}): field {key!r} "
            f"expected {expected_value!r}, got {actual_value!r}"
        )


@pytest.mark.parametrize(
    "fixture_path",
    _discover_fixtures_with_external(),
    ids=lambda p: p.stem,
)
def test_golden_external_timeline(fixture_path: Path) -> None:
    """Run a fixture's external block: fan-out + enrich + project + filter, then match."""
    with fixture_path.open() as fp:
        fixture = yaml.safe_load(fp)

    issue_number = fixture["issue_number"]
    view = fixture.get("external_view", "user")
    fanned_records = _apply_fan_out(fixture["records"])
    all_events = project_timeline(fanned_records, issue_number=issue_number)
    user_events = _filter_to_view(all_events, view)
    expected = fixture["external_timeline"]

    assert len(user_events) == len(expected), (
        f"length mismatch (view={view}): expected {len(expected)} events, "
        f"got {len(user_events)}. Actual order: {[e.event for e in user_events]!r}"
    )
    for i, (actual_event, expected_event) in enumerate(
        zip(user_events, expected, strict=True)
    ):
        _assert_event_matches(i, actual_event, expected_event)
