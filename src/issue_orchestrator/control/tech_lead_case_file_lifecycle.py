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
    TerminalRetirementPolicy,
    require_canonical_case_file,
    resolve_admitted_transition,
)
from .comment_publication import ensure_comment_published
from .tech_lead_case_file_owner import AmbiguousPatternPublicationError

if TYPE_CHECKING:
    from ..ports import RepositoryHost
    from ..ports.pattern_registry import PatternRegistryEntry


@dataclass(frozen=True)
class CaseFileRetirementOutcome:
    """What durable authority recorded for one retirement request.

    ``transition`` is ALWAYS the registry's recorded transition. It differs from
    the request only when ``adopted``: the signature was already terminal under
    another transition and the writer declared
    :attr:`TerminalRetirementPolicy.ADOPT`, so its disposition may differ from
    the one requested (#7345). Callers that project the outcome into their own
    ledgers must read the disposition from here, never from their request.
    """

    issue_number: int
    transition: CaseFileLifecycleTransition
    deduplicated: bool
    adopted: bool = False


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
        self,
        *,
        signature: str,
        transition: CaseFileLifecycleTransition,
        expected_revision: str | None = None,
        expected_signatures: frozenset[str] | None = None,
    ) -> "PatternRegistryEntry":
        """Record a reviewed active/needs-human outcome without closing GitHub."""
        self._before_write()
        return self._registry.record_lifecycle(
            signature=signature,
            transition=transition,
            expected_revision=expected_revision,
            expected_signatures=expected_signatures,
        )

    def retire(
        self,
        *,
        signature: str,
        transition: CaseFileLifecycleTransition,
        issue_number: int,
        already_terminal: TerminalRetirementPolicy,
        expected_revision: str | None = None,
        expected_signatures: frozenset[str] | None = None,
    ) -> CaseFileRetirementOutcome:
        """Publish evidence, close idempotently, then commit terminal authority.

        ``already_terminal`` is REQUIRED so every writer states what a signature
        some other transition already retired means to it: a reviewed decision
        refuses (changing a terminal disposition is a reopen), an observed
        outcome adopts the recorded disposition and performs no GitHub effect
        (#7345). See :class:`TerminalRetirementPolicy`.

        ``issue_number`` is the case file the CALLER is authorized to mutate —
        the guarded command's reconciliation subject. It rides into the reserving
        compare-and-swap so shared authority rejects a divergent subject before
        anything is written, and it is re-asserted against every entry this owner
        acts on afterwards, so no recovered or raced entry can redirect the
        comment or the close onto a different issue (#7247 review F2/A1).
        """
        comment = retirement_comment(transition)
        self._before_write()
        reservation = self._registry.reserve_retirement(
            signature=signature,
            transition=transition,
            comment=comment,
            issue_number=issue_number,
            expected_revision=expected_revision,
            expected_signatures=expected_signatures,
            already_terminal=already_terminal,
        )
        if reservation.state is PatternReservationState.COMMITTED:
            return self._outcome(
                reservation.entry,
                issue_number,
                transition,
                deduplicated=True,
                already_terminal=already_terminal,
            )
        reservation = self._recover_or_acquire(reservation, issue_number)
        entry = self._subject(reservation.entry, issue_number)
        pending = entry.pending_retirement
        if pending is None:
            return self._outcome(entry, issue_number, transition, deduplicated=True)

        if pending.phase is PatternRetirementPhase.COMMENT:
            entry = self._subject(
                self._publish_comment(reservation, issue_number), issue_number
            )
            pending = entry.pending_retirement
            if pending is None:
                return self._outcome(entry, issue_number, transition, deduplicated=True)

        self._before_write()
        self._repository.update_issue_state(issue_number, "closed")
        self._before_write()
        committed = self._registry.finalize_retirement(
            signature=signature, reservation_id=entry.reservation_id
        )
        return self._outcome(committed, issue_number, transition, deduplicated=False)

    def _recover_or_acquire(
        self, reservation: PatternReservation, issue_number: int
    ) -> PatternReservation:
        self._subject(reservation.entry, issue_number)
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
        self, reservation: PatternReservation, issue_number: int
    ) -> "PatternRegistryEntry":
        entry = self._subject(reservation.entry, issue_number)
        pending = entry.pending_retirement
        assert pending is not None
        if reservation.state is PatternReservationState.RECOVERABLE:
            receipt = self._find_receipt(entry)
            if receipt is None:
                raise AmbiguousPatternPublicationError(
                    f"pattern {entry.signature!r} retirement publication started at"
                    f" {entry.publication_started_at}, but its marker is not yet"
                    " observable; preserving publication state"
                )
        else:
            # The publication marker is recorded by ``mark_attempt``, INSIDE
            # ``ensure_comment_published`` and immediately before the only call
            # that can reach GitHub. Recording it here instead — before the
            # receipt pre-check and before the mutation guard — is what made a
            # transient refusal permanent: nothing was posted, the entry was
            # left PUBLISHING, and every retry then required a receipt that
            # could never exist (#7248 review F6).
            def mark_attempt() -> None:
                nonlocal entry
                started = self._registry.begin_retirement_publication(
                    signature=entry.signature, reservation_id=entry.reservation_id
                )
                if started.state is not PatternReservationState.ACQUIRED:
                    raise PatternRegistryError(
                        f"pattern {entry.signature!r} retirement reservation changed"
                        " before publication"
                    )
                entry = self._subject(started.entry, issue_number)

            ensure_comment_published(
                issue_number,
                pending.comment,
                find_receipt=lambda number, body: self._repository.find_issue_comment_receipt(
                    number, body=body
                ),
                post_comment=self._repository.add_comment,
                before_write=self._before_write,
                mark_attempt=mark_attempt,
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
    def _subject(
        entry: "PatternRegistryEntry", issue_number: int
    ) -> "PatternRegistryEntry":
        """Re-assert the authorized case file on an entry this owner will act on."""
        require_canonical_case_file(entry, issue_number)
        return entry

    def _outcome(
        self,
        entry: "PatternRegistryEntry",
        issue_number: int,
        requested: CaseFileLifecycleTransition,
        *,
        deduplicated: bool,
        already_terminal: TerminalRetirementPolicy = TerminalRetirementPolicy.REFUSE,
    ) -> CaseFileRetirementOutcome:
        """Report the transition the REGISTRY committed, never the attempt's.

        ``CaseFileLifecycleTransition.same_intent`` deliberately excludes
        ``recorded_at`` so a retry reuses the first successful reservation. That
        makes the caller's own transition the wrong thing to report back: a retry
        would hand its later wall-clock timestamp to the next layer while durable
        authority says something earlier. Resolving *requested* against the
        committed lifecycle keeps this owner's result and its authority the same
        fact (#7247 review F1).

        An ADOPTED outcome is resolved by the same rule the registry admitted it
        under (:func:`resolve_admitted_transition`), so the disposition reported
        here is the one shared authority recorded, not the one requested.
        """
        entry = self._subject(entry, issue_number)
        committed = self._committed_transition(
            entry, requested, already_terminal=already_terminal
        )
        return CaseFileRetirementOutcome(
            issue_number=issue_number,
            transition=committed,
            deduplicated=deduplicated,
            adopted=committed.transition_id != requested.transition_id,
        )

    @staticmethod
    def _committed_transition(
        entry: "PatternRegistryEntry",
        requested: CaseFileLifecycleTransition,
        *,
        already_terminal: TerminalRetirementPolicy,
    ) -> CaseFileLifecycleTransition:
        """The durable transition that answers *requested*."""
        recorded = resolve_admitted_transition(
            entry, requested, already_terminal=already_terminal
        )
        if recorded is None:
            raise PatternRegistryError(
                f"pattern {entry.signature!r} reports a completed retirement, but"
                f" durable authority has no transition {requested.transition_id!r}"
            )
        return recorded
