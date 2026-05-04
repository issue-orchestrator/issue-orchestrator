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
       ├── apply view-registry fan-out (one internal → N external records)
       ├── enrich narratives (`_enrich_narrative` mirroring the writer)
       ├── project_timeline()
       ├── filter to `external_view` (default 'user')
       └── assert ordered match against `external_timeline:`

Bisection between the two layers:

  - internal pass + external pass  → projection + fan-out both correct
  - internal pass + external fail  → fan-out / view-registry / narrative bug
  - internal fail + external fail  → projection bug (cascades to external)
  - internal fail + external pass  → fixture authoring bug (shouldn't happen)

The recurring "events out of expected order" failure mode is now
asserted at *both* layers: the internal layer catches it at projection;
the external layer catches it at the user-facing rendering.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from issue_orchestrator.events.view_registry import fan_out
from issue_orchestrator.execution.timeline_writer import _enrich_narrative
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


def _build_internal_records(records_data: list[dict[str, Any]]) -> list[TimelineRecord]:
    return [
        TimelineRecord(
            event_id=f"i-{i:04d}",
            timestamp=entry["timestamp"],
            event=entry["event"],
            data=entry.get("data", {}),
            source_event=entry.get("source_event", entry["event"]),
        )
        for i, entry in enumerate(records_data)
    ]


def _apply_fan_out(records: list[TimelineRecord]) -> list[TimelineRecord]:
    """Mirror the production fan-out: one internal record → N external records.

    Replicates what `DefaultTimelineWriter.record()` does after
    semantic enrichment:
      - Look up `fan_out(internal_name)` to get external ViewEvents.
      - For each, copy the data, attach `views` and (optionally)
        override `logical_phase`, and apply narrative enrichment.

    Logical-semantics enrichment (logical_run / logical_cycle /
    restart_pending) is intentionally omitted — those fields depend on
    state-machine context the test fixture does not simulate, and
    coupling goldens to them would make fixtures brittle.
    """
    out: list[TimelineRecord] = []
    for i, record in enumerate(records):
        view_events = fan_out(record.event)
        for j, ve in enumerate(view_events):
            new_data = {**record.data, "views": sorted(ve.views)}
            if ve.narrative:
                new_data["narrative"] = _enrich_narrative(
                    ve.narrative, record.event, record.data
                )
            if ve.phase:
                new_data["logical_phase"] = ve.phase
            out.append(
                TimelineRecord(
                    event_id=f"e-{i:04d}-{j}",
                    timestamp=record.timestamp,
                    event=ve.name,
                    data=new_data,
                    source_event=record.event,
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
    internal_records = _build_internal_records(fixture["records"])
    fanned_records = _apply_fan_out(internal_records)
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
