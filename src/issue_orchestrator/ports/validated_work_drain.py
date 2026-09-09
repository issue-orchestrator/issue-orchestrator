"""Bounded automatic publication selection, without exposing storage rows."""

from typing import Protocol

from ..domain.models import OrchestratorState
from ..domain.recovery_drain import RecoveryDrainMode, RecoveryDrainReport
from ..domain.recovery_attempt import RecoveryAttemptPending
from ..domain.recovery_completion import RecoveryCompleted
from ..domain.recovery_entry import RecoveryRecordRequest
from ..domain.validated_work_remote_authority import RemoteAuthorityRefreshRequest

ValidatedWorkDrainRequest = RecoveryRecordRequest | RemoteAuthorityRefreshRequest


class ValidatedWorkDrainQueue(Protocol):
    def drain_requests(self, *, after_record_id: str, limit: int) -> tuple[ValidatedWorkDrainRequest, ...]:
        """Current recovery or authority-refresh work, in stable key order.

        Selection grants no authority: the execution owner must re-read and
        compare current evidence/state before claiming and before any effect.
        Only PARKED remote-read failures may be selected for refresh; no parked
        record is implicitly promoted by selection.
        """
        ...


class ValidatedWorkRecoveryOperation(Protocol):
    def run(self, request: RecoveryRecordRequest, state: OrchestratorState) -> RecoveryCompleted | RecoveryAttemptPending:
        """Own execution and claim until all synchronous effects/children finish."""
        ...


class ValidatedWorkAuthorityRefreshOperation(Protocol):
    def run(self, request: RemoteAuthorityRefreshRequest) -> RecoveryAttemptPending:
        """Refresh one exact retained record under execution and durable claims."""
        ...


class RecoveryDrainAdmission(Protocol):
    """Live lifecycle authority for starting another recovery operation."""

    def __call__(self) -> RecoveryDrainMode: ...


class ValidatedWorkRecoveryDrain(Protocol):
    def tick(
        self, state: OrchestratorState, admission: RecoveryDrainAdmission
    ) -> RecoveryDrainReport:
        """Run a due bounded batch and return its observation."""
        ...


class NullValidatedWorkRecoveryDrain:
    """Explicit testing composition with no production recovery authority."""

    def tick(
        self, state: OrchestratorState, admission: RecoveryDrainAdmission
    ) -> RecoveryDrainReport:
        return RecoveryDrainReport(())
