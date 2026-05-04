"""Internal-timeline golden tests.

A "golden" pairs a scenario's input record stream with the expected
ordered output of `project_timeline()`. The fixture pins both:

  - input: list of `TimelineRecord` payloads to feed in
  - output: list of expected `TimelineEvent` field subsets, in order

The assertion logic is intentionally structural and order-sensitive:

  - Length mismatch fails first (cheap, decisive signal).
  - Each row is a subset match — only fields present in the fixture
    are checked against the projected event. This keeps fixtures
    focused on the contract that matters for that scenario without
    requiring exhaustive enumeration of every optional field.
  - Failures point at row index + field name + expected vs. actual.

This is the **internal** layer — it asserts on the output of
`project_timeline()` directly, with raw event names. The view-registry
fan-out + user-view filtering is asserted separately in the upcoming
external-golden test (PR 6).

To add a scenario:

  1. Drop a YAML file in `tests/fixtures/timeline/golden/*.yaml`
     with `records:` and `internal_timeline:` arrays.
  2. The parametrized test discovers it automatically.

To regenerate a fixture's `internal_timeline:` block from current
projection behavior:

  python tests/unit/test_golden_timeline_internal.py --regenerate <fixture>
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

from issue_orchestrator.ports.timeline_store import TimelineRecord
from issue_orchestrator.timeline import TimelineEvent, project_timeline

_GOLDEN_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "timeline" / "golden"


def _discover_fixtures() -> list[Path]:
    return sorted(_GOLDEN_DIR.glob("*.yaml"))


def _load_fixture(path: Path) -> dict[str, Any]:
    with path.open() as fp:
        return yaml.safe_load(fp)


def _build_records(records_data: list[dict[str, Any]]) -> list[TimelineRecord]:
    """Construct TimelineRecords from fixture entries.

    Event IDs are auto-generated as ascending sequence so order is
    explicit and visible. Source events default to the event name
    (matching how internal events flow when not fanned out).
    """
    return [
        TimelineRecord(
            event_id=f"e-{i:04d}",
            timestamp=entry["timestamp"],
            event=entry["event"],
            data=entry.get("data", {}),
            source_event=entry.get("source_event", entry["event"]),
        )
        for i, entry in enumerate(records_data)
    ]


def _assert_event_matches(
    index: int, actual: TimelineEvent, expected: dict[str, Any]
) -> None:
    """Assert each field in `expected` matches the corresponding field
    on `actual`. Other actual-only fields are ignored.
    """
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
    _discover_fixtures(),
    ids=lambda p: p.stem,
)
def test_golden_internal_timeline(fixture_path: Path) -> None:
    """Run a scenario fixture: project records, assert ordered match."""
    fixture = _load_fixture(fixture_path)
    issue_number = fixture["issue_number"]
    records = _build_records(fixture["records"])
    expected = fixture["internal_timeline"]

    actual = project_timeline(records, issue_number=issue_number)

    assert len(actual) == len(expected), (
        f"length mismatch: expected {len(expected)} events, "
        f"got {len(actual)}. Actual order: {[e.event for e in actual]!r}"
    )
    for i, (actual_event, expected_event) in enumerate(zip(actual, expected, strict=True)):
        _assert_event_matches(i, actual_event, expected_event)


def test_at_least_one_golden_fixture_exists() -> None:
    """Discovery sanity check — if zero fixtures exist the parametrized
    test silently passes, which would defeat the purpose."""
    fixtures = _discover_fixtures()
    assert fixtures, f"No golden fixtures found in {_GOLDEN_DIR}"


def _regenerate(fixture_path: Path) -> None:
    """Refresh a fixture's `internal_timeline:` block from current
    projection output. Use only when behavior change is intentional.
    """
    fixture = _load_fixture(fixture_path)
    records = _build_records(fixture["records"])
    actual = project_timeline(records, issue_number=fixture["issue_number"])

    # Rewrite only the keys that fixtures track today, preserving order.
    tracked_keys = ("event", "phase", "step", "status", "level", "parent_key")
    fixture["internal_timeline"] = [
        {k: e.to_dict()[k] for k in tracked_keys if k in e.to_dict()}
        for e in actual
    ]
    with fixture_path.open("w") as fp:
        yaml.safe_dump(fixture, fp, sort_keys=False)
    print(f"Regenerated {fixture_path} ({len(actual)} entries)")


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--regenerate":
        _regenerate(Path(sys.argv[2]))
    else:
        print("Usage: --regenerate <path-to-fixture.yaml>")
