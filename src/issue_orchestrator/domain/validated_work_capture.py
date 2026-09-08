"""Exact candidate grouping and immutable evidence construction."""

from hashlib import sha256
from .prepared_completion import PreparedCompletionEvidence
from .validated_work import (
    AdmittedArtifact, ArtifactSlot, IDENTITY_SCHEMA_VERSION, ReviewDisposition,
    ValidatedWorkEvidence, ValidatedWorkIdentity, ValidatedWorkKey, ValidatedWorkObservations,
)
from .completion_intake import CompletionIntakeError


def candidate_key(candidate: PreparedCompletionEvidence, issue_number: int) -> ValidatedWorkKey:
    branch = candidate.run.branch_name
    if branch is None:
        raise CompletionIntakeError("exact run has no recorded branch binding")
    return ValidatedWorkKey(candidate.run.session_key.issue.scope(), issue_number, branch, candidate.validation.head_sha)


def newest_per_work(candidates: tuple[PreparedCompletionEvidence, ...], issue_number: int) -> tuple[PreparedCompletionEvidence, ...]:
    selected: dict[ValidatedWorkKey, PreparedCompletionEvidence] = {}
    for candidate in candidates:
        key = candidate_key(candidate, issue_number)
        previous = selected.get(key)
        if previous is None or candidate.entry.receive_sequence > previous.entry.receive_sequence:
            selected[key] = candidate
    return tuple(sorted(selected.values(), key=lambda value: value.entry.receive_sequence))


def candidate_evidence(candidate: PreparedCompletionEvidence, *, issue_number: int, head: str, branch_verified: bool, captured_at: str) -> ValidatedWorkEvidence:
    entry, validation = candidate.entry, candidate.validation
    return ValidatedWorkEvidence(
        ValidatedWorkIdentity(
            IDENTITY_SCHEMA_VERSION, candidate_key(candidate, issue_number), entry.run.identity,
            AdmittedArtifact(ArtifactSlot.COMPLETION, sha256(candidate.completion_bytes).hexdigest(), len(candidate.completion_bytes)),
            AdmittedArtifact(ArtifactSlot.VALIDATION, sha256(candidate.validation_bytes).hexdigest(), len(candidate.validation_bytes)),
            None, candidate.requested_actions, ReviewDisposition.ROUTE_TO_PR_REVIEW,
            None, branch_verified, None,
        ),
        ValidatedWorkObservations(captured_at, head, None, None, (), {
            ArtifactSlot.COMPLETION: str(entry.normalized_path),
            ArtifactSlot.VALIDATION: str(validation.result_path),
        }),
    )
