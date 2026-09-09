"""Bounded persisted selection and fair outage retry without wall-clock waits."""

from types import SimpleNamespace

import pytest

from issue_orchestrator.control.recovery_drain import RecoveryDrain
from issue_orchestrator.domain.models import OrchestratorState
from issue_orchestrator.domain.recovery_attempt import RecoveryAttemptPending
from issue_orchestrator.domain.validated_work import ValidatedWorkState, ValidatedWorkFailure
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
    first = store.recovery_requests(after_record_id="", limit=2)
    second = store.recovery_requests(after_record_id=first[-1].record_id, limit=2)
    assert len(first) == 2 and len(second) == 1
    assert [r.record_id for r in first + second] == sorted(row.evidence.record_id for row in admitted)
    assert all(r.approved is None for r in first + second)
    assert rig.open().recovery_requests(after_record_id="", limit=10) == first + second
    with pytest.raises(ValueError):
        store.recovery_requests(after_record_id="", limit=0)


def test_batch_bound_and_interval_do_not_starve_after_exception(tmp_path):
    store = Rig(tmp_path / "work.sqlite").open()
    for issue in range(1, 6):
        store.admit(capture(issue=issue))
    ordered = store.recovery_requests(after_record_id="", limit=10)
    now = SimpleNamespace(value=0.0)
    operations = Operations()
    operations.raise_for.add(ordered[0].record_id)
    drain = RecoveryDrain(queue=store, operation=operations, batch_size=2, interval_seconds=10, clock=lambda: now.value)
    state = OrchestratorState()
    first = drain.tick(state)
    assert len(first.items) == 2
    assert "remote interrupted" in first.items[0].outcome.message
    assert len(drain.tick(state).items) == 0
    now.value = 10
    assert len(drain.tick(state).items) == 2
    now.value = 20
    assert len(drain.tick(state).items) == 1
    assert operations.called == [request.record_id for request in ordered]
    now.value = 30
    assert len(drain.tick(state).items) == 2
    assert operations.called[-2:] == [request.record_id for request in ordered[:2]]


def test_interval_starts_after_synchronous_work_finishes(tmp_path):
    store = Rig(tmp_path / "work.sqlite").open()
    store.admit(capture())
    now = SimpleNamespace(value=0.0)
    operations = Operations()
    operations.during = lambda: setattr(now, "value", 100.0)
    drain = RecoveryDrain(queue=store, operation=operations, batch_size=1, interval_seconds=10, clock=lambda: now.value)
    assert len(drain.tick(OrchestratorState()).items) == 1
    now.value = 109
    assert len(drain.tick(OrchestratorState()).items) == 0
    now.value = 110
    assert len(drain.tick(OrchestratorState()).items) == 1
