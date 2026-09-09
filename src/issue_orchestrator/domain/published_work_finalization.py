"""Staged finalization contracts; label effects never prove remote recovery."""

from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import urlsplit

from .models import OrchestratorState
from .retry_review_routing import RetryReviewRouting
from .validated_work import (
    FinalizationPhase,
    ReviewDisposition,
    ValidatedWorkFailure,
    ValidatedWorkKey,
    require_positive,
    require_text,
)
from .validated_work_claim import ValidatedWorkClaim
from .validated_work_execution import RecordExecutionToken


@dataclass(frozen=True, slots=True)
class PublishedWorkTarget:
    key: ValidatedWorkKey
    pr_number: int
    pr_url: str
    review_disposition: ReviewDisposition

    def __post_init__(self) -> None:
        require_positive(self.pr_number, "pr_number")
        if type(self.review_disposition) is not ReviewDisposition:
            raise ValueError("review disposition must be typed")
        url = urlsplit(self.pr_url)
        if (
            url.scheme not in {"http", "https"}
            or not url.netloc
            or url.path.rstrip("/") != f"/{self.key.repo_slug}/pull/{self.pr_number}"
            or url.query
            or url.fragment
        ):
            raise ValueError("PR URL must name the target repository and PR")


@dataclass(frozen=True, slots=True)
class PublishedWorkFinalizationRequest:
    state: OrchestratorState
    execution_token: RecordExecutionToken
    claim: ValidatedWorkClaim
    resume_from: FinalizationPhase
    target: PublishedWorkTarget
    routing: RetryReviewRouting
    issue_title: str
    agent_label: str | None
    history_reason: str
    recovery_label: str
    observed_blocking_labels: tuple[str, ...]
    worktree_path: str | None

    def __post_init__(self) -> None:
        if type(self.resume_from) is not FinalizationPhase:
            raise ValueError("resume phase must be typed")
        if self.claim.record_id != self.target.key.record_id:
            raise ValueError("claim must name the published work")
        if self.routing.branch_name != self.target.key.branch_name:
            raise ValueError("routing must preserve the published branch")
        approved = self.target.review_disposition is ReviewDisposition.EXCHANGE_APPROVED
        if self.routing.review_exchange_completed != approved:
            raise ValueError("routing must preserve the admitted review disposition")
        if approved and self.routing.review_exchange_halted:
            raise ValueError("an approved exchange cannot also be halted")
        require_text(self.history_reason, "history_reason")
        require_text(self.recovery_label, "recovery_label")
        _labels(self.observed_blocking_labels)

    def require_routing_label(self, label: str) -> None:
        """Routing cannot also be one of the blocks this publication releases."""
        require_text(label, "routing_label")
        if label in (self.recovery_label, *self.observed_blocking_labels):
            raise ValueError("routing label must be distinct from released blockers")


@dataclass(frozen=True, slots=True)
class FinalizationCheckpoint:
    """Store-authenticated phase and matching routing failure, read atomically."""

    phase: FinalizationPhase
    failure: ValidatedWorkFailure | None
    message: str

    def __post_init__(self) -> None:
        if type(self.phase) is not FinalizationPhase:
            raise ValueError("checkpoint phase must be typed")
        if self.failure is not None and (
            self.failure is not ValidatedWorkFailure.REVIEW_ROUTING_FAILED
            or self.phase is not FinalizationPhase.NOT_STARTED
        ):
            raise ValueError(
                "only an unrouted routing failure is a finalization checkpoint"
            )
        require_text(self.message, "message")


class FinalizationStatus(StrEnum):
    FINALIZED = "finalized"
    TRANSIENT = "transient"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class FinalizationOutcome:
    status: FinalizationStatus
    phase_reached: FinalizationPhase
    review_disposition: ReviewDisposition
    labels_added: tuple[str, ...]
    labels_removed: tuple[str, ...]
    failure: ValidatedWorkFailure | None
    message: str

    def __post_init__(self) -> None:
        if (
            type(self.status) is not FinalizationStatus
            or type(self.phase_reached) is not FinalizationPhase
            or type(self.review_disposition) is not ReviewDisposition
        ):
            raise ValueError(
                "finalization outcome requires typed status, phase and review"
            )
        if (self.status is FinalizationStatus.FAILED) != (self.failure is not None):
            raise ValueError("failure is required exactly for durable FAILED")
        if (
            self.failure is not None
            and self.failure is not ValidatedWorkFailure.REVIEW_ROUTING_FAILED
        ):
            raise ValueError("only review routing failure belongs to the finalizer")
        if self.status is FinalizationStatus.FINALIZED and self.phase_reached not in {
            FinalizationPhase.RECOVERY_CLEARED,
            FinalizationPhase.COMPLETE,
        }:
            raise ValueError("finalized requires durable recovery release")
        if (
            self.status is FinalizationStatus.FAILED
            and self.phase_reached is not FinalizationPhase.NOT_STARTED
        ):
            raise ValueError("routing failure cannot follow durable routing")
        require_text(self.message, "message")
        _labels(self.labels_added)
        _labels(self.labels_removed)


@dataclass(frozen=True, slots=True)
class RecoveryBlockReleaseRequest:
    """Release ONLY this record's interest, under the aggregate owner's issue gate."""

    execution_token: RecordExecutionToken
    claim: ValidatedWorkClaim
    target: PublishedWorkTarget
    phase: FinalizationPhase
    recovery_label: str
    observed_blocking_labels: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.claim.record_id != self.target.key.record_id:
            raise ValueError("release must name the current record")
        if self.phase is not FinalizationPhase.REVIEW_ROUTED:
            raise ValueError("only durably routed publication can release its interest")
        require_text(self.recovery_label, "recovery_label")
        _labels(self.observed_blocking_labels)

    def require_context(self, repo_slug: str, recovery_label: str) -> None:
        if (
            self.target.key.repo_slug != repo_slug
            or self.recovery_label != recovery_label
        ):
            raise ValueError("release names another repository or recovery label")


class RecoveryBlockReleaseStatus(StrEnum):
    RELEASED = "released"  # own interest released; siblings may still hold labels
    REFUSED = "refused"  # gate busy, stale authority, or projection not completed


@dataclass(frozen=True, slots=True)
class RecoveryBlockReleaseOutcome:
    record_id: str
    status: RecoveryBlockReleaseStatus
    labels_removed: tuple[str, ...]
    message: str

    def __post_init__(self) -> None:
        require_text(self.record_id, "record_id")
        if type(self.status) is not RecoveryBlockReleaseStatus:
            raise ValueError("release status must be typed")
        _labels(self.labels_removed)
        require_text(self.message, "message")

    @property
    def phase_reached(self) -> FinalizationPhase:
        """Effect completion only; the finalizer must still commit this phase."""
        if self.status is RecoveryBlockReleaseStatus.RELEASED:
            return FinalizationPhase.RECOVERY_CLEARED
        return FinalizationPhase.REVIEW_ROUTED


def _labels(labels: tuple[str, ...]) -> None:
    if type(labels) is not tuple:
        raise ValueError("labels must be an immutable tuple")
    for label in labels:
        require_text(label, "label")
    if len(set(labels)) != len(labels):
        raise ValueError("label effects must be unique")
