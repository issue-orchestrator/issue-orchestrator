"""Unit tests for logical run projection abstraction."""

from __future__ import annotations

from issue_orchestrator.domain.logical_run_projection import LogicalRunProjector


def test_build_runs_groups_by_lifecycle_not_physical_run_id() -> None:
    projector = LogicalRunProjector()
    cycles = [
        {"cycle": 1, "lifecycle": 1, "run_id": "run-a", "session_run_ids": ["run-a"], "outcome": "Completed", "time_label": "t1"},
        {"cycle": 2, "lifecycle": 1, "run_id": "run-b", "session_run_ids": ["run-b"], "outcome": "Approved", "time_label": "t2"},
    ]

    runs = projector.build_runs(cycles)
    assert len(runs) == 1
    assert runs[0]["run_key"] == "lifecycle:1"
    assert runs[0]["session_run_ids"] == ["run-a", "run-b"]


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
        {"cycle": 1, "lifecycle": 1, "run_id": "run-a", "session_run_ids": ["run-a"], "outcome": "In progress", "time_label": "t1"},
        {"cycle": 2, "lifecycle": 2, "run_id": "run-b", "session_run_ids": ["run-b"], "outcome": "Approved", "time_label": "t2"},
    ]

    runs = projector.build_runs(cycles)
    assert runs[0]["outcome"] == "Superseded"
    assert runs[0]["cycles"][0]["outcome"] == "Superseded"
