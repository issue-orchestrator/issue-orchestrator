"""Pure receipt eligibility and publication rules shared by intake consumers."""

from .completion_intake import (
    CompletionIntakeEntry,
    CompletionIntakeReceipt,
    CompletionIntakeError,
    CompletionParseStatus,
    CompletionValidationAttestation,
    CompletionValidationFailed,
)
from .models import CompletionRecord
from .session_run import RunContainedFile


def latest_accepted_receipt(
    entries: tuple[CompletionIntakeEntry, ...],
) -> CompletionIntakeReceipt | None:
    accepted = [
        entry
        for entry in entries
        if entry.parse_status is CompletionParseStatus.ACCEPTED
    ]
    return accepted[-1].receipt if accepted else None


def require_publication_attestation(
    record: CompletionRecord,
    attestation: CompletionValidationAttestation | None,
) -> None:
    """Publication intent requires a passed owner attestation; failure remains retryable."""
    if not record.requests_publication:
        return
    if attestation is None:
        raise CompletionIntakeError("completion has no trusted validation attestation")
    if not attestation.passed:
        raise CompletionValidationFailed(attestation.result_path)


def normalized_completion_artifact(entry: CompletionIntakeEntry) -> RunContainedFile:
    if entry.normalized_path is None:
        raise CompletionIntakeError("receipt has no normalized completion")
    return RunContainedFile(entry.normalized_path.parent, entry.normalized_path)
