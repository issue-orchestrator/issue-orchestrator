"""Composite durable capability required by retained-work recovery."""

from typing import Protocol

from .published_work_finalization import FinalizationPhaseRecorder
from .recovery_block import RecoveryBlockIssueSource, RecoveryBlockStore
from .validated_work_drain import ValidatedWorkDrainQueue
from .validated_work_preservation import ValidatedWorkAdmissionBackend
from .validated_work_store import ValidatedWorkStore


class ValidatedWorkRecoveryStore(
    ValidatedWorkStore,
    ValidatedWorkAdmissionBackend,
    ValidatedWorkDrainQueue,
    RecoveryBlockStore,
    RecoveryBlockIssueSource,
    FinalizationPhaseRecorder,
    Protocol,
):
    """One transactional store implementing the complete recovery contract."""
