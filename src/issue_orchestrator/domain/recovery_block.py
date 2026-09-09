"""Issue-wide recovery interests, read together from retained dispositions."""

from dataclasses import dataclass
from enum import Enum

from .validated_work import (
    FinalizationPhase,
    ValidatedWorkState,
    require_positive,
    require_text,
)
from .validated_work_commands import ValidatedWorkDisposition
from .published_work_finalization import (
    FinalizationCheckpoint,
    RecoveryBlockReleaseRequest,
)


@dataclass(frozen=True, slots=True)
class RecoveryCleanupKey:
    record_id: str
    evidence_id: str
    attempt_no: int

    def __post_init__(self) -> None:
        require_text(self.record_id, "record_id")
        require_text(self.evidence_id, "evidence_id")
        if type(self.attempt_no) is not int or self.attempt_no < 0:
            raise ValueError("cleanup attempt number must be nonnegative")


@dataclass(frozen=True, slots=True)
class RecoveryBlockInterest:
    disposition: ValidatedWorkDisposition
    phase: FinalizationPhase
    observed_blocking_labels: tuple[str, ...]
    cleanup_key: RecoveryCleanupKey
    cleanup_acknowledged: bool

    def __post_init__(self) -> None:
        if type(self.phase) is not FinalizationPhase:
            raise ValueError("recovery interest requires a typed phase")
        if type(self.observed_blocking_labels) is not tuple:
            raise ValueError("captured blocking labels must be immutable")
        for label in self.observed_blocking_labels:
            require_text(label, "blocking label")
        if (
            self.cleanup_key.record_id != self.disposition.record_id
            or self.cleanup_key.evidence_id != self.disposition.evidence_id
        ):
            raise ValueError("cleanup key differs from retained evidence")

    @property
    def holds_recovery(self) -> bool:
        released = (
            self.disposition.state is ValidatedWorkState.PUBLISHING
            and self.phase
            in {
                FinalizationPhase.RECOVERY_CLEARED,
                FinalizationPhase.COMPLETE,
            }
        )
        return self.disposition.unresolved and not released

    @property
    def needs_human(self) -> bool:
        return self.disposition.state is ValidatedWorkState.FAILED

    @property
    def permits_captured_cleanup(self) -> bool:
        return self.disposition.state is ValidatedWorkState.RECOVERED or (
            self.disposition.state is ValidatedWorkState.PUBLISHING
            and not self.holds_recovery
        )


@dataclass(frozen=True, slots=True)
class RecoveryBlockSnapshot:
    repo_slug: str
    issue_number: int
    interests: tuple[RecoveryBlockInterest, ...]

    def __post_init__(self) -> None:
        require_text(self.repo_slug, "repo_slug")
        require_positive(self.issue_number, "issue_number")
        if type(self.interests) is not tuple:
            raise ValueError("recovery interests must be immutable")
        ids = set()
        for interest in self.interests:
            key = interest.disposition.key
            if key.repo_slug != self.repo_slug or key.issue_number != self.issue_number:
                raise ValueError(
                    "recovery interest belongs to another repository or issue"
                )
            if key.record_id in ids:
                raise ValueError("duplicate recovery interest")
            ids.add(key.record_id)

    def plan(self) -> "RecoveryBlockPlan":
        return self._plan(releasing="")

    def release_plan(
        self,
        request: RecoveryBlockReleaseRequest,
        checkpoint: FinalizationCheckpoint | None,
    ) -> "RecoveryBlockPlan":
        if (
            checkpoint is None
            or checkpoint.phase is not FinalizationPhase.REVIEW_ROUTED
            or checkpoint.failure is not None
        ):
            raise ValueError("release lacks durable routed publication")
        matching = [
            item
            for item in self.interests
            if item.disposition.record_id == request.claim.record_id
        ]
        if len(matching) != 1:
            raise ValueError("release record absent from aggregate")
        current = matching[0]
        if (
            current.disposition.key != request.target.key
            or current.disposition.state is not ValidatedWorkState.PUBLISHING
            or current.phase is not FinalizationPhase.REVIEW_ROUTED
            or current.observed_blocking_labels != request.observed_blocking_labels
        ):
            raise ValueError("release differs from retained publication evidence")
        return self._plan(releasing=request.claim.record_id)

    def _plan(self, *, releasing: str) -> "RecoveryBlockPlan":
        completed = tuple(
            item
            for item in self.interests
            if item.permits_captured_cleanup or item.disposition.record_id == releasing
        )
        return RecoveryBlockPlan(
            self.issue_number,
            bool(self.interests),
            any(
                item.holds_recovery and item.disposition.record_id != releasing
                for item in self.interests
            ),
            tuple(item for item in self.interests if item.needs_human),
            tuple(
                item
                for item in self.interests
                if item in completed
                or item.disposition.state is ValidatedWorkState.ABANDONED
            ),
            frozenset(
                label
                for item in completed
                if not item.cleanup_acknowledged
                for label in item.observed_blocking_labels
            ),
            tuple(
                item.cleanup_key for item in completed if not item.cleanup_acknowledged
            ),
        )


@dataclass(frozen=True, slots=True)
class RecoveryBlockPlan:
    issue_number: int
    has_records: bool
    recovery_required: bool
    assert_human: tuple[RecoveryBlockInterest, ...]
    withdraw_human: tuple[RecoveryBlockInterest, ...]
    captured_labels: frozenset[str]
    cleanup_keys: tuple[RecoveryCleanupKey, ...]


class RecoveryBlockReconcileStatus(Enum):
    RECONCILED = "reconciled"
    BUSY = "busy"
    RETRY = "retry"


class RecoveryAdmissionDeferred(RuntimeError):
    """Admission cannot acknowledge custody until its aggregate block is observed."""


class RecoveryMutationBusy(RecoveryAdmissionDeferred):
    """Another issue operation owns the gate; no work was admitted."""


@dataclass(frozen=True, slots=True)
class RecoveryBlockReconcileOutcome:
    status: RecoveryBlockReconcileStatus
    labels_removed: tuple[str, ...]
    message: str


def require_cleanup_absence(label: str, observed: tuple[str, ...]) -> None:
    """A prior removal intent authorizes observation, never a second deletion."""
    if label in observed:
        raise RuntimeError(
            f"ambiguous prior removal of {label}; preserving current label "
            "until its owner resolves it"
        )
