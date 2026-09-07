"""Pure exact-snapshot publication eligibility, shared by the durable owner."""

from .validated_work import (
    DispositionPhase,
    LineageRole,
    ValidatedWorkState,
)
from .validated_work_store import PublishAttempt, PublishValidatedHeadStatus
from .validated_work_commands import ValidatedWorkAuthoritySnapshot


def publication_eligible(
    *,
    state: ValidatedWorkState,
    lineage_role: LineageRole,
    current: ValidatedWorkAuthoritySnapshot,
    approved: ValidatedWorkAuthoritySnapshot | None,
    target: str,
    expected: str,
    phase: DispositionPhase,
) -> bool:
    if approved is not None and (
        type(approved) is not ValidatedWorkAuthoritySnapshot or approved != current
    ):
        return False
    if target != current.validated_head_sha or expected != (
        current.expected_remote_head_sha or ""
    ):
        return False
    if state is ValidatedWorkState.PUBLISHING:
        return phase is DispositionPhase.RECONCILING
    if (
        phase is not DispositionPhase.PRE_SUBMISSION
        or lineage_role is LineageRole.PENDING
    ):
        return False
    if state is ValidatedWorkState.QUEUED:
        return lineage_role is LineageRole.HEAD
    return state is ValidatedWorkState.PARKED and approved == current


def retry_permitted(
    last: PublishAttempt | None, *, fence: int, evidence_id: str
) -> bool:
    """Successful attempts resume finalization; lost attempts require a successor."""
    if last is None:
        return True
    if last.evidence_id != evidence_id and last.outcome is not None:
        return True
    if last.succeeded:
        return False
    if last.outcome is None:
        return last.fence != fence
    return last.outcome is PublishValidatedHeadStatus.TRANSIENT_FAILURE


def recordable_outcome(outcome: PublishValidatedHeadStatus) -> bool:
    """Reject unsupported intent; authenticated attempt construction owns shape."""
    if type(outcome) is not PublishValidatedHeadStatus:
        raise ValueError("attempt outcome must be typed")
    return outcome is not PublishValidatedHeadStatus.SUPERSEDED


def attempt_requires_failure(
    outcome: PublishValidatedHeadStatus, *, attempt_no: int, limit: int
) -> bool:
    return outcome in {
        PublishValidatedHeadStatus.REJECTED,
        PublishValidatedHeadStatus.DIVERGED,
    } or (
        outcome is PublishValidatedHeadStatus.TRANSIENT_FAILURE and attempt_no >= limit
    )
