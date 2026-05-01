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


def test_initial_issue_unblocked_does_not_skip_run_one() -> None:
    out = enrich_logical_semantics(
        event_name="issue.unblocked",
        event_data={"from_scratch": True},
        previous_event_name=None,
        previous_data=None,
    )
    assert out.logical_run == 1
    assert out.logical_cycle == 1
    assert out.logical_phase == "orchestrator"


def test_rework_cycle_signal_sets_logical_cycle() -> None:
    out = enrich_logical_semantics(
        event_name="rework.started",
        event_data={"task": "rework", "rework_cycle": 2},
        previous_event_name="review.changes_requested",
        previous_data={"logical_run": 1, "logical_cycle": 1},
    )
    assert out.logical_cycle == 3
    assert out.logical_phase == "rework"


def test_rework_start_after_terminal_restart_keeps_rework_cycle_signal() -> None:
    out = enrich_logical_semantics(
        event_name="rework.started",
        event_data={"task": "rework", "rework_cycle": 1},
        previous_event_name="cleanup.completed",
        previous_data={
            "logical_run": 3,
            "logical_cycle": 1,
            "_logical_restart_pending": True,
        },
    )

    assert out.logical_run == 4
    assert out.logical_cycle == 2
    assert out.logical_phase == "rework"


def test_cached_rework_review_and_completion_stay_in_rework_cycle() -> None:
    rework_start = enrich_logical_semantics(
        event_name="rework.started",
        event_data={"task": "rework", "rework_cycle": 1},
        previous_event_name="cleanup.completed",
        previous_data={
            "logical_run": 3,
            "logical_cycle": 1,
            "_logical_restart_pending": True,
        },
    )
    review_approved = enrich_logical_semantics(
        event_name="review.approved",
        event_data={"task": "review", "cached": True},
        previous_event_name="review.started",
        previous_data={
            "logical_run": rework_start.logical_run,
            "logical_cycle": rework_start.logical_cycle,
            "_logical_rework_driven": rework_start.rework_driven,
        },
    )
    completed = enrich_logical_semantics(
        event_name="session.completed",
        event_data={"task": "rework", "rework_cycle": 1},
        previous_event_name="review.approved",
        previous_data={
            "logical_run": review_approved.logical_run,
            "logical_cycle": review_approved.logical_cycle,
            "_logical_rework_driven": review_approved.rework_driven,
        },
    )

    assert review_approved.logical_cycle == 2
    assert completed.logical_cycle == 2
    assert completed.logical_phase == "rework"


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


def test_session_failed_then_new_start_starts_new_logical_run() -> None:
    out = enrich_logical_semantics(
        event_name="session.started",
        event_data={"task": "code"},
        previous_event_name="session.failed",
        previous_data={"logical_run": 1, "logical_cycle": 1},
    )
    assert out.logical_run == 2
    assert out.logical_cycle == 1
    assert out.logical_phase == "coding"


def test_validation_retry_needed_increments_logical_cycle() -> None:
    out = enrich_logical_semantics(
        event_name="session.validation_retry_needed",
        event_data={},
        previous_event_name="review.approved",
        previous_data={"logical_run": 1, "logical_cycle": 1},
    )
    assert out.logical_run == 1, "validation retry stays in the same run"
    assert out.logical_cycle == 2, "validation retry starts a new cycle"


def test_validation_retry_cycle_carries_forward_to_next_session() -> None:
    retry = enrich_logical_semantics(
        event_name="session.validation_retry_needed",
        event_data={},
        previous_event_name="review.approved",
        previous_data={"logical_run": 1, "logical_cycle": 1},
    )
    next_start = enrich_logical_semantics(
        event_name="session.started",
        event_data={"task": "code"},
        previous_event_name="session.validation_retry_needed",
        previous_data={
            "logical_run": retry.logical_run,
            "logical_cycle": retry.logical_cycle,
            "_logical_restart_pending": retry.restart_pending,
        },
    )
    assert next_start.logical_run == 1, "still same run"
    assert next_start.logical_cycle == 2, "cycle incremented from retry"
    assert next_start.logical_phase == "coding"


def test_terminal_then_interstitial_event_then_start_starts_new_logical_run() -> None:
    interstitial = enrich_logical_semantics(
        event_name="validation.completed",
        event_data={},
        previous_event_name="session.failed",
        previous_data={"logical_run": 1, "logical_cycle": 1},
    )
    out = enrich_logical_semantics(
        event_name="session.started",
        event_data={"task": "code"},
        previous_event_name="validation.completed",
        previous_data={
            "logical_run": interstitial.logical_run,
            "logical_cycle": interstitial.logical_cycle,
            "_logical_restart_pending": interstitial.restart_pending,
        },
    )
    assert out.logical_run == 2
    assert out.logical_cycle == 1
    assert out.logical_phase == "coding"


def test_instance_id_change_starts_new_logical_run() -> None:
    """Orchestrator restart (different instance_id) triggers a new logical run."""
    out = enrich_logical_semantics(
        event_name="session.started",
        event_data={"task": "code"},
        previous_event_name="session.started",
        previous_data={"logical_run": 1, "logical_cycle": 1},
        current_instance_id="instance-B",
        previous_instance_id="instance-A",
    )
    assert out.logical_run == 2
    assert out.logical_cycle == 1


def test_same_instance_id_does_not_bump_run() -> None:
    """Same instance_id should not trigger a run boundary."""
    out = enrich_logical_semantics(
        event_name="validation.completed",
        event_data={},
        previous_event_name="session.started",
        previous_data={"logical_run": 1, "logical_cycle": 1},
        current_instance_id="instance-A",
        previous_instance_id="instance-A",
    )
    assert out.logical_run == 1


def test_empty_instance_ids_do_not_trigger_restart() -> None:
    """Missing instance IDs (legacy data) should not trigger restart."""
    out = enrich_logical_semantics(
        event_name="session.started",
        event_data={"task": "code"},
        previous_event_name="session.started",
        previous_data={"logical_run": 1, "logical_cycle": 1},
        current_instance_id="",
        previous_instance_id="",
    )
    assert out.logical_run == 1


def test_review_approved_does_not_use_cumulative_rounds_as_cycle_number() -> None:
    """`rounds=2` means two review rounds overall, not "this is cycle 2"."""
    out = enrich_logical_semantics(
        event_name="review.approved",
        event_data={"task": "review", "rounds": 2},
        previous_event_name="review.started",
        previous_data={"logical_run": 2, "logical_cycle": 3, "logical_phase": "review"},
    )
    assert out.logical_run == 2
    assert out.logical_cycle == 3
    assert out.logical_phase == "review"
