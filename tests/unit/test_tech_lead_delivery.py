"""Receipt aggregation and delivery policy: no action-event inference or clocks."""

from datetime import datetime, timedelta, timezone

import pytest

from issue_orchestrator.contracts.public import TechLeadActivityContract
from issue_orchestrator.contracts.ui_openapi_models import TechLeadActivityPayload
from issue_orchestrator.domain.tech_lead_delivery import (
    DeliveryHistoryState,
    TechLeadDeliveryPolicy,
    TechLeadDeliveryStatus,
)
from issue_orchestrator.domain.tech_lead_run import TechLeadRunScopeKind
from issue_orchestrator.domain.tech_lead_run_record import (
    TechLeadRunPhase,
    TechLeadRunRecord,
)
from issue_orchestrator.domain.tech_lead_session import TechLeadSessionFlavor
from issue_orchestrator.infra.sqlite_connection import open_sqlite
from issue_orchestrator.infra.tech_lead_run_record_store import (
    SqliteTechLeadRunRecordStore,
)
from issue_orchestrator.ports.tech_lead_run_record_store import (
    InMemoryTechLeadRunRecordStore,
)
from issue_orchestrator.view_models.tech_lead_activity import read_tech_lead_activity

NOW = datetime(2026, 9, 6, 12)


def record(index, hours_ago, phase=TechLeadRunPhase.FAILED, *, proposals=0):
    start = NOW - timedelta(hours=hours_ago)
    return TechLeadRunRecord(
        run_key="global:health_review",
        scope_kind=TechLeadRunScopeKind.GLOBAL_HEALTH_REVIEW,
        flavor=TechLeadSessionFlavor.HEALTH_REVIEW,
        run_id=f"r{index}",
        session_name=f"s{index}",
        started_at=start,
        ended_at=start if phase.is_terminal else None,
        phase=phase,
        proposals=proposals,
    )


@pytest.fixture(params=["memory", "sqlite"])
def store(request, tmp_path):
    if request.param == "sqlite":
        return SqliteTechLeadRunRecordStore(tmp_path / "runs.sqlite")
    return InMemoryTechLeadRunRecordStore()


def status(store, *, now=NOW):
    return TechLeadDeliveryPolicy().evaluate(store.inspect_delivery_evidence(), now=now)


@pytest.mark.parametrize(
    "starts,expected",
    [
        ([], "observing"),  # startup
        ([8], "observing"),  # single run
        ([8, 7], "observing"),  # insufficient runs
        ([5.99, 3, 0], "observing"),  # not yet six hours
        ([6, 3, 0], "stalled"),  # exact silence boundary
        ([12, 9, 6], "stalled"),  # exact recent-start boundary
        ([12, 9, 6.01], "observing"),  # idle
    ],
)
def test_only_repeated_actual_runs_with_sustained_silence_alarm(
    store, starts, expected
):
    for index, age in enumerate(starts):
        store.open_run(record(index, age))
    assert status(store).value == expected


@pytest.mark.parametrize(
    "phase,proposals",
    [
        (TechLeadRunPhase.COMPLETED, 0),  # valid no-op
        (TechLeadRunPhase.COMPLETED, 2),  # delivered gated proposals, still unapproved
        (TechLeadRunPhase.NEEDS_HUMAN, 0),  # legitimate escalation
    ],
)
def test_successful_post_apply_conclusions_reset_silence(store, phase, proposals):
    for index, age in enumerate([10, 8, 2]):
        store.open_run(record(index, age))
    assert status(store) is TechLeadDeliveryStatus.STALLED
    store.open_run(record(3, 1, phase, proposals=proposals))
    assert status(store) is TechLeadDeliveryStatus.OBSERVING
    evidence = store.inspect_delivery_evidence()
    assert evidence.last_delivered_at == NOW - timedelta(hours=1)
    assert evidence.runs_without_delivery == 0


def test_withdrawn_runs_do_not_create_or_extend_launch_pressure(store):
    for index, age in enumerate([12, 10, 8]):
        store.open_run(record(index, age))
    for index, age in enumerate([6, 3, 0], start=3):
        store.open_run(record(index, age, TechLeadRunPhase.WITHDRAWN))
    assert status(store) is TechLeadDeliveryStatus.OBSERVING
    assert store.inspect_delivery_evidence().runs_without_delivery == 3


def test_repeated_missing_conclusions_count_and_same_run_is_not_counted_twice(store):
    for index, age in enumerate([8, 4, 0]):
        row = record(index, age, TechLeadRunPhase.RUNNING)
        store.open_run(row)
        store.open_run(row)
    assert status(store) is TechLeadDeliveryStatus.STALLED
    assert store.inspect_delivery_evidence().runs_without_delivery == 3


def test_restart_and_display_limit_do_not_erase_the_silence_baseline(tmp_path):
    path = tmp_path / "runs.sqlite"
    store = SqliteTechLeadRunRecordStore(path)
    store.open_run(record(0, 10, TechLeadRunPhase.COMPLETED))
    for index in range(1, 32):
        store.open_run(record(index, 9 - index / 4))
    restarted = SqliteTechLeadRunRecordStore(path)
    view = read_tech_lead_activity(restarted, now=NOW)
    assert len(view.entries) == 20
    assert view.delivery.status is TechLeadDeliveryStatus.STALLED
    assert "31 runs" in view.delivery.message
    assert "6 hours" in view.delivery.message
    assert all(entry.phase == "failed" for entry in view.entries)
    payload = view.model_dump(mode="json", by_alias=True)
    TechLeadActivityContract.model_validate(payload)
    TechLeadActivityPayload.model_validate(payload)
    # A smaller presentation cannot change the assessment.
    assert (
        read_tech_lead_activity(restarted, limit=1, now=NOW).delivery == view.delivery
    )


def test_recent_success_outside_display_window_prevents_false_alarm(tmp_path):
    store = SqliteTechLeadRunRecordStore(tmp_path / "runs.sqlite")
    store.open_run(record(0, 20, TechLeadRunPhase.RUNNING))
    store.conclude_run(
        run_id="r0",
        session_name="s0",
        phase=TechLeadRunPhase.COMPLETED,
        ended_at=NOW - timedelta(minutes=1),
    )
    for index in range(1, 30):
        store.open_run(record(index, 10 - index / 4))
    view = read_tech_lead_activity(store, now=NOW)
    assert all(entry.phase == "failed" for entry in view.entries)
    assert view.delivery.status is TechLeadDeliveryStatus.OBSERVING


@pytest.mark.parametrize("history_complete,capacity", [(False, 200), (True, 2)])
def test_memory_loss_or_fallback_is_unknown_not_falsely_healthy(
    history_complete, capacity
):
    store = InMemoryTechLeadRunRecordStore(
        capacity=capacity, history_complete=history_complete
    )
    for index, age in enumerate([8, 4, 0]):
        store.open_run(record(index, age))
    view = read_tech_lead_activity(store, now=NOW)
    assert view.delivery.status is TechLeadDeliveryStatus.UNKNOWN
    assert "unavailable or incomplete" in view.delivery.message


def test_unreadable_database_and_failed_receipt_write_stay_unknown(tmp_path):
    path = tmp_path / "runs.sqlite"
    store = SqliteTechLeadRunRecordStore(path)
    with open_sqlite(path) as connection:
        connection.execute("ALTER TABLE tech_lead_run_records RENAME TO unavailable")
    assert (
        store.inspect_delivery_evidence().history_state
        is DeliveryHistoryState.UNAVAILABLE
    )
    store.open_run(record(1, 10))  # best effort write fails
    with open_sqlite(path) as connection:
        connection.execute("ALTER TABLE unavailable RENAME TO tech_lead_run_records")
    assert status(store) is TechLeadDeliveryStatus.UNKNOWN


@pytest.mark.parametrize(
    "column,value",
    [("started_at", "broken"), ("phase", "new_phase"), ("ended_at", "broken")],
)
def test_malformed_evidence_is_unknown_not_silently_dropped(tmp_path, column, value):
    path = tmp_path / "runs.sqlite"
    store = SqliteTechLeadRunRecordStore(path)
    store.open_run(record(0, 10))
    with open_sqlite(path) as connection:
        connection.execute(f"UPDATE tech_lead_run_records SET {column} = ?", (value,))
    assert status(store) is TechLeadDeliveryStatus.UNKNOWN


def test_injected_policy_and_offset_clock_are_shared_by_projection(store):
    for index, age in enumerate([2, 0]):
        store.open_run(record(index, age))
    policy = TechLeadDeliveryPolicy(silence=timedelta(hours=2), minimum_runs=2)
    view = read_tech_lead_activity(
        store, now=NOW.replace(tzinfo=timezone.utc), policy=policy
    )
    assert view.delivery.status is TechLeadDeliveryStatus.STALLED
    assert "2 hours" in view.delivery.message


@pytest.mark.parametrize("bad_end", ["broken", "12:00:00", "2026-09-06", " ", "now"])
def test_malformed_running_ending_is_not_an_absent_ending(tmp_path, bad_end):
    path = tmp_path / "runs.sqlite"
    store = SqliteTechLeadRunRecordStore(path)
    for index, age in enumerate((8, 4, 0)):
        store.open_run(record(index, age, TechLeadRunPhase.RUNNING))
    assert status(store) is TechLeadDeliveryStatus.STALLED
    with open_sqlite(path) as connection:
        connection.execute(
            "UPDATE tech_lead_run_records SET ended_at = ? WHERE run_id = 'r0'",
            (bad_end,),
        )
    # The display rejects this receipt; aggregation must not treat its invalid
    # ending as the empty ending on a legitimate still-running receipt.
    assert {row.run_id for row in store.recent(limit=20)} == {"r1", "r2"}
    restarted = SqliteTechLeadRunRecordStore(path)
    evidence = restarted.inspect_delivery_evidence()
    assert evidence.history_state is DeliveryHistoryState.INCOMPLETE
    assert status(restarted) is TechLeadDeliveryStatus.UNKNOWN


@pytest.mark.parametrize("column", ["started_at", "ended_at"])
@pytest.mark.parametrize(
    "bad_time",
    [
        "12:00:00",
        "2026-09-06",
        "2461290.0",
        "now",
        "2026-02-30T12:00:00",
        "2026-09-06T10:00:00+00:60",
        "2026-09-06T10:00:00+00:00:60",
    ],
)
def test_non_receipt_timestamp_cannot_reset_delivery_silence(
    tmp_path, column, bad_time
):
    path = tmp_path / "runs.sqlite"
    store = SqliteTechLeadRunRecordStore(path)
    for index, age in enumerate((8, 4, 0)):
        store.open_run(record(index, age))
    store.open_run(record(3, 1, TechLeadRunPhase.COMPLETED))
    assert status(store) is TechLeadDeliveryStatus.OBSERVING
    with open_sqlite(path) as connection:
        connection.execute(
            f"UPDATE tech_lead_run_records SET {column} = ? WHERE run_id = 'r3'",
            (bad_time,),
        )
    assert "r3" not in {row.run_id for row in store.recent(limit=20)}
    evidence = store.inspect_delivery_evidence()
    assert evidence.history_state is DeliveryHistoryState.INCOMPLETE
    assert evidence.last_delivered_at is None
    assert status(store) is TechLeadDeliveryStatus.UNKNOWN


@pytest.mark.parametrize(
    "start,end,expected",
    [
        ("2026-09-06T10:00:00", "2026-09-06T11:00:00", NOW - timedelta(hours=1)),
        ("2026-09-06 10:00:00", "2026-09-06 11:00:00", NOW - timedelta(hours=1)),
        (
            "2026-09-06T10:00:00.123456",
            "2026-09-06T11:00:00.500000",
            NOW - timedelta(hours=1) + timedelta(milliseconds=500),
        ),
        (
            "2026-09-06T12:00:00+02:00",
            "2026-09-06T07:00:00-04:00",
            NOW - timedelta(hours=1),
        ),
        ("2026-09-06T10:00:00Z", "2026-09-06T11:00:00+00:00", NOW - timedelta(hours=1)),
    ],
)
def test_receipt_display_and_aggregate_accept_valid_naive_and_offset_times(
    tmp_path, start, end, expected
):
    path = tmp_path / "runs.sqlite"
    store = SqliteTechLeadRunRecordStore(path)
    store.open_run(record(0, 2, TechLeadRunPhase.COMPLETED))
    store.open_run(record(1, 0, TechLeadRunPhase.RUNNING))  # empty ending is valid
    with open_sqlite(path) as connection:
        connection.execute(
            "UPDATE tech_lead_run_records SET started_at = ?, ended_at = ? WHERE run_id = 'r0'",
            (start, end),
        )
    by_id = {row.run_id: row for row in store.recent(limit=20)}
    assert by_id["r0"].started_at == datetime.fromisoformat(start)
    assert by_id["r0"].ended_at == datetime.fromisoformat(end)
    assert by_id["r1"].ended_at is None
    evidence = store.inspect_delivery_evidence()
    assert evidence.history_state is DeliveryHistoryState.COMPLETE
    assert evidence.last_delivered_at == expected
    assert evidence.runs_without_delivery == 1
    assert status(store) is TechLeadDeliveryStatus.OBSERVING
