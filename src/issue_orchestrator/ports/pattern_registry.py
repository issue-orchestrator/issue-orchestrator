"""Shared authority for Tech Lead pattern case-file identity and evidence."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from ..domain.tech_lead_findings import (
    CaseFileClassification,
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


@dataclass(frozen=True)
class PendingPatternObservation:
    """Evidence admitted by shared authority but not yet finalized."""

    observation: PatternObservation
    classification: CaseFileClassification


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

    def __post_init__(self) -> None:
        for name in ("signature", "reservation_id", "claimant_id", "expires_at"):
            if not str(getattr(self, name)).strip():
                raise ValueError(f"pattern registry entry requires {name}")
        if self.issue_number is None:
            if self.pending is None or self.observation_ids or self.pending_observation:
                raise ValueError("a reserved pattern requires pending data and no observations")
        elif self.issue_number <= 0 or not self.observation_ids or self.pending is not None:
            raise ValueError(
                "a committed pattern requires a positive issue, observations,"
                " and no creation intent"
            )
        if len(set(self.observation_ids)) != len(self.observation_ids):
            raise ValueError("pattern observation identities must be unique")
        if self.pending is not None and self.pending.signature != self.signature:
            raise ValueError("pending case-file signature disagrees with registry key")

    @property
    def committed(self) -> bool:
        return self.issue_number is not None


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

    def renew_creation(
        self, *, signature: str, reservation_id: str
    ) -> PatternReservation:
        """Fence and renew the exact reservation at the publication boundary."""
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

    def renew_observation(
        self, *, signature: str, reservation_id: str
    ) -> PatternReservation:
        """Fence the exact evidence token at the comment publication boundary."""
        ...

    def finalize_observation(
        self, *, signature: str, reservation_id: str
    ) -> bool:
        """Commit one admitted observation after its comment is durable."""
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
