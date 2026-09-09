"""Idempotent recovery completion after the publication resolution transaction."""

from dataclasses import dataclass

from .published_work_finalization import PublishedWorkTarget
from .validated_work import FinalizationPhase, ResolutionKind, ValidatedWorkState
from .validated_work_store import ValidatedWorkRecord


@dataclass(frozen=True, slots=True)
class RecoveryCompleted:
    target: PublishedWorkTarget


def recovery_resolution_complete(record: ValidatedWorkRecord, target: PublishedWorkTarget) -> bool:
    if record.disposition.state is not ValidatedWorkState.RECOVERED:
        return False
    if (record.finalization_phase is not FinalizationPhase.COMPLETE
            or record.resolution_kind is not ResolutionKind.PUBLISHED
            or record.disposition.published_head_sha != target.key.validated_head_sha
            or record.disposition.key != target.key
            or record.disposition.pr_number != target.pr_number
            or record.current_evidence.admission.evidence.identity.review_disposition is not target.review_disposition):
        raise ValueError("recovered record does not prove this publication")
    return True
