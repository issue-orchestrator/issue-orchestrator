"""Staged durability and aggregate release, awaiting disposition runtime composition."""

from typing import Protocol

from ..domain.published_work_finalization import (
    FinalizationOutcome,
    FinalizationCheckpoint,
    PublishedWorkFinalizationRequest,
    PublishedWorkTarget,
    RecoveryBlockReleaseOutcome,
    RecoveryBlockReleaseRequest,
)
from ..domain.validated_work import FinalizationPhase, ValidatedWorkFailure
from ..domain.validated_work_claim import ValidatedWorkClaim


class FinalizationPhaseRecorder(Protocol):
    def read_finalization_checkpoint(
        self, claim: ValidatedWorkClaim, target: PublishedWorkTarget
    ) -> FinalizationCheckpoint | None:
        """Exact claim, identity, PR, review and successful attempt checked atomically.

        None refuses stale, mismatched or unrelated work before any effect.
        Publishing phases, a matching REVIEW_ROUTING_FAILED checkpoint, and
        COMPLETE for this record's recovered publication are the only results.
        """
        ...

    def record_finalization_phase(
        self, claim: ValidatedWorkClaim, *, phase: FinalizationPhase, recorded_at: str
    ) -> bool: ...

    def fail(
        self,
        claim: ValidatedWorkClaim,
        *,
        failure: ValidatedWorkFailure,
        reason: str,
        failed_at: str,
    ) -> bool: ...


class AggregateRecoveryBlockOwner(Protocol):
    def release_published_record(
        self, request: RecoveryBlockReleaseRequest
    ) -> RecoveryBlockReleaseOutcome:
        """Under the issue mutation gate, revalidate this record and all siblings.

        Require the shared effect authority before EVERY effect and observation.
        Release this record's interest only after publication and REVIEW_ROUTED
        are durable. Other unresolved records (including ancestors) retain their
        blocks. Clear observed blockers only through their owners. Observe writes
        before returning RELEASED; a refused/failed projection remains retryable.
        Replay is idempotent if interrupted before RECOVERY_CLEARED is committed.

        The real disposition owner/runtime must implement this; there is no
        production last-holder assumption or per-record label-deletion adapter.
        """
        ...


class PublishedWorkFinalizer(Protocol):
    def finalize(
        self, request: PublishedWorkFinalizationRequest
    ) -> FinalizationOutcome: ...
