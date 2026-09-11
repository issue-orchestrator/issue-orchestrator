"""One owner for durable pattern case-file disposition and retirement."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

from ..domain.tech_lead_findings import CaseFileLifecycleTransition
from ..ports.pattern_registry import (
    PatternCaseFileRegistry,
    PatternRegistryError,
    PatternReservation,
    PatternReservationState,
    PatternRetirementPhase,
)
from .comment_publication import ensure_comment_published
from .tech_lead_case_file_owner import AmbiguousPatternPublicationError

if TYPE_CHECKING:
    from ..ports import RepositoryHost
    from ..ports.pattern_registry import PatternRegistryEntry


@dataclass(frozen=True)
class CaseFileRetirementOutcome:
    issue_number: int
    transition: CaseFileLifecycleTransition
    deduplicated: bool


def lifecycle_marker(transition_id: str) -> str:
    digest = hashlib.sha256(transition_id.encode()).hexdigest()
    return f"<!-- issue-orchestrator:tech-lead-case-lifecycle:v1:{digest} -->"


def retirement_comment(transition: CaseFileLifecycleTransition) -> str:
    evidence = "\n".join(f"- {item}" for item in transition.evidence)
    return (
        f"## Pattern case file retired: `{transition.disposition}`\n\n"
        f"{transition.reason}\n\n"
        f"Evidence:\n{evidence}\n\n"
        "The Tech Lead pattern registry retains this signature and its canonical"
        " issue after closure. Later sightings append here and cannot create a"
        " replacement case file. Reopening requires a separate reviewed lifecycle"
        " transition.\n\n"
        f"{lifecycle_marker(transition.transition_id)}"
    )


class PatternCaseFileLifecycleOwner:
    """Coordinate registry authority with comment-before-close GitHub effects."""

    def __init__(
        self,
        *,
        registry: PatternCaseFileRegistry,
        repository_host: "RepositoryHost",
        before_write: Callable[[], None] = lambda: None,
    ) -> None:
        self._registry = registry
        self._repository = repository_host
        self._before_write = before_write

    def classify(
        self, *, signature: str, transition: CaseFileLifecycleTransition
    ) -> "PatternRegistryEntry":
        """Record a reviewed active/needs-human outcome without closing GitHub."""
        self._before_write()
        return self._registry.record_lifecycle(
            signature=signature, transition=transition
        )

    def retire(
        self, *, signature: str, transition: CaseFileLifecycleTransition
    ) -> CaseFileRetirementOutcome:
        """Publish evidence, close idempotently, then commit terminal authority."""
        comment = retirement_comment(transition)
        self._before_write()
        reservation = self._registry.reserve_retirement(
            signature=signature, transition=transition, comment=comment
        )
        if reservation.state is PatternReservationState.COMMITTED:
            return self._outcome(reservation.entry, transition, deduplicated=True)
        reservation = self._recover_or_acquire(reservation)
        entry = reservation.entry
        pending = entry.pending_retirement
        if pending is None:
            return self._outcome(entry, transition, deduplicated=True)

        if pending.phase is PatternRetirementPhase.COMMENT:
            entry = self._publish_comment(reservation)
            pending = entry.pending_retirement
            if pending is None:
                return self._outcome(entry, transition, deduplicated=True)

        assert entry.issue_number is not None
        self._before_write()
        self._repository.update_issue_state(entry.issue_number, "closed")
        self._before_write()
        committed = self._registry.finalize_retirement(
            signature=signature, reservation_id=entry.reservation_id
        )
        return self._outcome(committed, transition, deduplicated=False)

    def _recover_or_acquire(
        self, reservation: PatternReservation
    ) -> PatternReservation:
        if reservation.state is PatternReservationState.HELD:
            raise PatternRegistryError(
                f"pattern {reservation.entry.signature!r} lifecycle is owned by"
                f" {reservation.entry.claimant_id}; retry after"
                f" {reservation.entry.expires_at}"
            )
        if reservation.state is PatternReservationState.PUBLISHING:
            self._require_comment_receipt(reservation.entry)
            return PatternReservation(PatternReservationState.RECOVERABLE, reservation.entry)
        if reservation.state is PatternReservationState.RECOVERABLE:
            pending = reservation.entry.pending_retirement
            if pending is None or pending.phase is PatternRetirementPhase.CLOSE:
                return reservation
            if self._find_receipt(reservation.entry) is not None:
                return reservation
            self._before_write()
            takeover = self._registry.take_over_retirement(
                signature=reservation.entry.signature,
                stale_reservation_id=reservation.entry.reservation_id,
            )
            if takeover.state is not PatternReservationState.ACQUIRED:
                raise PatternRegistryError(
                    f"pattern {reservation.entry.signature!r} retirement changed"
                    " during recovery"
                )
            return takeover
        return reservation

    def _publish_comment(
        self, reservation: PatternReservation
    ) -> "PatternRegistryEntry":
        entry = reservation.entry
        pending = entry.pending_retirement
        assert pending is not None and entry.issue_number is not None
        if reservation.state is PatternReservationState.RECOVERABLE:
            receipt = self._find_receipt(entry)
            if receipt is None:
                raise AmbiguousPatternPublicationError(
                    f"pattern {entry.signature!r} retirement publication started at"
                    f" {entry.publication_started_at}, but its marker is not yet"
                    " observable; preserving publication state"
                )
        else:
            self._before_write()
            started = self._registry.begin_retirement_publication(
                signature=entry.signature, reservation_id=entry.reservation_id
            )
            if started.state is not PatternReservationState.ACQUIRED:
                raise PatternRegistryError(
                    f"pattern {entry.signature!r} retirement reservation changed"
                    " before publication"
                )
            entry = started.entry
            pending = entry.pending_retirement
            assert pending is not None and entry.issue_number is not None
            ensure_comment_published(
                entry.issue_number,
                pending.comment,
                find_receipt=lambda number, body: self._repository.find_issue_comment_receipt(
                    number, body=body
                ),
                post_comment=self._repository.add_comment,
                before_write=self._before_write,
            )
        self._before_write()
        return self._registry.confirm_retirement_comment(
            signature=entry.signature, reservation_id=entry.reservation_id
        )

    def _find_receipt(self, entry: "PatternRegistryEntry") -> object | None:
        pending = entry.pending_retirement
        assert pending is not None and entry.issue_number is not None
        return self._repository.find_issue_comment_receipt(
            entry.issue_number, body=pending.comment
        )

    def _require_comment_receipt(self, entry: "PatternRegistryEntry") -> None:
        if self._find_receipt(entry) is None:
            raise AmbiguousPatternPublicationError(
                f"pattern {entry.signature!r} retirement publication started at"
                f" {entry.publication_started_at}, but its marker is not yet"
                " observable; preserving publication state"
            )

    @staticmethod
    def _outcome(
        entry: "PatternRegistryEntry",
        transition: CaseFileLifecycleTransition,
        *,
        deduplicated: bool,
    ) -> CaseFileRetirementOutcome:
        if entry.issue_number is None:
            raise PatternRegistryError(
                f"pattern {entry.signature!r} has no canonical case file"
            )
        return CaseFileRetirementOutcome(
            issue_number=entry.issue_number,
            transition=transition,
            deduplicated=deduplicated,
        )
