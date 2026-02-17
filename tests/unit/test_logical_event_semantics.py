from issue_orchestrator.domain.logical_event_semantics import enrich_logical_semantics


def test_first_event_starts_run_and_cycle_one() -> None:
    out = enrich_logical_semantics(
        event_name="session.started",
        event_data={"task": "code"},
        previous_event_name=None,
        previous_data=None,
    )
    assert out.logical_run == 1
    assert out.logical_cycle == 1
    assert out.logical_phase == "coding"


def test_rework_cycle_signal_sets_logical_cycle() -> None:
    out = enrich_logical_semantics(
        event_name="rework.started",
        event_data={"task": "rework", "rework_cycle": 2},
        previous_event_name="review.changes_requested",
        previous_data={"logical_run": 1, "logical_cycle": 1},
    )
    assert out.logical_cycle == 3
    assert out.logical_phase == "rework"


def test_pr_pending_removed_starts_new_logical_run() -> None:
    out = enrich_logical_semantics(
        event_name="issue.labels_changed",
        event_data={"removed": ["pr-pending"]},
        previous_event_name="review.approved",
        previous_data={"logical_run": 2, "logical_cycle": 1},
    )
    assert out.logical_run == 3
    assert out.logical_cycle == 1
    assert out.logical_phase == "orchestrator"
