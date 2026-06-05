"""Unit tests for logical run projection abstraction."""

from __future__ import annotations

from issue_orchestrator.domain.logical_run_projection import (
    LogicalRunProjector,
    group_events_by_logical_cycle,
)


def _first_run(runs: list[dict[str, object]]) -> dict[str, object]:
    assert runs
    return runs[0]


def _first_cycle(run: dict[str, object]) -> dict[str, object]:
    cycles = run.get("cycles")
    assert isinstance(cycles, list) and cycles
    cycle = cycles[0]
    assert isinstance(cycle, dict)
    return cycle


def test_build_runs_groups_by_lifecycle_not_physical_run_id() -> None:
    projector = LogicalRunProjector()
    cycles = [
        {
            "cycle": 1,
            "lifecycle": 1,
            "run_id": "run-a",
            "session_run_ids": ["run-a"],
            "outcome": "Completed",
            "time_label": "t1",
        },
        {
            "cycle": 2,
            "lifecycle": 1,
            "run_id": "run-b",
            "session_run_ids": ["run-b"],
            "outcome": "Approved",
            "time_label": "t2",
        },
    ]

    runs = projector.build_runs(cycles)
    assert len(runs) == 1
    run = _first_run(runs)
    assert run["run_key"] == "lifecycle:1"
    assert run["session_run_ids"] == ["run-a", "run-b"]


def test_filter_last_run_cycles_prefers_latest_lifecycle() -> None:
    projector = LogicalRunProjector()
    cycles = [
        {"cycle": 1, "lifecycle": 2, "run_id": "run-old"},
        {"cycle": 2, "lifecycle": 3, "run_id": "run-older"},
        {"cycle": 3, "lifecycle": 3, "run_id": "run-newer"},
    ]

    latest = projector.filter_last_run_cycles(cycles)
    assert [c["cycle"] for c in latest] == [2, 3]


def test_annotate_cycle_in_run_is_logical_run_local() -> None:
    projector = LogicalRunProjector()
    cycles = [
        {"cycle": 1, "lifecycle": 1, "run_id": "run-1"},
        {"cycle": 2, "lifecycle": 1, "run_id": "run-2"},
        {"cycle": 3, "lifecycle": 2, "run_id": "run-3"},
    ]
    annotated = projector.annotate_cycle_in_run(cycles)
    assert [c["cycle_in_run"] for c in annotated] == [1, 2, 1]


def test_build_runs_marks_older_in_progress_as_superseded() -> None:
    projector = LogicalRunProjector()
    cycles = [
        {
            "cycle": 1,
            "lifecycle": 1,
            "run_id": "run-a",
            "session_run_ids": ["run-a"],
            "outcome": "In progress",
            "time_label": "t1",
        },
        {
            "cycle": 2,
            "lifecycle": 2,
            "run_id": "run-b",
            "session_run_ids": ["run-b"],
            "outcome": "Approved",
            "time_label": "t2",
        },
    ]

    runs = projector.build_runs(cycles)
    first_run = _first_run(runs)
    assert first_run["outcome"] == "Superseded"
    assert _first_cycle(first_run)["outcome"] == "Superseded"


def test_late_physical_rework_start_splits_occupied_logical_cycle() -> None:
    groups = group_events_by_logical_cycle(
        [
            {
                "event": "review.approved",
                "logical_run": 3,
                "logical_cycle": 2,
            },
            {
                "event": "agent.completed",
                "source_event": "session.completed",
                "logical_run": 3,
                "logical_cycle": 2,
            },
            {
                "event": "agent.rework_started",
                "source_event": "rework.started",
                "logical_run": 3,
                "logical_cycle": 2,
            },
        ]
    )

    assert [(group.logical_run, group.logical_cycle) for group in groups] == [
        (3, 2),
        (3, 3),
    ]
    assert [event["event"] for event in groups[0].events] == [
        "review.approved",
        "agent.completed",
    ]
    assert [event["event"] for event in groups[1].events] == [
        "agent.rework_started",
    ]
