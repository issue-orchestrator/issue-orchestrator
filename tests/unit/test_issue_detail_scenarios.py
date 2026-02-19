"""Scenario-driven regression tests for issue detail journey cycle logic."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from issue_orchestrator.view_models.issue_detail import (
    _build_journey_cycles,
    filter_last_run_cycles,
)


def _evt(event: str, **kw: object) -> dict[str, object]:
    base: dict[str, object] = {
        "event": event,
        "timestamp": "2026-02-09T20:15:00Z",
        "status": "started",
        "step": event.split(".")[-1] if "." in event else event,
        "phase": "in_progress",
    }
    base.update(kw)
    return base


@dataclass(frozen=True)
class ExpectedCycle:
    lifecycle: int | None
    iteration: int
    retry_count: int
    outcome_contains: str


@dataclass(frozen=True)
class JourneyScenario:
    name: str
    today: str
    events: list[dict[str, object]]
    expected_cycles: list[ExpectedCycle]
    expected_last_run_lifecycle: int | None
    expected_last_run_count: int


SCENARIOS: list[JourneyScenario] = [
    JourneyScenario(
        name="split_on_run_id_without_terminal_events",
        today="2026-02-09",
        events=[
            _evt("session.started", timestamp="2026-02-09T10:00:00Z", rework_cycle=0, run_id="run-1"),
            _evt("session.completed", timestamp="2026-02-09T10:30:00Z", rework_cycle=0, run_id="run-1"),
            _evt("session.started", timestamp="2026-02-09T11:00:00Z", rework_cycle=0, run_id="run-2"),
            _evt("session.failed", timestamp="2026-02-09T11:10:00Z", rework_cycle=0, run_id="run-2"),
            _evt("session.started", timestamp="2026-02-09T11:15:00Z", rework_cycle=0, run_id="run-2"),
            _evt("session.completed", timestamp="2026-02-09T11:35:00Z", rework_cycle=0, run_id="run-2"),
        ],
        expected_cycles=[
            ExpectedCycle(lifecycle=1, iteration=1, retry_count=0, outcome_contains="Ready for review"),
            ExpectedCycle(lifecycle=2, iteration=1, retry_count=1, outcome_contains="Ready for review"),
        ],
        expected_last_run_lifecycle=2,
        expected_last_run_count=1,
    ),
    JourneyScenario(
        name="split_on_run_dir_when_run_id_missing",
        today="2026-02-09",
        events=[
            _evt(
                "session.started",
                timestamp="2026-02-09T10:00:00Z",
                rework_cycle=0,
                run_dir="/tmp/repo/.issue-orchestrator/sessions/20260218-100000Z__coding-1",
            ),
            _evt(
                "session.completed",
                timestamp="2026-02-09T10:20:00Z",
                rework_cycle=0,
                run_dir="/tmp/repo/.issue-orchestrator/sessions/20260218-100000Z__coding-1",
            ),
            _evt(
                "session.started",
                timestamp="2026-02-09T10:30:00Z",
                rework_cycle=0,
                run_dir="/tmp/repo/.issue-orchestrator/sessions/20260218-103000Z__coding-1",
            ),
            _evt(
                "session.completed",
                timestamp="2026-02-09T10:55:00Z",
                rework_cycle=0,
                run_dir="/tmp/repo/.issue-orchestrator/sessions/20260218-103000Z__coding-1",
            ),
        ],
        expected_cycles=[
            ExpectedCycle(lifecycle=1, iteration=1, retry_count=0, outcome_contains="Ready for review"),
            ExpectedCycle(lifecycle=2, iteration=1, retry_count=0, outcome_contains="Ready for review"),
        ],
        expected_last_run_lifecycle=2,
        expected_last_run_count=1,
    ),
    JourneyScenario(
        name="split_on_run_dir_even_when_run_id_stays_constant",
        today="2026-02-09",
        events=[
            _evt(
                "session.started",
                timestamp="2026-02-09T10:00:00Z",
                rework_cycle=0,
                run_id="orchestrator-process-run",
                run_dir="/tmp/repo/.issue-orchestrator/sessions/20260218-100000Z__coding-1",
            ),
            _evt(
                "session.completed",
                timestamp="2026-02-09T10:20:00Z",
                rework_cycle=0,
                run_id="orchestrator-process-run",
                run_dir="/tmp/repo/.issue-orchestrator/sessions/20260218-100000Z__coding-1",
            ),
            _evt(
                "session.started",
                timestamp="2026-02-09T10:30:00Z",
                rework_cycle=0,
                run_id="orchestrator-process-run",
                run_dir="/tmp/repo/.issue-orchestrator/sessions/20260218-103000Z__coding-1",
            ),
            _evt(
                "session.completed",
                timestamp="2026-02-09T10:55:00Z",
                rework_cycle=0,
                run_id="orchestrator-process-run",
                run_dir="/tmp/repo/.issue-orchestrator/sessions/20260218-103000Z__coding-1",
            ),
        ],
        expected_cycles=[
            ExpectedCycle(lifecycle=1, iteration=1, retry_count=0, outcome_contains="Ready for review"),
            ExpectedCycle(lifecycle=2, iteration=1, retry_count=0, outcome_contains="Ready for review"),
        ],
        expected_last_run_lifecycle=2,
        expected_last_run_count=1,
    ),
    JourneyScenario(
        name="review_outcome_tracks_per_run_without_merging",
        today="2026-02-09",
        events=[
            _evt("session.started", timestamp="2026-02-09T09:00:00Z", rework_cycle=0, run_id="run-a"),
            _evt("session.completed", timestamp="2026-02-09T09:25:00Z", rework_cycle=0, run_id="run-a"),
            _evt("review.changes_requested", timestamp="2026-02-09T09:28:00Z", rework_cycle=0, run_id="run-a"),
            _evt("session.started", timestamp="2026-02-09T10:00:00Z", rework_cycle=0, run_id="run-b"),
            _evt("session.completed", timestamp="2026-02-09T10:24:00Z", rework_cycle=0, run_id="run-b"),
            _evt("review.approved", timestamp="2026-02-09T10:27:00Z", rework_cycle=0, run_id="run-b"),
        ],
        expected_cycles=[
            ExpectedCycle(lifecycle=1, iteration=1, retry_count=0, outcome_contains="Changes Requested"),
            ExpectedCycle(lifecycle=2, iteration=1, retry_count=0, outcome_contains="Approved"),
        ],
        expected_last_run_lifecycle=2,
        expected_last_run_count=1,
    ),
    JourneyScenario(
        name="mixed_legacy_signal_and_run_boundaries_remain_isolated",
        today="2026-02-10",
        events=[
            # Legacy era (no rework_cycle), old run
            _evt("session.started", timestamp="2026-02-08T09:00:00Z", run_id="legacy-run"),
            _evt("session.completed", timestamp="2026-02-08T09:20:00Z", run_id="legacy-run"),
            _evt("issue.blocked", timestamp="2026-02-08T09:25:00Z", run_id="legacy-run"),
            # Signal era run #1
            _evt("session.started", timestamp="2026-02-09T10:00:00Z", rework_cycle=0, run_id="signal-run-1"),
            _evt("session.completed", timestamp="2026-02-09T10:25:00Z", rework_cycle=0, run_id="signal-run-1"),
            _evt("review.changes_requested", timestamp="2026-02-09T10:28:00Z", rework_cycle=0, run_id="signal-run-1"),
            _evt("rework.started", timestamp="2026-02-09T10:35:00Z", rework_cycle=1, run_id="signal-run-1"),
            _evt("session.completed", timestamp="2026-02-09T10:58:00Z", rework_cycle=1, run_id="signal-run-1"),
            # Signal era run #2 (same rework indexes, new run)
            _evt("session.started", timestamp="2026-02-10T11:00:00Z", rework_cycle=0, run_id="signal-run-2"),
            _evt("session.completed", timestamp="2026-02-10T11:20:00Z", rework_cycle=0, run_id="signal-run-2"),
            _evt("review.approved", timestamp="2026-02-10T11:23:00Z", rework_cycle=0, run_id="signal-run-2"),
        ],
        expected_cycles=[
            ExpectedCycle(lifecycle=None, iteration=0, retry_count=0, outcome_contains="Blocked"),
            ExpectedCycle(lifecycle=None, iteration=1, retry_count=0, outcome_contains="Changes Requested"),
            ExpectedCycle(lifecycle=None, iteration=2, retry_count=0, outcome_contains="Ready for review"),
            ExpectedCycle(lifecycle=None, iteration=1, retry_count=0, outcome_contains="Approved"),
        ],
        expected_last_run_lifecycle=None,
        expected_last_run_count=1,
    ),
]


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.name)
def test_journey_cycle_scenarios(scenario: JourneyScenario) -> None:
    cycles = _build_journey_cycles(scenario.events, scenario.today)
    assert len(cycles) == len(scenario.expected_cycles)

    for actual, expected in zip(cycles, scenario.expected_cycles):
        if expected.lifecycle is not None:
            assert actual["lifecycle"] == expected.lifecycle
        assert actual["iteration"] == expected.iteration
        assert actual["retry_count"] == expected.retry_count
        assert expected.outcome_contains in str(actual["outcome"])

    filtered = filter_last_run_cycles(cycles)
    assert len(filtered) == scenario.expected_last_run_count
    expected_last_lifecycle = (
        scenario.expected_last_run_lifecycle
        if scenario.expected_last_run_lifecycle is not None
        else max(c["lifecycle"] for c in cycles)
    )
    assert all(c["lifecycle"] == expected_last_lifecycle for c in filtered)
