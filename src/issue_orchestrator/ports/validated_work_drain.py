"""Bounded automatic publication selection, without exposing storage rows."""

from typing import Protocol

from ..domain.models import OrchestratorState
from ..domain.recovery_attempt import RecoveryAttemptPending
from ..domain.recovery_completion import RecoveryCompleted
from ..domain.recovery_entry import RecoveryRecordRequest


class ValidatedWorkDrainQueue(Protocol):
    def recovery_requests(self, *, after_record_id: str, limit: int) -> tuple[RecoveryRecordRequest, ...]:
        """Current QUEUED lineage heads and PUBLISHING records, in stable key order.

        Selection grants no authority: the execution owner must re-read and
        compare current evidence/state before claiming and before any effect.
        PARKED/FAILED/resolved work is never implicitly promoted by selection.
        """
        ...


class ValidatedWorkRecoveryOperation(Protocol):
    def run(self, request: RecoveryRecordRequest, state: OrchestratorState) -> RecoveryCompleted | RecoveryAttemptPending:
        """Own execution and claim until all synchronous effects/children finish."""
        ...
