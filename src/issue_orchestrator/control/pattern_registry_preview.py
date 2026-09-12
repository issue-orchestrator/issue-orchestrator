"""The non-writing view of case-file authority a preview composes against.

:mod:`.pattern_registry` holds the two registries that OWN durable case-file
authority: the mirrored registry that writes through to shared authority and
its local SQLite replica, and the single-process local registry. Both write on
paths that read like reads, which is exactly why the preview boundary cannot
live among them as one more class with a flag. It lives here, in a module whose
whole content is "reads only", so the guarantee is legible before any method is.
"""

from __future__ import annotations

from typing import NoReturn

from ..domain.tech_lead_findings import (
    CaseFileClassification,
    CaseFileLifecycleTransition,
    PatternObservation,
    PendingCaseFile,
)
from ..ports.pattern_registry import (
    PatternCaseFileRegistry,
    PatternRegistryEntry,
    PatternRegistryError,
    PatternReservation,
)


class ReadOnlyPatternCaseFileRegistry(PatternCaseFileRegistry):
    """A registry view whose writes are unavailable by construction, not by flag.

    THE preview boundary for a command that promised to write nothing. A dry
    run still needs durable authority — it can only tell an operator what an
    apply would do by reading the real registry — but every registry in
    :mod:`.pattern_registry` writes on paths that read like reads. The mirrored
    registry's ``list_entries`` and ``read`` project each committed shared row
    into the local SQLite replica and discard any matching pending create
    intent, and its ``synchronize`` publishes this client's legacy rows to
    shared authority.

    Preview through that mirror is therefore destructive in exactly the case
    the rolling-upgrade seed exists for: when shared authority holds ``{A}`` and
    local evidence still holds ``{A, B}``, suppressing only the seed leaves the
    mirror free to replace local state with ``{A}``, so ``B`` can never be
    published afterwards. Suppressing one write with a boolean is not a
    boundary; the others stay reachable through the read path (#7248 review
    F1/A1).

    This wrapper is the boundary. It forwards the three genuinely read-only
    questions to shared authority and refuses every write method, so a preview
    that ever grows a write fails loudly instead of mutating durable memory.
    """

    def __init__(self, shared: PatternCaseFileRegistry) -> None:
        self._shared = shared

    def read(self, *, signature: str) -> PatternRegistryEntry | None:
        return self._shared.read(signature=signature)

    def list_entries(self) -> tuple[PatternRegistryEntry, ...]:
        return self._shared.list_entries()

    def has_observation(self, *, signature: str, observation_id: str) -> bool:
        return self._shared.has_observation(
            signature=signature, observation_id=observation_id
        )

    def reserve(self, pending: PendingCaseFile) -> PatternReservation:
        self._refuse("reserve")

    def take_over(
        self, *, stale_reservation_id: str, pending: PendingCaseFile
    ) -> PatternReservation:
        self._refuse("take_over")

    def finalize(
        self, *, signature: str, reservation_id: str, issue_number: int
    ) -> PatternRegistryEntry:
        self._refuse("finalize")

    def begin_creation_publication(
        self, *, signature: str, reservation_id: str
    ) -> PatternReservation:
        self._refuse("begin_creation_publication")

    def reserve_observation(
        self,
        *,
        signature: str,
        observation: "PatternObservation",
        classification: CaseFileClassification,
        issue_number: int,
    ) -> PatternReservation:
        self._refuse("reserve_observation")

    def take_over_observation(
        self, *, signature: str, stale_reservation_id: str
    ) -> PatternReservation:
        self._refuse("take_over_observation")

    def begin_observation_publication(
        self, *, signature: str, reservation_id: str
    ) -> PatternReservation:
        self._refuse("begin_observation_publication")

    def finalize_observation(self, *, signature: str, reservation_id: str) -> bool:
        self._refuse("finalize_observation")

    def record_lifecycle(
        self,
        *,
        signature: str,
        transition: CaseFileLifecycleTransition,
        expected_revision: str | None = None,
    ) -> PatternRegistryEntry:
        self._refuse("record_lifecycle")

    def reserve_retirement(
        self,
        *,
        signature: str,
        transition: CaseFileLifecycleTransition,
        comment: str,
        issue_number: int,
        expected_revision: str | None = None,
    ) -> PatternReservation:
        self._refuse("reserve_retirement")

    def take_over_retirement(
        self, *, signature: str, stale_reservation_id: str
    ) -> PatternReservation:
        self._refuse("take_over_retirement")

    def begin_retirement_publication(
        self, *, signature: str, reservation_id: str
    ) -> PatternReservation:
        self._refuse("begin_retirement_publication")

    def confirm_retirement_comment(
        self, *, signature: str, reservation_id: str
    ) -> PatternRegistryEntry:
        self._refuse("confirm_retirement_comment")

    def finalize_retirement(
        self, *, signature: str, reservation_id: str
    ) -> PatternRegistryEntry:
        self._refuse("finalize_retirement")

    def seed_committed(self, entries: tuple[PatternRegistryEntry, ...]) -> None:
        self._refuse("seed_committed")

    @staticmethod
    def _refuse(operation: str) -> NoReturn:
        raise PatternRegistryError(
            f"{operation} is not available on a read-only pattern registry:"
            " this composition previews case-file authority and may not write"
            " to it"
        )
