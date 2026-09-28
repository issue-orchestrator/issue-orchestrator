"""Bounded drain observations; publication failures remain attached to their work."""

from dataclasses import dataclass
from enum import StrEnum

from .recovery_attempt import RecoveryAttemptPending
from .recovery_completion import RecoveryCompleted
from .recovery_block import RecoveryBlockSweepReport
from .retained_claim_maintenance import RetainedClaimMaintenanceReport


class RecoveryDrainMode(StrEnum):
    ACTIVE = "active"
    STOPPED = "stopped"


@dataclass(frozen=True, slots=True)
class RecoveryDrainItem:
    record_id: str
    evidence_id: str
    outcome: RecoveryCompleted | RecoveryAttemptPending


@dataclass(frozen=True, slots=True)
class RecoveryScopeSweepReport:
    """Records the scope sweep resolved as outside recovery scope (#7323)."""

    retired: tuple[str, ...]
    error: str = ""

    def __post_init__(self) -> None:
        if type(self.retired) is not tuple or any(
            type(item) is not str or not item for item in self.retired
        ):
            raise ValueError("retired records must be an immutable tuple of ids")
        if type(self.error) is not str:
            raise ValueError("scope sweep error must be text")


@dataclass(frozen=True, slots=True)
class RecoveryDrainReport:
    items: tuple[RecoveryDrainItem, ...]
    claim_maintenance: RetainedClaimMaintenanceReport = RetainedClaimMaintenanceReport(())
    block_sweep: RecoveryBlockSweepReport = RecoveryBlockSweepReport(())
    scope_sweep: RecoveryScopeSweepReport = RecoveryScopeSweepReport(())
