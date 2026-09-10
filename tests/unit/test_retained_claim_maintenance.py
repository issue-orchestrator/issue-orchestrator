"""Quiescent release of publication claims retained after record work ends."""

from __future__ import annotations

from contextlib import closing
from dataclasses import replace
import sqlite3
from threading import Event
from types import SimpleNamespace
from typing import cast
from unittest.mock import Mock

import pytest

from issue_orchestrator.control.retained_claim_maintenance import (
    RetainedClaimMaintenance,
)
from issue_orchestrator.domain.recovery_drain import RecoveryDrainMode
from issue_orchestrator.domain.retained_claim_maintenance import (
    RetainedClaimMaintenanceReport,
    RetainedClaimMaintenanceStatus as Status,
)
from issue_orchestrator.domain.validated_work_execution import RecordExecutionBusy
from issue_orchestrator.domain.validated_work import (
    ValidatedWorkFailure as Failure,
    ValidatedWorkState as State,
)
from issue_orchestrator.execution.validated_work_execution import (
    LocalValidatedWorkExecutionOwner,
)
from issue_orchestrator.ports.validated_work_execution import (
    ValidatedWorkExecutionOwner,
)
from issue_orchestrator.ports.retained_claim_maintenance import (
    RetainedClaimMaintenanceStore,
)
from issue_orchestrator.ports.validated_work_store import ValidatedWorkStore
from tests.unit.threading_helpers import join_or_fail, run_in_thread, wait_for_event
from tests.unit.validated_work_support import (
    LATER,
    OWNER,
    OTHER,
    Liveness,
    Rig,
    begin,
    capture,
    claim,
    finalize,
)


ACTIVE = lambda: RecoveryDrainMode.ACTIVE
MAINTENANCE_CASES: tuple[State, ...] = (
    State.ABANDONED,
    State.FAILED,
    State.PARKED,
    State.RECOVERED,
)


def _record(path, record_id):
    with closing(sqlite3.connect(path)) as conn:
        conn.row_factory = sqlite3.Row
        return dict(
            conn.execute(
                "SELECT * FROM validated_work_records WHERE record_id=?",
                (record_id,),
            ).fetchone()
        )


def _evidence(path, record_id):
    with closing(sqlite3.connect(path)) as conn:
        conn.row_factory = sqlite3.Row
        return tuple(
            dict(row)
            for row in conn.execute(
                "SELECT * FROM validated_work_evidence WHERE record_id=? "
                "ORDER BY evidence_id",
                (record_id,),
            )
        )


def _set_state(path, state, *, reserve_for=None):
    with closing(sqlite3.connect(path)) as conn, conn:
        values = [state.value]
        sql = "UPDATE validated_work_records SET state=?"
        if reserve_for is not None:
            sql += ",stop_reserved_fence=?,stop_reserved_engine=?,stop_reservation_id='stop-id',stop_reserved_at='reserved'"
            values.extend((reserve_for.fence, reserve_for.owner.instance_id))
        conn.execute(sql, values)


def _retained_in_state(rig, state):
    store = rig.open()
    if state is State.RECOVERED:
        admitted = capture()
        store.admit(admitted)
        owned = claim(store, admitted)
        attempt = begin(store, owned)
        assert attempt is not None
        finalize(store, owned, attempt)
        return store, admitted, owned
    admitted = (
        capture(state=State.FAILED, failure=Failure.ARTIFACT_MISSING)
        if state is State.FAILED
        else capture(state=State.PARKED)
    )
    store.admit(admitted)
    owned = claim(store, admitted)
    if state is State.ABANDONED:
        with closing(sqlite3.connect(rig.path)) as conn, conn:
            conn.execute(
                "UPDATE validated_work_records SET state='abandoned',failure='',"
                "resolution_kind='operator_abandoned',resolved_by='operator',"
                "resolution_reason='accepted loss',resolved_at=?,terminal_at=?,"
                "abandon_authority_json='{}'",
                (LATER, LATER),
            )
    return store, admitted, owned


@pytest.mark.parametrize("state", MAINTENANCE_CASES)
@pytest.mark.parametrize("reserved", [False, True])
def test_dead_retained_claim_is_released_without_changing_work(
    tmp_path, state, reserved
):
    rig = Rig(tmp_path / f"{state.value}-{reserved}.sqlite")
    original, _, old_claim = _retained_in_state(rig, state)
    _set_state(
        rig.path,
        state,
        reserve_for=old_claim if reserved else None,
    )
    before_record = _record(rig.path, old_claim.record_id)
    before_evidence = _evidence(rig.path, old_claim.record_id)
    before_attempts = original.publish_attempts(old_claim.record_id)
    liveness = Liveness(OTHER, {OWNER})
    successor = rig.open(liveness)
    execution = LocalValidatedWorkExecutionOwner(successor)

    report = RetainedClaimMaintenance(
        store=successor, execution=execution
    ).reconcile(ACTIVE)

    assert [item.status for item in report.items] == [Status.RELEASED]
    assert liveness.checked == [OWNER]
    assert successor.owner_of(old_claim.record_id) is None
    after_record = _record(rig.path, old_claim.record_id)
    after_evidence = _evidence(rig.path, old_claim.record_id)
    changed = {
        key for key in before_record if before_record[key] != after_record[key]
    }
    expected = {
        "owner_fence",
        "owner_host",
        "owner_pid",
        "owner_started_at",
        "owner_claim_hash",
        "owner_instance_id",
        "owner_claimed_at",
    }
    if reserved:
        expected |= {
            "stop_reserved_fence",
            "stop_reserved_engine",
            "stop_reservation_id",
            "stop_reserved_at",
        }
    assert changed == expected
    assert after_evidence == before_evidence
    assert successor.publish_attempts(old_claim.record_id) == before_attempts


def test_cached_local_claim_retries_release_without_liveness_takeover(tmp_path):
    liveness = Liveness()
    store = Rig(tmp_path / "work.sqlite", liveness=liveness).open()
    admitted = capture(state=State.PARKED)
    store.admit(admitted)
    execution = LocalValidatedWorkExecutionOwner(store)
    lease = execution.try_enter(admitted.evidence.record_id)
    assert not isinstance(lease, RecordExecutionBusy)
    with lease as token:
        owned = store.acquire_claim(
            admitted.evidence.record_id,
            expected_states=frozenset({State.PARKED}),
            evidence_id=admitted.evidence.evidence_id,
        )
        assert owned is not None
        execution.remember_claim(token, owned)

    report = RetainedClaimMaintenance(
        store=store, execution=execution
    ).reconcile(ACTIVE)

    assert report.items[0].status is Status.RELEASED
    assert liveness.checked == []
    assert store.owner_of(admitted.evidence.record_id) is None


def test_stop_reserved_cached_claim_stays_private_until_release_succeeds(tmp_path):
    rig = Rig(tmp_path / "work.sqlite")
    store = rig.open()
    admitted = capture(state=State.PARKED)
    store.admit(admitted)
    execution = LocalValidatedWorkExecutionOwner(store)
    lease = execution.try_enter(admitted.evidence.record_id)
    assert not isinstance(lease, RecordExecutionBusy)
    with lease as token:
        owned = store.acquire_claim(
            admitted.evidence.record_id,
            expected_states=frozenset({State.PARKED}),
            evidence_id=admitted.evidence.evidence_id,
        )
        assert owned is not None
        execution.remember_claim(token, owned)
    _set_state(rig.path, State.PARKED, reserve_for=owned)
    maintenance = RetainedClaimMaintenance(store=store, execution=execution)

    first = maintenance.reconcile(ACTIVE)

    assert first.items[0].status is Status.RELEASE_DEFERRED
    assert store.holds_claim(owned)
    with closing(sqlite3.connect(rig.path)) as conn, conn:
        conn.execute(
            "UPDATE validated_work_records SET stop_reserved_fence=-1,"
            "stop_reserved_engine='',stop_reservation_id='',stop_reserved_at=''"
        )

    second = maintenance.reconcile(ACTIVE)

    assert second.items[0].status is Status.RELEASED
    assert store.owner_of(owned.record_id) is None


def test_reservation_racing_takeover_retains_new_claim_for_next_pass(tmp_path):
    rig = Rig(tmp_path / "work.sqlite")
    original = rig.open()
    admitted = capture(state=State.PARKED)
    original.admit(admitted)
    old = claim(original, admitted)
    successor = rig.open(Liveness(OTHER, {OWNER}))

    class RacingStore:
        def __init__(self):
            self.raced = False

        def __getattr__(self, name):
            return getattr(successor, name)

        def relinquish_claim(self, owned):
            if not self.raced:
                self.raced = True
                _set_state(rig.path, State.PARKED, reserve_for=owned)
            return successor.relinquish_claim(owned)

    racing = RacingStore()
    execution = LocalValidatedWorkExecutionOwner(
        cast(ValidatedWorkStore, racing)
    )
    maintenance = RetainedClaimMaintenance(
        store=cast(RetainedClaimMaintenanceStore, racing), execution=execution
    )

    first = maintenance.reconcile(ACTIVE)

    assert first.items[0].status is Status.RELEASE_DEFERRED
    current = successor.owner_of(old.record_id)
    assert current == OTHER
    with closing(sqlite3.connect(rig.path)) as conn, conn:
        conn.execute(
            "UPDATE validated_work_records SET stop_reserved_fence=-1,"
            "stop_reserved_engine='',stop_reservation_id='',stop_reserved_at=''"
        )

    second = maintenance.reconcile(ACTIVE)

    assert second.items[0].status is Status.RELEASED
    assert successor.owner_of(old.record_id) is None


@pytest.mark.parametrize(
    "successor",
    [
        Liveness(OTHER),
        Liveness(replace(OTHER, host="remote"), {OWNER}),
    ],
)
def test_live_or_remote_owner_is_never_displaced(tmp_path, successor):
    rig = Rig(tmp_path / "work.sqlite")
    original = rig.open()
    admitted = capture(state=State.PARKED)
    original.admit(admitted)
    owned = claim(original, admitted)
    before = _record(rig.path, owned.record_id)
    store = rig.open(successor)

    report = RetainedClaimMaintenance(
        store=store,
        execution=LocalValidatedWorkExecutionOwner(store),
    ).reconcile(ACTIVE)

    assert report.items[0].status is Status.OWNER_UNAVAILABLE
    assert _record(rig.path, owned.record_id) == before


def test_active_record_operation_keeps_maintenance_out(tmp_path):
    store = Rig(tmp_path / "work.sqlite").open()
    admitted = capture(state=State.PARKED)
    store.admit(admitted)
    execution = LocalValidatedWorkExecutionOwner(store)
    entered, finish = Event(), Event()

    def active_operation():
        lease = execution.try_enter(admitted.evidence.record_id)
        assert not isinstance(lease, RecordExecutionBusy)
        with lease as token:
            owned = store.acquire_claim(
                admitted.evidence.record_id,
                expected_states=frozenset({State.PARKED}),
                evidence_id=admitted.evidence.evidence_id,
            )
            assert owned is not None
            execution.remember_claim(token, owned)
            entered.set()
            wait_for_event(finish, 10)
            assert execution.relinquish(token)

    thread, result = run_in_thread(active_operation)
    wait_for_event(entered, 5)
    try:
        report = RetainedClaimMaintenance(
            store=store, execution=execution
        ).reconcile(ACTIVE)
        assert report.items[0].status is Status.BUSY
        assert store.owner_of(admitted.evidence.record_id) is not None
    finally:
        finish.set()
        join_or_fail(thread, 5)
    assert result.error is None


def test_changed_candidate_is_skipped_before_claim_acquisition():
    candidate = Mock()
    candidate.record_id = "record"
    candidate.evidence_id = "evidence"
    candidate.state = State.PARKED
    store = Mock()
    store.retained_claims.return_value = (candidate,)
    store.retained_claim.return_value = None
    execution = LocalValidatedWorkExecutionOwner(store)

    report = RetainedClaimMaintenance(
        store=store, execution=execution
    ).reconcile(ACTIVE)

    assert report.items[0].status is Status.CHANGED
    store.acquire_claim.assert_not_called()
    store.relinquish_claim.assert_not_called()


def test_lifecycle_stop_between_candidates_preserves_unattempted_claim(tmp_path):
    rig = Rig(tmp_path / "work.sqlite")
    original = rig.open()
    admitted = [capture(issue=issue, state=State.PARKED) for issue in (1, 2)]
    old_claims = []
    for item in admitted:
        original.admit(item)
        old_claims.append(claim(original, item))
    successor = rig.open(Liveness(OTHER, {OWNER}))
    mode = SimpleNamespace(value=RecoveryDrainMode.ACTIVE)
    underlying = LocalValidatedWorkExecutionOwner(successor)

    class StoppingExecution:
        def __getattr__(self, name):
            return getattr(underlying, name)

        def relinquish(self, token):
            result = underlying.relinquish(token)
            mode.value = RecoveryDrainMode.STOPPED
            return result

    report = RetainedClaimMaintenance(
        store=successor,
        execution=cast(ValidatedWorkExecutionOwner, StoppingExecution()),
    ).reconcile(lambda: mode.value)

    assert [item.status for item in report.items] == [Status.RELEASED]
    assert successor.owner_of(old_claims[0].record_id) is None
    assert successor.owner_of(old_claims[1].record_id) == OWNER


def test_stopped_lifecycle_does_not_scan():
    store = Mock()
    report = RetainedClaimMaintenance(
        store=store, execution=Mock()
    ).reconcile(lambda: RecoveryDrainMode.STOPPED)
    assert report.items == ()
    store.retained_claims.assert_not_called()


def test_scan_and_item_failures_are_reported_without_raising():
    unavailable = Mock()
    unavailable.retained_claims.side_effect = OSError("database unavailable")
    scan = RetainedClaimMaintenance(
        store=unavailable,
        execution=Mock(),
    ).reconcile(ACTIVE)
    assert scan.items == ()
    assert "database unavailable" in scan.scan_error

    candidate = SimpleNamespace(
        record_id="record", evidence_id="evidence", state=State.PARKED
    )
    failing = Mock()
    failing.retained_claims.return_value = (candidate,)
    failing.retained_claim.side_effect = OSError("read interrupted")
    item = RetainedClaimMaintenance(
        store=failing,
        execution=LocalValidatedWorkExecutionOwner(failing),
    ).reconcile(ACTIVE)
    assert item.items[0].status is Status.ERROR
    assert item.items[0].error == "OSError: read interrupted"
    assert isinstance(item, RetainedClaimMaintenanceReport)


def test_retained_claim_queries_reject_empty_or_untyped_states(tmp_path):
    store = Rig(tmp_path / "work.sqlite").open()
    for states in (frozenset(), frozenset({"parked"})):
        with pytest.raises(ValueError, match="typed states"):
            store.retained_claims(cast(frozenset[State], states))
        with pytest.raises(ValueError, match="typed states"):
            store.retained_claim("record", cast(frozenset[State], states))


def test_report_rejects_mutable_or_untyped_items():
    with pytest.raises(ValueError, match="immutable"):
        RetainedClaimMaintenanceReport([])  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="typed items"):
        RetainedClaimMaintenanceReport((object(),))  # type: ignore[arg-type]
