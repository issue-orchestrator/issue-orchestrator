"""Trusted intake output: immutable owned bytes and the exact allocation binding."""

from dataclasses import dataclass
from .completion_intake import CompletionIntakeEntry, CompletionValidationAttestation
from .issue_run_evidence import IssueRunRecord
from .models import RequestedAction
from .registered_completion import CompletionRunRole
from .review_validation import ReviewValidationEvidence


@dataclass(frozen=True, slots=True)
class PreparedCompletionEvidence:
    run: IssueRunRecord
    role: CompletionRunRole
    entry: CompletionIntakeEntry
    validation: CompletionValidationAttestation
    completion_bytes: bytes
    validation_bytes: bytes
    requested_actions: tuple[RequestedAction, ...]

    @property
    def review_validation(self) -> ReviewValidationEvidence:
        return ReviewValidationEvidence(self.validation_bytes, self.validation.head_sha,
                                        self.validation.passed)


from hashlib import sha256
import json
from .completion_intake import CompletionIntakeError, CompletionParseStatus
from .models import CompletionOutcome, CompletionRecord


def prepare_candidate_evidence(run: IssueRunRecord, role: CompletionRunRole, entry: CompletionIntakeEntry,
        validation: CompletionValidationAttestation | None, completion_bytes: bytes | None,
        validation_bytes: bytes | None) -> PreparedCompletionEvidence | None:
    if (role.agent_label, role.task) != (run.agent_label, run.completion_task):
        raise CompletionIntakeError("receipt role differs from exact recorded allocation")
    if entry.run != run.run:
        raise CompletionIntakeError("receipt differs from exact recorded run")
    if entry.parse_status is CompletionParseStatus.REJECTED:
        return None
    if completion_bytes is None or sha256(completion_bytes).hexdigest() != entry.normalized_sha256:
        raise CompletionIntakeError("admitted completion custody is corrupt")
    record = CompletionRecord.from_dict(json.loads(completion_bytes))
    if record.outcome is not CompletionOutcome.COMPLETED or not record.requests_publication:
        return None
    if validation is None:
        raise CompletionIntakeError("completed publication intent lacks owner validation")
    if validation_bytes is None or sha256(validation_bytes).hexdigest() != validation.result_sha256:
        raise CompletionIntakeError("admitted validation custody is corrupt")
    if not validation.passed:
        return None
    if run.branch_name is None:
        raise CompletionIntakeError("legacy run has no owner-recorded branch binding")
    return PreparedCompletionEvidence(run, role, entry, validation, completion_bytes, validation_bytes,
        tuple(record.requested_actions))
