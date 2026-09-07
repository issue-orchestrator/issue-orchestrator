"""Prepare verified state-directory evidence independently of disposable run copies."""

from hashlib import sha256
from ..domain.completion_intake import CompletionIntakeError, CompletionParseStatus
from ..domain.issue_run_evidence import IssueRunRecord
from ..domain.models import CompletionOutcome
from ..domain.prepared_completion import PreparedCompletionEvidence
from ..ports.completion_intake import CompletionIntakeLedger
from .completion_intake_artifacts import read_regular


def prepare_candidate(
    ledger: CompletionIntakeLedger, entry_id: str, run: IssueRunRecord,
) -> PreparedCompletionEvidence | None:
    entry = ledger.entry_for_receipt(entry_id)
    if entry.run != run.run:
        raise CompletionIntakeError("receipt differs from exact recorded run")
    if entry.parse_status is CompletionParseStatus.REJECTED:
        return None
    record = ledger.read_owned_completion(entry_id)
    if record.outcome is not CompletionOutcome.COMPLETED or not record.requests_publication:
        return None
    validation = ledger.validation_for_receipt(entry_id)
    if validation is None:
        raise CompletionIntakeError("completed publication intent lacks owner validation")
    if not validation.passed:
        return None
    if run.branch_name is None:
        raise CompletionIntakeError("legacy run has no owner-recorded branch binding")
    assert entry.normalized_path is not None
    completion_bytes = read_regular(entry.normalized_path)
    validation_bytes = read_regular(validation.result_path)
    if (sha256(completion_bytes).hexdigest() != entry.normalized_sha256
        or sha256(validation_bytes).hexdigest() != validation.result_sha256):
        raise CompletionIntakeError("admitted candidate custody is corrupt")
    return PreparedCompletionEvidence(
        run, entry, validation, completion_bytes, validation_bytes,
        tuple(record.requested_actions),
    )
