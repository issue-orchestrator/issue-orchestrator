"""Boundaries for releasing claims left on quiescent retained work."""

from collections.abc import Callable
from typing import Protocol

from ..domain.recovery_drain import RecoveryDrainMode
from ..domain.retained_claim_maintenance import RetainedClaimMaintenanceReport
from ..domain.validated_work import ValidatedWorkState
from ..domain.validated_work_claim import RetainedClaim, ValidatedWorkClaim


class RetainedClaimMaintenanceStore(Protocol):
    def retained_claims(
        self, states: frozenset[ValidatedWorkState]
    ) -> tuple[RetainedClaim, ...]: ...

    def retained_claim(
        self, record_id: str, states: frozenset[ValidatedWorkState]
    ) -> RetainedClaim | None:
        """Return one atomic current snapshot when it remains eligible."""
        ...

    def acquire_claim(
        self,
        record_id: str,
        *,
        expected_states: frozenset[ValidatedWorkState],
        evidence_id: str,
    ) -> ValidatedWorkClaim | None: ...


class RetainedClaimMaintenanceOwner(Protocol):
    def reconcile(
        self, admission: Callable[[], RecoveryDrainMode]
    ) -> RetainedClaimMaintenanceReport: ...


class NullRetainedClaimMaintenance:
    def reconcile(
        self, admission: Callable[[], RecoveryDrainMode]
    ) -> RetainedClaimMaintenanceReport:
        return RetainedClaimMaintenanceReport(())
