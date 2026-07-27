"""Hold ``docs/api/ui-openapi-migration-plan.md`` to the live guardrail baseline.

The plan is the umbrella issue's (#6410) accounting mechanism for retiring the
``ui_openapi_routes:uncontracted:*`` baseline in reviewable groups. Prose counts
drift silently — the plan once described Group 2 as "the eight ``/api/session/*``
routes" when nine were outstanding, which made the documented totals disagree
with the baseline and put a route at risk of being stranded.

These tests parse the route tokens the plan enumerates and assert the partition
is exact in both directions, so a group can neither lose a route nor claim one
that another group already owns.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

PLAN_PATH = Path("docs/api/ui-openapi-migration-plan.md")
BASELINE_PATH = Path("quality/guardrails-baseline.json")

BASELINE_PREFIX = "ui_openapi_routes:uncontracted:"

# Routes are written as inline-code ``METHOD /api/path`` tokens. Requiring the
# method prefix keeps prose mentions of a bare path (``/api/dialog/*``,
# ``/api/issue-detail/{issue_number}``) out of the route sets.
ROUTE_TOKEN = re.compile(r"`((?:GET|POST|PUT|PATCH|DELETE) /api/[^`]*)`")

# | 2 | Session and log artifact reads | #6416 | 11 | pending |
TABLE_ROW = re.compile(
    r"^\|\s*(\d+)\s*\|\s*([^|]+?)\s*\|\s*[^|]*\|\s*(\d+)\s*\|\s*([^|]+?)\s*\|\s*$"
)

SECTION_HEADING = re.compile(r"^### Group (\d+)\b", re.MULTILINE)


def _plan_text() -> str:
    return PLAN_PATH.read_text()


def _baseline_routes() -> set[str]:
    metrics = json.loads(BASELINE_PATH.read_text())["metrics"]
    return {
        key[len(BASELINE_PREFIX) :]
        for key in metrics
        if key.startswith(BASELINE_PREFIX)
    }


def _table_rows() -> dict[int, tuple[int, str]]:
    """Group number → (declared route count, status text)."""
    rows: dict[int, tuple[int, str]] = {}
    for line in _plan_text().splitlines():
        match = TABLE_ROW.match(line)
        if match is None:
            continue
        rows[int(match.group(1))] = (int(match.group(3)), match.group(4))
    return rows


def _section_routes() -> dict[int, list[str]]:
    """Group number → routes enumerated in that group's ``### Group N`` section."""
    text = _plan_text()
    headings = list(SECTION_HEADING.finditer(text))
    assert headings, "plan has no '### Group N' sections to parse"

    sections: dict[int, list[str]] = {}
    for index, heading in enumerate(headings):
        end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
        body = text[heading.start() : end]
        sections[int(heading.group(1))] = ROUTE_TOKEN.findall(body)
    return sections


def _pending(status: str) -> bool:
    return "done" not in status.lower()


def test_every_group_in_the_table_has_a_section_and_vice_versa() -> None:
    assert set(_table_rows()) == set(_section_routes())


def test_group_route_counts_match_the_enumerated_routes() -> None:
    """The ``Routes`` column is the number a reader trusts; make it derived."""
    sections = _section_routes()

    for group, (declared, _) in _table_rows().items():
        assert declared == len(sections[group]), (
            f"Group {group} table count is {declared} but its section "
            f"enumerates {len(sections[group])} routes: {sections[group]}"
        )


def test_no_route_is_claimed_by_two_groups() -> None:
    counts = Counter(
        route for routes in _section_routes().values() for route in routes
    )
    duplicates = {route: n for route, n in counts.items() if n > 1}
    assert not duplicates, f"routes listed in more than one group: {duplicates}"


def test_pending_groups_partition_the_remaining_baseline() -> None:
    """Every outstanding baseline entry belongs to exactly one pending group,
    and no pending group lists a route the baseline no longer tracks."""
    rows = _table_rows()
    sections = _section_routes()

    pending = {
        route
        for group, routes in sections.items()
        if _pending(rows[group][1])
        for route in routes
    }
    baseline = _baseline_routes()

    assert baseline - pending == set(), (
        "baseline entries missing from every pending group: "
        f"{sorted(baseline - pending)}"
    )
    assert pending - baseline == set(), (
        "pending groups list routes that are not in the baseline: "
        f"{sorted(pending - baseline)}"
    )


def test_done_groups_are_removed_from_the_baseline() -> None:
    """A group marked **done** must have shed its baseline entries — the second
    half of "contracted"; leaving them behind means the migration only landed a
    schema entry."""
    rows = _table_rows()
    sections = _section_routes()
    baseline = _baseline_routes()

    for group, (_, status) in rows.items():
        if _pending(status):
            continue
        still_listed = sorted(set(sections[group]) & baseline)
        assert not still_listed, (
            f"Group {group} is marked done but these routes remain in the "
            f"baseline: {still_listed}"
        )


def test_pending_counts_sum_to_the_remaining_baseline_size() -> None:
    rows = _table_rows()
    pending_total = sum(
        count for count, status in rows.values() if _pending(status)
    )

    assert pending_total == len(_baseline_routes())
