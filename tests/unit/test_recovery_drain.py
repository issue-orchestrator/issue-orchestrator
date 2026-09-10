"""Bounded persisted selection and fair outage retry without wall-clock waits."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from issue_orchestrator.control.recovery_drain import RecoveryDrain
from issue_orchestrator.domain.models import OrchestratorState
from issue_orchestrator.domain.recovery_attempt import RecoveryAttemptPending
from issue_orchestrator.domain.recovery_drain import RecoveryDrainMode
from issue_orchestrator.domain.validated_work import (
    RemoteBaselineStatus,
    ValidatedWorkFailure,
    ValidatedWorkState,
)
from issue_orchestrator.domain.validated_work_remote_authority import RemoteAuthorityRefreshRequest
from tests.unit.validated_work_support import Rig, capture, claim, begin


class Operations:
    def __init__(self):
        self.called = []
        self.raise_for = set()
        self.during = lambda: None

    def run(self, request, state):
        self.called.append(request.record_id)
        self.during()
        if request.record_id in self.raise_for:
            raise OSError("remote interrupted")
        return RecoveryAttemptPending("still retained")


class Refreshes:
    def __init__(self):
        self.called = []

    def run(self, request):
        self.called.append(request.record_id)
        return RecoveryAttemptPending("authority refreshed")


def test_queue_selects_current_automatic_heads_and_publishing_only(tmp_path):
    rig = Rig(tmp_path / "work.sqlite")
    store = rig.open()
    admitted = [capture(issue=i) for i in range(1, 4)]
    for row in admitted:
        store.admit(row)
    publishing = admitted[1]
    owned = claim(store, publishing)
    assert begin(store, owned) is not None
    assert store.relinquish_claim(owned)
    store.admit(capture(issue=4, state=ValidatedWorkState.PARKED))
    store.admit(capture(issue=5, state=ValidatedWorkState.FAILED, failure=ValidatedWorkFailure.PUSH_FAILED))
    first = store.drain_requests(after_record_id="", limit=2)
    second = store.drain_requests(after_record_id=first[-1].record_id, limit=2)
    assert len(first) == 2 and len(second) == 1
    assert [r.record_id for r in first + second] == sorted(row.evidence.record_id for row in admitted)
    assert all(r.approved is None for r in first + second)
    assert rig.open().drain_requests(after_record_id="", limit=10) == first + second
    with pytest.raises(ValueError):
        store.drain_requests(after_record_id="", limit=0)


def test_queue_routes_unobserved_remote_authority_to_refresh(tmp_path):
    store = Rig(tmp_path / "work.sqlite").open()
    parked = capture(
        issue=1,
        state=ValidatedWorkState.PARKED,
        failure=ValidatedWorkFailure.REMOTE_UNREADABLE,
        remote_status=RemoteBaselineStatus.UNOBSERVED,
    )
    publishing = capture(
        issue=2,
        state=ValidatedWorkState.PARKED,
        failure=ValidatedWorkFailure.REMOTE_UNREADABLE,
        remote_status=RemoteBaselineStatus.UNOBSERVED,
    )
    store.admit(parked)
    store.admit(publishing)
    owned = claim(store, publishing)
    assert begin(store, owned, approved=True) is not None
    assert store.relinquish_claim(owned)

    requests = store.drain_requests(after_record_id="", limit=10)

    assert len(requests) == 2
    assert all(isinstance(item, RemoteAuthorityRefreshRequest) for item in requests)
    operations = Operations()
    refreshes = Refreshes()
    report = RecoveryDrain(
        queue=store,
        operation=operations,
        authority_refresh=refreshes,
        batch_size=2,
        interval_seconds=10,
    ).tick(OrchestratorState(), lambda: RecoveryDrainMode.ACTIVE)
    assert len(report.items) == 2
    assert operations.called == []
    assert refreshes.called == [request.record_id for request in requests]


def test_batch_bound_and_interval_do_not_starve_after_exception(tmp_path):
    store = Rig(tmp_path / "work.sqlite").open()
    for issue in range(1, 6):
        store.admit(capture(issue=issue))
    ordered = store.drain_requests(after_record_id="", limit=10)
    now = SimpleNamespace(value=0.0)
    operations = Operations()
    operations.raise_for.add(ordered[0].record_id)
    drain = RecoveryDrain(
        queue=store,
        operation=operations,
        authority_refresh=Refreshes(),
        batch_size=2,
        interval_seconds=10,
        clock=lambda: now.value,
    )
    state = OrchestratorState()
    active = lambda: RecoveryDrainMode.ACTIVE
    first = drain.tick(state, active)
    assert len(first.items) == 2
    assert "remote interrupted" in first.items[0].outcome.message
    assert len(drain.tick(state, active).items) == 0
    now.value = 10
    assert len(drain.tick(state, active).items) == 2
    now.value = 20
    assert len(drain.tick(state, active).items) == 1
    assert operations.called == [request.record_id for request in ordered]
    now.value = 30
    assert len(drain.tick(state, active).items) == 2
    assert operations.called[-2:] == [request.record_id for request in ordered[:2]]


def test_interval_starts_after_synchronous_work_finishes(tmp_path):
    store = Rig(tmp_path / "work.sqlite").open()
    store.admit(capture())
    now = SimpleNamespace(value=0.0)
    operations = Operations()
    operations.during = lambda: setattr(now, "value", 100.0)
    drain = RecoveryDrain(
        queue=store,
        operation=operations,
        authority_refresh=Refreshes(),
        batch_size=1,
        interval_seconds=10,
        clock=lambda: now.value,
    )
    active = lambda: RecoveryDrainMode.ACTIVE
    assert len(drain.tick(OrchestratorState(), active).items) == 1
    now.value = 109
    assert len(drain.tick(OrchestratorState(), active).items) == 0
    now.value = 110
    assert len(drain.tick(OrchestratorState(), active).items) == 1


def test_stopped_mode_cannot_select_or_start_recovery_work():
    queue = Mock()
    operation = Mock()
    authority_refresh = Mock()
    drain = RecoveryDrain(
        queue=queue,
        operation=operation,
        authority_refresh=authority_refresh,
        batch_size=1,
        interval_seconds=10,
    )

    report = drain.tick(OrchestratorState(), lambda: RecoveryDrainMode.STOPPED)

    assert report.items == ()
    queue.drain_requests.assert_not_called()
    operation.run.assert_not_called()
    authority_refresh.run.assert_not_called()


def test_lifecycle_stop_during_batch_prevents_another_operation_and_preserves_cursor(
    tmp_path,
):
    store = Rig(tmp_path / "work.sqlite").open()
    for issue in range(1, 4):
        store.admit(capture(issue=issue))
    ordered = store.drain_requests(after_record_id="", limit=10)
    now = SimpleNamespace(value=0.0)
    mode = SimpleNamespace(value=RecoveryDrainMode.ACTIVE)
    operations = Operations()
    operations.during = lambda: setattr(mode, "value", RecoveryDrainMode.STOPPED)
    drain = RecoveryDrain(
        queue=store,
        operation=operations,
        authority_refresh=Refreshes(),
        batch_size=3,
        interval_seconds=10,
        clock=lambda: now.value,
    )

    first = drain.tick(OrchestratorState(), lambda: mode.value)

    assert [item.record_id for item in first.items] == [ordered[0].record_id]
    assert operations.called == [ordered[0].record_id]
    mode.value = RecoveryDrainMode.ACTIVE
    operations.during = lambda: None
    now.value = 10

    resumed = drain.tick(OrchestratorState(), lambda: mode.value)

    assert [item.record_id for item in resumed.items] == [
        ordered[1].record_id,
        ordered[2].record_id,
    ]
