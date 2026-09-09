"""Read owner custody; domain eligibility never consults a disposable certified copy."""

from ..domain.issue_run_evidence import IssueRunRecord
from ..domain.validated_work import ValidatedWorkEvidence
from ..domain.validated_work_capture import candidate_evidence
from ..domain.completion_intake import CompletionIntakeError
from ..domain.prepared_completion import PreparedCompletionEvidence, prepare_candidate_evidence
from ..ports.completion_intake import CompletionIntakeLedger
from .completion_intake_artifacts import read_regular


def prepare_candidate(ledger: CompletionIntakeLedger, entry_id: str, run: IssueRunRecord) -> PreparedCompletionEvidence | None:
    entry = ledger.entry_for_receipt(entry_id)
    attestation = ledger.validation_for_receipt(entry_id)
    content = None if entry.normalized_path is None else read_regular(entry.normalized_path)
    result = None if attestation is None else read_regular(attestation.result_path)
    return prepare_candidate_evidence(run, ledger.role_for_receipt(entry_id), entry, attestation, content, result)


def evidence_receive_sequence(ledger: CompletionIntakeLedger, evidence: ValidatedWorkEvidence) -> int:
    """Rank evidence through the same exact custody proof used by publication."""
    return prepare_evidence(ledger, evidence).entry.receive_sequence


def prepare_evidence(ledger: CompletionIntakeLedger, evidence: ValidatedWorkEvidence) -> PreparedCompletionEvidence:
    """Recover the exact allocated role and owned bytes without a session worktree."""
    candidates: list[PreparedCompletionEvidence] = []
    identity = evidence.identity
    for entry in ledger.entries_for_issue(identity.key.issue_number):
        if entry.run.identity != identity.run_identity or entry.normalized_sha256 != identity.completion_artifact.sha256:
            continue
        candidate = ledger.prepare_candidate(entry.entry_id, ledger.recorded_run(entry.run))
        if candidate is None:
            continue
        reconstructed = candidate_evidence(candidate, issue_number=identity.key.issue_number,
            head=evidence.observations.worktree_head_sha, branch_verified=identity.branch_binding_verified,
            captured_at=evidence.observations.captured_at)
        if reconstructed.identity == identity:
            candidates.append(candidate)
    if not candidates:
        raise CompletionIntakeError("retained evidence has no exact owner receive-order proof")
    return max(candidates, key=lambda candidate: candidate.entry.receive_sequence)
