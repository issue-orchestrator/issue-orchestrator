"""Shared authority for Tech Lead pattern case-file identity and evidence."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from ..domain.tech_lead_findings import (
    CASE_FILE_ACTIVE,
    CaseFileClassification,
    CaseFileDisposition,
    CaseFileLifecycleTransition,
    PatternObservation,
    PendingCaseFile,
)


class PatternRegistryError(RuntimeError):
    """The registry could not safely answer or commit a request."""


class PatternReservationState(Enum):
    ACQUIRED = "acquired"
    COMMITTED = "committed"
    HELD = "held"
    RECOVERABLE = "recoverable"
    PUBLISHING = "publishing"


@dataclass(frozen=True)
class PendingPatternObservation:
    """Evidence admitted by shared authority but not yet finalized."""

    observation: PatternObservation
    classification: CaseFileClassification


class PatternRetirementPhase(Enum):
    COMMENT = "comment"
    CLOSE = "close"


@dataclass(frozen=True)
class PendingPatternRetirement:
    """A terminal lifecycle transition whose GitHub effects are in flight."""

    transition: CaseFileLifecycleTransition
    comment: str
    phase: PatternRetirementPhase = PatternRetirementPhase.COMMENT

    def __post_init__(self) -> None:
        if not self.transition.terminal:
            raise ValueError("only a terminal disposition may retire a case file")
        if not self.comment.strip():
            raise ValueError("pattern retirement requires a non-empty comment")


@dataclass(frozen=True)
class PatternRegistryEntry:
    """The complete shared record for one pattern signature."""

    signature: str
    reservation_id: str
    claimant_id: str
    expires_at: str
    pending: PendingCaseFile | None
    issue_number: int | None
    observation_ids: tuple[str, ...]
    classification: CaseFileClassification
    pending_observation: PendingPatternObservation | None = None
    lifecycle: tuple[CaseFileLifecycleTransition, ...] = ()
    pending_retirement: PendingPatternRetirement | None = None
    publication_started_at: str | None = None

    def __post_init__(self) -> None:
        for name in ("signature", "reservation_id", "claimant_id", "expires_at"):
            if not str(getattr(self, name)).strip():
                raise ValueError(f"pattern registry entry requires {name}")
        if self.issue_number is None:
            if self.pending is None or self.observation_ids or self.pending_observation:
                raise ValueError(
                    "a reserved pattern requires pending data and no observations"
                )
        elif (
            self.issue_number <= 0
            or not self.observation_ids
            or self.pending is not None
        ):
            raise ValueError(
                "a committed pattern requires a positive issue, observations,"
                " and no creation intent"
            )
        if len(set(self.observation_ids)) != len(self.observation_ids):
            raise ValueError("pattern observation identities must be unique")
        if self.pending is not None and self.pending.signature != self.signature:
            raise ValueError("pending case-file signature disagrees with registry key")
        self._validate_lifecycle()
        has_pending_effect = self.pending is not None or any(
            (self.pending_observation is not None, self.pending_retirement is not None)
        )
        if self.publication_started_at is not None and not has_pending_effect:
            raise ValueError("publication state requires a pending external effect")

    def _validate_lifecycle(self) -> None:
        transition_ids = tuple(item.transition_id for item in self.lifecycle)
        if len(set(transition_ids)) != len(transition_ids):
            raise ValueError("case-file lifecycle transition identities must be unique")
        if self.pending_retirement is not None and self.issue_number is None:
            raise ValueError("only a committed pattern can be retired")
        if self.pending_observation is not None and self.pending_retirement is not None:
            raise ValueError("pattern observation and retirement cannot be pending together")
        if (
            self.pending_retirement is not None
            and self.lifecycle
            and self.lifecycle[-1].terminal
        ):
            raise ValueError("a terminal pattern cannot have a pending retirement")

    @property
    def committed(self) -> bool:
        return self.issue_number is not None

    @property
    def disposition(self) -> CaseFileDisposition:
        return self.lifecycle[-1].disposition if self.lifecycle else CASE_FILE_ACTIVE


@dataclass(frozen=True)
class PatternReservation:
    """Outcome of reserving one signature's create boundary."""

    state: PatternReservationState
    entry: PatternRegistryEntry


class PatternCaseFileRegistry(Protocol):
    """Atomic, cross-client owner of one case file and its evidence per signature."""

    def reserve(self, pending: PendingCaseFile) -> PatternReservation:
        """Reserve creation, return the canonical entry, or expose stale recovery."""
        ...

    def take_over(
        self, *, stale_reservation_id: str, pending: PendingCaseFile
    ) -> PatternReservation:
        """Replace the exact stale reservation after remote absence is proven."""
        ...

    def finalize(
        self, *, signature: str, reservation_id: str, issue_number: int
    ) -> PatternRegistryEntry:
        """Bind the reserved signature to its canonical GitHub issue."""
        ...

    def begin_creation_publication(
        self, *, signature: str, reservation_id: str
    ) -> PatternReservation:
        """Durably enter the non-expiring ambiguous-write phase for creation."""
        ...

    def reserve_observation(
        self,
        *,
        signature: str,
        observation: PatternObservation,
        classification: CaseFileClassification,
    ) -> PatternReservation:
        """Admit one exact evidence publication through shared authority."""
        ...

    def take_over_observation(
        self, *, signature: str, stale_reservation_id: str
    ) -> PatternReservation:
        """Fence an exact recoverable evidence reservation for this client."""
        ...

    def begin_observation_publication(
        self, *, signature: str, reservation_id: str
    ) -> PatternReservation:
        """Durably enter the non-expiring ambiguous-write phase for evidence."""
        ...

    def finalize_observation(self, *, signature: str, reservation_id: str) -> bool:
        """Commit one admitted observation after its comment is durable."""
        ...

    def record_lifecycle(
        self, *, signature: str, transition: CaseFileLifecycleTransition
    ) -> PatternRegistryEntry:
        """Record an active/needs-human classification without GitHub mutation."""
        ...

    def reserve_retirement(
        self,
        *,
        signature: str,
        transition: CaseFileLifecycleTransition,
        comment: str,
    ) -> PatternReservation:
        """Reserve one exact terminal transition and its evidence comment."""
        ...

    def take_over_retirement(
        self, *, signature: str, stale_reservation_id: str
    ) -> PatternReservation:
        """Fence an expired retirement before its remote comment began."""
        ...

    def begin_retirement_publication(
        self, *, signature: str, reservation_id: str
    ) -> PatternReservation:
        """Make an ambiguous retirement comment permanently non-reissuable."""
        ...

    def confirm_retirement_comment(
        self, *, signature: str, reservation_id: str
    ) -> PatternRegistryEntry:
        """Advance a verified comment to the idempotent close phase."""
        ...

    def finalize_retirement(
        self, *, signature: str, reservation_id: str
    ) -> PatternRegistryEntry:
        """Commit the terminal disposition after the case file is closed."""
        ...

    def has_observation(self, *, signature: str, observation_id: str) -> bool:
        """Report whether one exact observation is already authoritative."""
        ...

    def read(self, *, signature: str) -> PatternRegistryEntry | None:
        """Read one exact signature mapping."""
        ...

    def list_entries(self) -> tuple[PatternRegistryEntry, ...]:
        """Read the bounded shared registry for restart reconstruction."""
        ...

    def seed_committed(self, entries: tuple[PatternRegistryEntry, ...]) -> None:
        """Merge trusted pre-registry local rows during rolling upgrade."""
        ...
