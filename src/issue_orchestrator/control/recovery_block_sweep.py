"""Bounded round-robin reconciliation of retained-work issue projections."""

from ..domain.recovery_block import RecoveryBlockSweepItem, RecoveryBlockSweepReport
from ..domain.recovery_drain import RecoveryDrainMode
from ..domain.validated_work import require_positive
from ..ports.recovery_block import RecoveryBlockIssueReconciler, RecoveryBlockIssueSource
from ..ports.validated_work_drain import RecoveryDrainAdmission


class AggregateRecoveryBlockSweep:
    def __init__(
        self,
        *,
        source: RecoveryBlockIssueSource,
        reconciler: RecoveryBlockIssueReconciler,
        batch_size: int,
    ) -> None:
        require_positive(batch_size, "recovery block batch size")
        self._source = source
        self._reconciler = reconciler
        self._batch_size = batch_size
        self._after = 0

    def tick(self, admission: RecoveryDrainAdmission) -> RecoveryBlockSweepReport:
        try:
            issues = self._next_issues()
        except Exception as error:
            return RecoveryBlockSweepReport((), f"Retained issue scan failed: {error}")
        items: list[RecoveryBlockSweepItem] = []
        for issue_number in issues:
            if admission() is RecoveryDrainMode.STOPPED:
                break
            items.append(
                RecoveryBlockSweepItem(
                    issue_number,
                    self._reconciler.reconcile_issue_block(issue_number),
                )
            )
        if items:
            completed_page = len(items) == len(issues)
            self._after = (
                0
                if completed_page and len(issues) < self._batch_size
                else items[-1].issue_number
            )
        return RecoveryBlockSweepReport(tuple(items))

    def _next_issues(self) -> tuple[int, ...]:
        issues = self._source.retained_issue_numbers(
            after_issue_number=self._after, limit=self._batch_size
        )
        if not issues and self._after:
            self._after = 0
            issues = self._source.retained_issue_numbers(
                after_issue_number=0, limit=self._batch_size
            )
        return issues
