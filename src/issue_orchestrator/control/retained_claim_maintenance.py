"""Release claims left on work whose publication operation is quiescent."""

from collections.abc import Callable
import logging

from ..domain.recovery_drain import RecoveryDrainMode
from ..domain.retained_claim_maintenance import (
    RetainedClaimMaintenanceItem,
    RetainedClaimMaintenanceReport,
    RetainedClaimMaintenanceStatus,
)
from ..domain.validated_work import ValidatedWorkState
from ..domain.validated_work_claim import RetainedClaim
from ..domain.validated_work_execution import RecordExecutionBusy
from ..ports.retained_claim_maintenance import RetainedClaimMaintenanceStore
from ..ports.validated_work_execution import ValidatedWorkExecutionOwner

logger = logging.getLogger(__name__)

MAINTENANCE_STATES = frozenset(
    {
        ValidatedWorkState.PARKED,
        ValidatedWorkState.FAILED,
        ValidatedWorkState.RECOVERED,
        ValidatedWorkState.ABANDONED,
    }
)


class RetainedClaimMaintenance:
    """Serialize with complete record operations before releasing ownership."""

    def __init__(
        self,
        *,
        store: RetainedClaimMaintenanceStore,
        execution: ValidatedWorkExecutionOwner,
    ) -> None:
        self._store = store
        self._execution = execution

    def reconcile(
        self, admission: Callable[[], RecoveryDrainMode]
    ) -> RetainedClaimMaintenanceReport:
        if admission() is RecoveryDrainMode.STOPPED:
            return RetainedClaimMaintenanceReport(())
        try:
            candidates = self._store.retained_claims(MAINTENANCE_STATES)
        except Exception as error:
            logger.exception("Retained-claim maintenance scan failed")
            return RetainedClaimMaintenanceReport(
                (), f"Retained-claim maintenance scan failed: {error}"
            )

        items: list[RetainedClaimMaintenanceItem] = []
        for candidate in candidates:
            if admission() is RecoveryDrainMode.STOPPED:
                break
            items.append(self._reconcile(candidate))
        return RetainedClaimMaintenanceReport(tuple(items))

    def _reconcile(
        self, candidate: RetainedClaim
    ) -> RetainedClaimMaintenanceItem:
        lease = self._execution.try_enter(candidate.record_id)
        if isinstance(lease, RecordExecutionBusy):
            return self._item(candidate, RetainedClaimMaintenanceStatus.BUSY)
        try:
            with lease as token:
                current = self._store.retained_claim(
                    candidate.record_id, MAINTENANCE_STATES
                )
                if current != candidate:
                    return self._item(
                        candidate, RetainedClaimMaintenanceStatus.CHANGED
                    )
                claim = self._execution.claim(token)
                if claim is None:
                    claim = self._store.acquire_claim(
                        current.record_id,
                        expected_states=frozenset({current.state}),
                        evidence_id=current.evidence_id,
                    )
                    if claim is None:
                        return self._item(
                            candidate,
                            RetainedClaimMaintenanceStatus.OWNER_UNAVAILABLE,
                        )
                    self._execution.remember_claim(token, claim)
                status = (
                    RetainedClaimMaintenanceStatus.RELEASED
                    if self._execution.relinquish(token)
                    else RetainedClaimMaintenanceStatus.RELEASE_DEFERRED
                )
                return self._item(candidate, status)
        except Exception as error:
            logger.exception(
                "Retained-claim maintenance failed for record %s",
                candidate.record_id,
            )
            return RetainedClaimMaintenanceItem(
                candidate.record_id,
                candidate.evidence_id,
                candidate.state,
                RetainedClaimMaintenanceStatus.ERROR,
                f"{type(error).__name__}: {error}",
            )

    @staticmethod
    def _item(
        candidate: RetainedClaim, status: RetainedClaimMaintenanceStatus
    ) -> RetainedClaimMaintenanceItem:
        return RetainedClaimMaintenanceItem(
            candidate.record_id,
            candidate.evidence_id,
            candidate.state,
            status,
        )
