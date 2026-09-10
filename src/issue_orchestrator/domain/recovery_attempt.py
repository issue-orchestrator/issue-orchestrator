"""Durable recovery attempt decisions and typed resumable outcomes."""

from dataclasses import dataclass, replace

from .published_work_finalization import PublishedWorkTarget
from .publication_verification import PublicationVerification
from .recovery_publication import PreparedRecoveryPublication
from .validated_head_publication import PublishValidatedHeadCommand, PublishValidatedHeadOutcome
from .validated_work import DispositionPhase, ValidatedWorkFailure, ValidatedWorkState, PublishValidatedHeadStatus
from .validated_work_commands import ValidatedWorkAuthoritySnapshot
from .validated_work_store import PublishAttempt, ValidatedWorkRecord


@dataclass(frozen=True, slots=True)
class RecoveryAttemptPlan:
    command: PublishValidatedHeadCommand
    phase: DispositionPhase
    previous_attempt_no: int
    resume_finalization: bool

    @classmethod
    def for_record(cls, prepared: PreparedRecoveryPublication, record: ValidatedWorkRecord,
                   attempts: tuple[PublishAttempt, ...]) -> "RecoveryAttemptPlan":
        row = record.current_evidence
        if (row.evidence_id != prepared.workspace.evidence_id
                or row.record_id != prepared.workspace.record_id or row.released_at):
            raise ValueError("prepared publication no longer names current retained evidence")
        command = prepared.command
        command.require_disposition_binding(record.disposition.record_id)
        if command.expected_remote_head_sha != row.authority.expected_remote_head_sha:
            raise ValueError("prepared publication baseline differs from current evidence")
        last = attempts[-1] if attempts else None
        succeeded = last is not None and last.succeeded_for(row.authority)
        if not succeeded and command.pr_number != row.authority.pr_number:
            raise ValueError("prepared publication PR differs from current evidence")
        if succeeded and record.disposition.state is not ValidatedWorkState.PUBLISHING:
            raise ValueError("finalization requires a publishing record")
        command = replace(command, pr_number=record.disposition.pr_number or command.pr_number)
        phase = (DispositionPhase.RECONCILING if record.disposition.state is ValidatedWorkState.PUBLISHING
                 else DispositionPhase.PRE_SUBMISSION)
        return cls(command, phase, last.attempt_no if last is not None else 0, succeeded)


@dataclass(frozen=True, slots=True)
class RecoveryAuthorityStale:
    """The exact approved and current facts for a zero-write stale refusal."""

    approved: ValidatedWorkAuthoritySnapshot
    current: ValidatedWorkAuthoritySnapshot

    def differences(self) -> dict[str, dict[str, object]]:
        approved = self.approved.to_dict()
        current = self.current.to_dict()
        return {
            field: {"approved": approved[field], "current": current[field]}
            for field in approved
            if approved[field] != current[field]
        }

    def describe(self) -> str:
        detail = "; ".join(
            f"{field}: approved={values['approved']!r}, current={values['current']!r}"
            for field, values in self.differences().items()
        )
        return f"Recovery approval no longer matches retained facts ({detail})"

    def to_dict(self) -> dict[str, object]:
        return {
            "approved": self.approved.to_dict(),
            "current": self.current.to_dict(),
            "differences": self.differences(),
        }


@dataclass(frozen=True, slots=True)
class RecoveryAttemptPending:
    message: str
    failure: ValidatedWorkFailure | None = None
    authority_stale: RecoveryAuthorityStale | None = None

    def __post_init__(self) -> None:
        if (
            self.authority_stale is not None
            and self.failure is not ValidatedWorkFailure.AUTHORITY_SNAPSHOT_STALE
        ):
            raise ValueError("authority-stale facts require the matching failure")


def target_from_verification(prepared: PreparedRecoveryPublication,
                             verified: PublicationVerification) -> PublishedWorkTarget | RecoveryAttemptPending:
    if not verified.verified:
        return RecoveryAttemptPending(verified.message, verified.failure)
    pr = verified.pull_request
    if pr is None or pr.head_sha != prepared.command.target_head_sha or verified.branch_head != pr.head_sha:
        return RecoveryAttemptPending("Exact publication target is not confirmed", ValidatedWorkFailure.PUBLISH_TARGET_MISMATCH)
    return PublishedWorkTarget(prepared.workspace.key, pr.number, pr.url, prepared.review_disposition)


def publication_can_finalize(outcome: PublishValidatedHeadOutcome) -> bool:
    return outcome.status in {PublishValidatedHeadStatus.PUBLISHED, PublishValidatedHeadStatus.ALREADY_AT_TARGET}
