"""Bounded retained-issue projection convergence."""

from dataclasses import dataclass, field
from types import SimpleNamespace

import pytest

from issue_orchestrator.control.recovery_block_sweep import (
    AggregateRecoveryBlockSweep,
)
from issue_orchestrator.domain.recovery_block import (
    RecoveryBlockReconcileOutcome,
    RecoveryBlockReconcileStatus,
)
from issue_orchestrator.domain.recovery_drain import RecoveryDrainMode
from tests.unit.validated_work_support import Rig, capture


@dataclass
class Reconciler:
    calls: list[int] = field(default_factory=list)

    def reconcile_issue_block(self, issue_number: int) -> RecoveryBlockReconcileOutcome:
        self.calls.append(issue_number)
        return RecoveryBlockReconcileOutcome(
            RecoveryBlockReconcileStatus.RECONCILED,
            (),
            f"reconciled {issue_number}",
        )


def test_retained_issue_source_and_sweep_are_bounded_round_robin(tmp_path):
    store = Rig(tmp_path / "work.sqlite").open()
    for issue_number in range(1, 6):
        store.admit(capture(issue=issue_number))
    reconciler = Reconciler()
    sweep = AggregateRecoveryBlockSweep(
        source=store, reconciler=reconciler, batch_size=2
    )

    reports = tuple(sweep.tick(lambda: RecoveryDrainMode.ACTIVE) for _ in range(4))

    assert [
        [item.issue_number for item in report.items] for report in reports
    ] == [[1, 2], [3, 4], [5], [1, 2]]
    assert reconciler.calls == [1, 2, 3, 4, 5, 1, 2]
    assert store.retained_issue_numbers(after_issue_number=2, limit=2) == (3, 4)


def test_retained_issue_scan_failure_is_reported_without_partial_results():
    class UnavailableSource:
        def retained_issue_numbers(self, *, after_issue_number, limit):
            raise OSError("database unavailable")

    reconciler = Reconciler()
    report = AggregateRecoveryBlockSweep(
        source=UnavailableSource(), reconciler=reconciler, batch_size=2
    ).tick(lambda: RecoveryDrainMode.ACTIVE)

    assert report.items == ()
    assert report.error == "Retained issue scan failed: database unavailable"
    assert reconciler.calls == []


def test_sweep_preserves_each_typed_reconciliation_outcome():
    class Source:
        def retained_issue_numbers(self, *, after_issue_number, limit):
            return (7, 8)

    class MixedReconciler:
        def reconcile_issue_block(self, issue_number):
            status = (
                RecoveryBlockReconcileStatus.BUSY
                if issue_number == 7
                else RecoveryBlockReconcileStatus.RETRY
            )
            return RecoveryBlockReconcileOutcome(status, (), status.value)

    report = AggregateRecoveryBlockSweep(
        source=Source(), reconciler=MixedReconciler(), batch_size=2
    ).tick(lambda: RecoveryDrainMode.ACTIVE)

    assert [item.outcome.status for item in report.items] == [
        RecoveryBlockReconcileStatus.BUSY,
        RecoveryBlockReconcileStatus.RETRY,
    ]
    assert report.error == ""


@pytest.mark.parametrize("lifecycle_stop", ["pause", "shutdown"])
def test_sweep_stops_before_the_next_issue_and_resumes_at_that_issue(lifecycle_stop):
    class Source:
        def retained_issue_numbers(self, *, after_issue_number, limit):
            return tuple(
                issue for issue in (1, 2, 3) if issue > after_issue_number
            )[:limit]

    lifecycle = SimpleNamespace(pause=False, shutdown=False)

    def admission():
        return (
            RecoveryDrainMode.STOPPED
            if lifecycle.pause or lifecycle.shutdown
            else RecoveryDrainMode.ACTIVE
        )

    class StoppingReconciler(Reconciler):
        def reconcile_issue_block(self, issue_number):
            outcome = super().reconcile_issue_block(issue_number)
            if issue_number == 1:
                setattr(lifecycle, lifecycle_stop, True)
            return outcome

    reconciler = StoppingReconciler()
    sweep = AggregateRecoveryBlockSweep(
        source=Source(), reconciler=reconciler, batch_size=3
    )

    stopped = sweep.tick(admission)

    assert [item.issue_number for item in stopped.items] == [1]
    assert reconciler.calls == [1]
    setattr(lifecycle, lifecycle_stop, False)

    resumed = sweep.tick(admission)

    assert [item.issue_number for item in resumed.items] == [2, 3]
    assert reconciler.calls == [1, 2, 3]
