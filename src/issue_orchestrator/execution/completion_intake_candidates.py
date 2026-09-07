"""Read owner custody; domain eligibility never consults a disposable certified copy."""

from ..domain.issue_run_evidence import IssueRunRecord
from ..domain.prepared_completion import PreparedCompletionEvidence, prepare_candidate_evidence
from ..ports.completion_intake import CompletionIntakeLedger
from .completion_intake_artifacts import read_regular


def prepare_candidate(ledger: CompletionIntakeLedger, entry_id: str, run: IssueRunRecord) -> PreparedCompletionEvidence | None:
    entry = ledger.entry_for_receipt(entry_id)
    attestation = ledger.validation_for_receipt(entry_id)
    content = None if entry.normalized_path is None else read_regular(entry.normalized_path)
    result = None if attestation is None else read_regular(attestation.result_path)
    return prepare_candidate_evidence(run, entry, attestation, content, result)
