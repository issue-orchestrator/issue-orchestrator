"""Exact candidate grouping, remote facts, and immutable evidence construction."""

from dataclasses import dataclass
from hashlib import sha256
from .publication_remote import PublicationPullRequest
from .prepared_completion import PreparedCompletionEvidence
from .validated_work import (
    AdmittedArtifact, ArtifactSlot, IDENTITY_SCHEMA_VERSION, ReviewDisposition,
    RemoteBaselineStatus, ValidatedWorkEvidence, ValidatedWorkIdentity,
    ValidatedWorkKey, ValidatedWorkObservations, require_positive, require_sha,
    require_text,
    ValidatedWorkFailure, ValidatedWorkState,
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


@dataclass(frozen=True, slots=True)
class ValidatedWorkRemoteRequest:
    repo_slug: str
    issue_number: int
    branch_name: str

    def __post_init__(self) -> None:
        if len(self.repo_slug.split("/")) != 2 or not all(self.repo_slug.split("/")):
            raise ValueError("capture repository must be owner/name")
        require_positive(self.issue_number, "issue_number")
        require_text(self.branch_name, "branch_name")


@dataclass(frozen=True, slots=True)
class ValidatedWorkRemoteFacts:
    """One complete uncached branch/PR observation; construction proves readability."""

    branch_head_sha: str | None
    pull_requests: tuple[PublicationPullRequest, ...]

    def __post_init__(self) -> None:
        if self.branch_head_sha is not None:
            require_sha(self.branch_head_sha)
        if type(self.pull_requests) is not tuple or any(
            type(pr) is not PublicationPullRequest for pr in self.pull_requests
        ):
            raise ValueError("capture PR facts must be an immutable typed tuple")


@dataclass(frozen=True, slots=True)
class AutomaticCaptureDecision:
    state: ValidatedWorkState
    failure: ValidatedWorkFailure | None
    reason: str

    def __post_init__(self) -> None:
        if self.state not in {ValidatedWorkState.QUEUED, ValidatedWorkState.PARKED}:
            raise ValueError("automatic capture must queue or park")
        if type(self.state) is not ValidatedWorkState:
            raise ValueError("automatic capture state must be typed")
        if self.failure is not None and type(self.failure) is not ValidatedWorkFailure:
            raise ValueError("automatic capture failure must be typed")
        if self.state is ValidatedWorkState.QUEUED and self.failure is not None:
            raise ValueError("queued automatic capture cannot carry a failure")
        require_text(self.reason, "automatic capture reason")


def candidate_evidence(candidate: PreparedCompletionEvidence, *, issue_number: int,
                       head: str, branch_verified: bool, captured_at: str,
                       remote_baseline_status: RemoteBaselineStatus = RemoteBaselineStatus.UNOBSERVED,
                       expected_remote_head_sha: str | None = None,
                       pr_number: int | None = None) -> ValidatedWorkEvidence:
    entry, validation = candidate.entry, candidate.validation
    return ValidatedWorkEvidence(
        ValidatedWorkIdentity(
            IDENTITY_SCHEMA_VERSION, candidate_key(candidate, issue_number), entry.run.identity,
            AdmittedArtifact(ArtifactSlot.COMPLETION, sha256(candidate.completion_bytes).hexdigest(), len(candidate.completion_bytes)),
            AdmittedArtifact(ArtifactSlot.VALIDATION, sha256(candidate.validation_bytes).hexdigest(), len(candidate.validation_bytes)),
            None, candidate.requested_actions, ReviewDisposition.ROUTE_TO_PR_REVIEW,
            None, branch_verified, None,
        ),
        ValidatedWorkObservations(captured_at, head, expected_remote_head_sha, pr_number, (), {
            ArtifactSlot.COMPLETION: str(entry.normalized_path),
            ArtifactSlot.VALIDATION: str(validation.result_path),
        }, remote_baseline_status),
    )
