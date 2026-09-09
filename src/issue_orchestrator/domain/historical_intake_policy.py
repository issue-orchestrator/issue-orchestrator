"""Historical selection eligibility and PARKED evidence identity, without I/O."""

import json
from hashlib import sha256
from .historical_intake import (
    HistoricalIntakeCommand,
    HistoricalIntakeRefusal,
    HistoricalIntakeRefused,
    HistoricalIntakeValidationFailed,
)
from .completion_intake import (
    CompletionIntakeEntry,
    CompletionIntakeError,
    CompletionValidationAttestation,
)
from .models import CompletionRecord, CompletionOutcome
from .validated_work import (
    AdmittedArtifact,
    ArtifactSlot,
    IDENTITY_SCHEMA_VERSION,
    ReviewDisposition,
    ValidatedWorkEvidence,
    ValidatedWorkIdentity,
    ValidatedWorkKey,
    ValidatedWorkObservations,
)


def repository_refusal(
    command: HistoricalIntakeCommand, configured_repo: str | None
) -> HistoricalIntakeRefused | None:
    if configured_repo is None:
        return HistoricalIntakeRefused(HistoricalIntakeRefusal.PREREQUISITE_UNAVAILABLE)
    if command.repo_slug != configured_repo:
        return HistoricalIntakeRefused(HistoricalIntakeRefusal.WRONG_REPOSITORY)
    return None


def completion_refusal(record: CompletionRecord) -> HistoricalIntakeRefused | None:
    if (
        record.outcome is not CompletionOutcome.COMPLETED
        or not record.requests_publication
    ):
        return HistoricalIntakeRefused(HistoricalIntakeRefusal.INVALID_COMPLETION)
    return None


def failed_historical_validation(
    entry_id: str, attestation: CompletionValidationAttestation
) -> HistoricalIntakeValidationFailed | None:
    if not attestation.passed:
        return HistoricalIntakeValidationFailed(
            entry_id, attestation.result_sha256, str(attestation.result_path)
        )
    return None


def require_historical_attestation(
    command: HistoricalIntakeCommand,
    registered: HistoricalIntakeCommand,
    entry: CompletionIntakeEntry,
    attestation: CompletionValidationAttestation | None,
) -> CompletionValidationAttestation:
    if command != registered:
        raise CompletionIntakeError(
            "historical command does not match registered receipt"
        )
    if attestation is None or attestation.head_sha != command.target_head_sha:
        raise CompletionIntakeError(
            "historical receipt lacks exact selected validation"
        )
    if (
        entry.normalized_path is None
        or entry.normalized_sha256 is None
        or not attestation.passed
    ):
        raise CompletionIntakeError(
            "historical admission requires validated normalized evidence"
        )
    return attestation


def historical_admission_evidence(
    command: HistoricalIntakeCommand,
    entry: CompletionIntakeEntry,
    validation: CompletionValidationAttestation,
    completion_bytes: bytes,
    validation_bytes: bytes,
) -> ValidatedWorkEvidence:
    if (
        sha256(completion_bytes).hexdigest() != entry.normalized_sha256
        or sha256(validation_bytes).hexdigest() != validation.result_sha256
    ):
        raise CompletionIntakeError("historical evidence changed before admission")
    assert entry.normalized_sha256 is not None
    record = CompletionRecord.from_dict(json.loads(completion_bytes))
    key = ValidatedWorkKey(
        command.repo_slug,
        command.issue_number,
        command.branch_name,
        command.target_head_sha,
    )
    identity = ValidatedWorkIdentity(
        IDENTITY_SCHEMA_VERSION,
        key,
        entry.run.identity,
        AdmittedArtifact(
            ArtifactSlot.COMPLETION, entry.normalized_sha256, len(completion_bytes)
        ),
        AdmittedArtifact(
            ArtifactSlot.VALIDATION, validation.result_sha256, len(validation_bytes)
        ),
        None,
        tuple(record.requested_actions),
        ReviewDisposition.ROUTE_TO_PR_REVIEW,
        None,
        True,
        None,
    )
    evidence = ValidatedWorkEvidence(
        identity,
        ValidatedWorkObservations(
            captured_at=validation.recorded_at,
            worktree_head_sha=command.target_head_sha,
            expected_remote_head_sha=None,
            pr_number=None,
            observed_blocking_labels=(),
            admitted_from_paths={
                ArtifactSlot.COMPLETION: str(entry.normalized_path),
                ArtifactSlot.VALIDATION: str(validation.result_path),
            },
        ),
    )
    return evidence
