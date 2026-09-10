"""Bounded drain observations; publication failures remain attached to their work."""

from dataclasses import dataclass
from enum import StrEnum

from .recovery_attempt import RecoveryAttemptPending
from .recovery_completion import RecoveryCompleted
from .recovery_block import RecoveryBlockSweepReport


class RecoveryDrainMode(StrEnum):
    ACTIVE = "active"
    STOPPED = "stopped"


@dataclass(frozen=True, slots=True)
class RecoveryDrainItem:
    record_id: str
    evidence_id: str
    outcome: RecoveryCompleted | RecoveryAttemptPending


@dataclass(frozen=True, slots=True)
class RecoveryDrainReport:
    items: tuple[RecoveryDrainItem, ...]
    block_sweep: RecoveryBlockSweepReport = RecoveryBlockSweepReport(())
