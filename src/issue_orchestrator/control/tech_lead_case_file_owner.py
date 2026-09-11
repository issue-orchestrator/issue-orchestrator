"""The bounded owner of pattern case-file identity and evidence (#6781/#6957).

A case file is the durable evidence ledger for one pattern signature, and three
invariants make it trustworthy:

* **At most one case file per signature, ever.** Promotion reads the accrued
  observation count, so a second case file would split the evidence that gates
  it — or orphan half of it.
* **Every observation counted exactly once**, under its own identity. The count
  is orchestrator-owned precisely so it cannot be inflated by editing the issue,
  and it must not be inflated by the orchestrator's own retries either.
* **The recorded classification is what the recorded body says.** ``fix_class``
  and ``area`` decide whether a finding is promotable at all and which repo it
  routes to, so they must come from the command that actually wrote the issue.

All three span a shared registry write, a local planning-replica write, and a
GitHub issue write, in an order that matters. This module owns that transaction
while the registry owns cross-client compare-and-swap.

**The creation transaction.** The GitHub issue is created before its ledger row
exists, so a crash in between leaves an issue nothing knows about. A marker
lookup can find that issue again — but it cannot say WHICH command wrote it, and
the retry is not guaranteed to be the same command: an ordinary case-file
finalization failure is returned as an ``ActionResult`` failure, so the next
observation of that signature can be the one that recovers it. Adopting the
orphan with the retrying action's metadata therefore recorded the wrong
observation identity and could rewrite a ``fix:human`` finding as ``fix:code``,
with no durable row for the classification preflight to defend (#6957 round-3
review F10). So the owner acquires a leased shared reservation containing a
durable :class:`PendingCaseFile` BEFORE the create and finalizes a recovered
issue FROM THAT INTENT. A different client may recover an expired reservation,
while a live peer's reservation stops the competing create. The retrying action
is then handled separately, as an ordinary append.

Five operations, one owner:

* :meth:`PatternCaseFileOwner.inspect` — read-only orphan detection before
  provisioning labels;
* :meth:`PatternCaseFileOwner.resolve` — typed :class:`CaseFileResolution`:
  already committed, reserved for this client, or recovered from the original
  intent;
* :meth:`PatternCaseFileOwner.begin` — assert the reservation immediately
  before the GitHub create;
* :meth:`PatternCaseFileOwner.open` — commit the ledger row for an issue this
  process just created, and append the rest of its decision's observations.
* :meth:`PatternCaseFileOwner.adopt` and :meth:`append_observations` — reconcile
  an action's observations onto an existing case file.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Callable, Iterable

from .comment_publication import ensure_comment_published

from ..domain.tech_lead_findings import CaseFileClassification, PendingCaseFile
from ..ports.pattern_registry import (
    PatternCaseFileRegistry,
    PatternRegistryError,
    PatternReservation,
    PatternReservationState,
)

if TYPE_CHECKING:
    from ..domain.tech_lead_findings import PatternObservation
    from ..ports import RepositoryHost
    from ..ports.pattern_registry import PatternRegistryEntry
    from .actions import CreateTechLeadCaseFileIssueAction

logger = logging.getLogger(__name__)


class OrphanedCaseFileError(RuntimeError):
    """A case file exists on GitHub with no ledger row and no creation intent.

    Unreachable through the ordinary crash path — the intent is written before
    every create — so this means the orchestrator's own durable state was lost
    while the remote issue survived. Neither available answer is safe to guess:
    adopting the orphan would invent its observation identity and classification
    from an unrelated action, and creating another would split the signature's
    evidence. So the lane stops and says so.
    """


class AmbiguousPatternPublicationError(PatternRegistryError):
    """An external write began but its durable outcome is not observable yet.

    GitHub issue and comment creation do not accept a caller idempotency key or
    fencing token. Once a write starts, expiry therefore cannot safely license
    a second write: the first request may still land. The durable publishing
    state is deliberately retained until the exact remote marker is observed.
    """


class CaseFileState(Enum):
    """What :meth:`PatternCaseFileOwner.resolve` found."""

    #: The ledger already holds this signature.
    COMMITTED = "committed"
    #: An in-flight creation was found on GitHub and finalized from its intent.
    RECOVERED = "recovered"
    #: No case file exists; the caller must create one.
    ABSENT = "absent"


@dataclass(frozen=True)
class CaseFileResolution:
    """Where a case-file creation action should land."""

    state: CaseFileState
    issue_number: int | None = None

    def __post_init__(self) -> None:
        exists = self.state is not CaseFileState.ABSENT
        if exists != (self.issue_number is not None):
            raise ValueError(
                f"a {self.state.value} case-file resolution must"
                f"{'' if exists else ' not'} carry an issue number"
            )


@dataclass(frozen=True)
class ObservationAppendOutcome:
    """What an append actually did: newly counted vs. already-recorded."""

    recorded: int
    skipped: int

    @property
    def deduplicated(self) -> bool:
        """True when nothing new was counted — every observation was a replay."""
        return self.recorded == 0 and self.skipped > 0


class PatternCaseFileOwner:
    """Owns case-file identity, its creation transaction, and evidence accrual."""

    def __init__(
        self,
        *,
        registry: PatternCaseFileRegistry,
        repository_host: "RepositoryHost",
        add_comment: Callable[[int, str], str],
        before_write: Callable[[], None],
    ) -> None:
        self._registry = registry
        self._repository_host = repository_host
        self._add_comment = add_comment
        self._before_write = before_write

    def inspect(
        self, action: "CreateTechLeadCaseFileIssueAction"
    ) -> CaseFileResolution | None:
        """Read authority and detect pre-registry orphans without writing."""
        current = self._registry.read(signature=action.pattern_signature)
        if current is not None:
            if current.committed:
                assert current.issue_number is not None
                return CaseFileResolution(CaseFileState.COMMITTED, current.issue_number)
            return None
        found = self._repository_host.find_issue_by_marker(
            title=action.title,
            marker=action.idempotency_marker,
            authoritative=False,
        )
        if found is not None:
            raise OrphanedCaseFileError(
                f"case file #{found} exists for signature"
                f" {action.pattern_signature!r} but the registry has neither a"
                " ledger row nor a record of creating it; import the trusted"
                " local ledger before proceeding"
            )
        self._orphan_checked_signature = action.pattern_signature
        return None

    def resolve(
        self, action: "CreateTechLeadCaseFileIssueAction"
    ) -> CaseFileResolution:
        """Does a case file already exist for this signature, and where?

        The ledger is the authority and is consulted first, so the common path
        costs no GitHub call. Otherwise the durable creation intent decides
        whether an in-flight create could exist at all:

        * an intent present means one might — look it up, and if it is there,
          COMMIT ITS LEDGER ROW FROM THE INTENT. The intent knows the body
          observation, classification, area, and diagnosis that were actually
          written; the action in hand may be a later, different observation of
          the same signature and must not lend its own metadata (#6957 round-3
          review F10);
        * an intent present but no remote issue means the create never
          happened. The intent is stale, so it is discarded explicitly rather
          than silently overwritten, and the caller creates fresh;
        * no intent at all means nothing was in flight. An issue nevertheless
          existing is a durable-state loss, not a crash window, and raises
          :class:`OrphanedCaseFileError` rather than being guessed at.

        A lookup that FAILS propagates: "unknown" must never be mistaken for
        "no case file exists", because that is what files a duplicate.
        """
        pending = PendingCaseFile(
            signature=action.pattern_signature,
            title=action.title,
            idempotency_marker=action.idempotency_marker,
            body_observation_id=action.body_observation.observation_id,
            fix_class=action.fix_class,
            area=action.area or "",
            diagnosis=action.diagnosis,
        )
        self._guard_unregistered_remote(action)
        self._before_write()
        reservation = self._registry.reserve(pending)
        if reservation.state is PatternReservationState.COMMITTED:
            assert reservation.entry.issue_number is not None
            return CaseFileResolution(
                CaseFileState.COMMITTED, reservation.entry.issue_number
            )
        if reservation.state is PatternReservationState.HELD:
            raise PatternRegistryError(
                f"pattern {action.pattern_signature!r} is being created by"
                f" {reservation.entry.claimant_id}; retry after"
                f" {reservation.entry.expires_at}"
            )
        if reservation.state is PatternReservationState.ACQUIRED:
            self._reservation_id = reservation.entry.reservation_id
            return CaseFileResolution(CaseFileState.ABSENT)

        if reservation.state is PatternReservationState.PUBLISHING:
            return self._recover_started_creation(reservation.entry)
        return self._recover_reserved_creation(action, pending, reservation)

    def _guard_unregistered_remote(
        self, action: "CreateTechLeadCaseFileIssueAction"
    ) -> None:
        """Reject remote identity whose durable registry provenance is absent."""
        current = self._registry.read(signature=action.pattern_signature)
        if current is not None or (
            getattr(self, "_orphan_checked_signature", "") == action.pattern_signature
        ):
            return
        # A concurrent legitimate creator must reserve first, so the later
        # atomic reserve closes the gap after this pre-registry orphan check.
        found = self._repository_host.find_issue_by_marker(
            title=action.title,
            marker=action.idempotency_marker,
            authoritative=False,
        )
        if found is not None:
            raise OrphanedCaseFileError(
                f"case file #{found} exists for signature"
                f" {action.pattern_signature!r} but the registry has neither a"
                " ledger row nor a record of creating it; import the trusted"
                " local ledger before proceeding"
            )

    def _recover_reserved_creation(
        self,
        action: "CreateTechLeadCaseFileIssueAction",
        pending: PendingCaseFile,
        reservation: PatternReservation,
    ) -> CaseFileResolution:
        """Recover or safely replace a reservation that never began its write."""
        assert reservation.state is PatternReservationState.RECOVERABLE
        original = reservation.entry.pending
        if original is None:
            raise PatternRegistryError("recoverable reservation has no creation intent")
        found = self._repository_host.find_issue_by_marker(
            title=original.title,
            marker=original.idempotency_marker,
            authoritative=True,
        )
        if found is None:
            self._before_write()
            takeover = self._registry.take_over(
                stale_reservation_id=reservation.entry.reservation_id,
                pending=pending,
            )
            if takeover.state is PatternReservationState.COMMITTED:
                assert takeover.entry.issue_number is not None
                return CaseFileResolution(
                    CaseFileState.COMMITTED, takeover.entry.issue_number
                )
            if takeover.state is not PatternReservationState.ACQUIRED:
                raise PatternRegistryError(
                    f"pattern {action.pattern_signature!r} changed during recovery"
                )
            self._reservation_id = takeover.entry.reservation_id
            return CaseFileResolution(CaseFileState.ABSENT)

        logger.warning(
            "[tech_lead] Recovered interrupted case file #%d for signature %r"
            " from the shared creation reservation",
            found,
            action.pattern_signature,
        )
        self._before_write()
        committed = self._registry.finalize(
            signature=action.pattern_signature,
            reservation_id=reservation.entry.reservation_id,
            issue_number=found,
        )
        assert committed.issue_number is not None
        return CaseFileResolution(CaseFileState.RECOVERED, committed.issue_number)

    def begin(self, action: "CreateTechLeadCaseFileIssueAction") -> None:
        """Fence the token and durably mark publication immediately before create."""
        reservation_id = getattr(self, "_reservation_id", "")
        if not reservation_id:
            raise PatternRegistryError(
                f"pattern {action.pattern_signature!r} was not reserved"
            )
        self._before_write()
        started = self._registry.begin_creation_publication(
            signature=action.pattern_signature,
            reservation_id=reservation_id,
        )
        if started.state is not PatternReservationState.ACQUIRED:
            raise PatternRegistryError(
                f"pattern {action.pattern_signature!r} creation reservation"
                " changed before publication"
            )
        self._reservation_id = started.entry.reservation_id

    def _recover_started_creation(
        self, entry: "PatternRegistryEntry"
    ) -> CaseFileResolution:
        """Finalize a proven create; never reissue an ambiguous started write."""
        pending = entry.pending
        if pending is None:
            raise PatternRegistryError("started creation has no pending intent")
        found = self._repository_host.find_issue_by_marker(
            title=pending.title,
            marker=pending.idempotency_marker,
            authoritative=True,
        )
        if found is None:
            raise AmbiguousPatternPublicationError(
                f"pattern {entry.signature!r} issue publication started at"
                f" {entry.publication_started_at}, but its marker is not yet"
                " observable; preserving publication state to prevent a"
                " duplicate issue"
            )
        self._before_write()
        committed = self._registry.finalize(
            signature=entry.signature,
            reservation_id=entry.reservation_id,
            issue_number=found,
        )
        assert committed.issue_number is not None
        return CaseFileResolution(CaseFileState.RECOVERED, committed.issue_number)

    def open(
        self, action: "CreateTechLeadCaseFileIssueAction", *, issue_number: int
    ) -> None:
        """Commit the ledger row for an issue THIS action just created.

        The issue BODY is the first observation, so the row is created carrying
        exactly that one identity; every further observation from the same
        decision is appended one at a time (comment, then count create-once)
        rather than pre-counted. Pre-counting them claimed evidence a crash
        might never post, and the retry counted it all again.
        """
        self._before_write()
        self._registry.finalize(
            signature=action.pattern_signature,
            reservation_id=self._reservation_id,
            issue_number=issue_number,
        )
        self.append_observations(
            signature=action.pattern_signature,
            issue_number=issue_number,
            observations=action.additional_observations,
            fix_class=action.fix_class,
            area=action.area or "",
            diagnosis=action.diagnosis,
        )

    def adopt(
        self, action: "CreateTechLeadCaseFileIssueAction", *, issue_number: int
    ) -> "ObservationAppendOutcome":
        """Reconcile a whole creation action onto an ALREADY-existing case file.

        Every observation the action carried becomes a repeat observation on the
        existing issue — comment AND durable count, exactly like the planned
        repeat path — so evidence is never silently dropped when a concurrent
        tick, a crash-retry, or a recovery got there first. When the action IS
        the one that created the issue, its body observation is already recorded
        and is skipped rather than re-posted.
        """
        return self.append_observations(
            signature=action.pattern_signature,
            issue_number=issue_number,
            observations=action.observations,
            fix_class=action.fix_class,
            area=action.area or "",
            diagnosis=action.diagnosis,
        )

    def append_observations(
        self,
        *,
        signature: str,
        issue_number: int,
        observations: Iterable["PatternObservation"],
        fix_class: str,
        area: str,
        diagnosis: str,
    ) -> "ObservationAppendOutcome":
        """Post and count each observation, skipping ones already recorded.

        The ordering is deliberate and shared by every caller:

        0. RESERVE the incoming identity and classification in shared authority.
           A conflicting classification or a live peer reservation raises before
           anything is published. This is the apply-time mirror of the planner's
           preflight, including recovery paths planning could not see.
        1. an identity ALREADY committed means a previous attempt completed —
           do nothing;
        2. otherwise start durable publication at the exact token boundary, recover
           or publish the authoritative comment receipt, then finalize the
           reserved count. A crash between publication and finalization recovers
           the exact receipt and reservation without reposting or double counting.

        The merged diagnosis rides the SAME create-once write as the
        classification upgrade, so a signature can never end up promotable with
        a diagnosis the ledger disagrees with, in either direction.

        Evidence is therefore never lost, never inflated, and never published
        under a classification the ledger disagrees with.

        Returns what actually happened, so callers report a replay honestly
        instead of inferring it from the absence of an error.
        """
        recorded = 0
        skipped = 0
        for observation in observations:
            outcome = self._append_observation(
                signature=signature,
                issue_number=issue_number,
                observation=observation,
                classification=CaseFileClassification(
                    fix_class=fix_class, area=area, diagnosis=diagnosis
                ),
            )
            recorded += outcome.recorded
            skipped += outcome.skipped
        return ObservationAppendOutcome(recorded=recorded, skipped=skipped)

    def _append_observation(
        self,
        *,
        signature: str,
        issue_number: int,
        observation: "PatternObservation",
        classification: CaseFileClassification,
    ) -> ObservationAppendOutcome:
        """Reserve, publish, and commit one observation under one shared token."""
        self._before_write()
        admission = self._admit_observation(
            signature=signature,
            issue_number=issue_number,
            observation=observation,
            classification=classification,
        )
        if isinstance(admission, ObservationAppendOutcome):
            return admission
        reservation = admission

        pending = reservation.entry.pending_observation
        if pending is None:
            raise PatternRegistryError(
                "evidence reservation has no pending observation"
            )

        def admit_comment() -> None:
            self._before_write()
            started = self._registry.begin_observation_publication(
                signature=signature,
                reservation_id=reservation.entry.reservation_id,
            )
            if started.state is not PatternReservationState.ACQUIRED:
                raise PatternRegistryError(
                    f"pattern {signature!r} evidence reservation changed"
                    " before publication"
                )

        ensure_comment_published(
            issue_number,
            pending.observation.comment,
            find_receipt=lambda number, body: (
                self._repository_host.find_issue_comment_receipt(number, body=body)
            ),
            post_comment=self._add_comment,
            before_write=admit_comment,
        )
        self._before_write()
        was_recorded = self._registry.finalize_observation(
            signature=signature,
            reservation_id=reservation.entry.reservation_id,
        )
        outcome = ObservationAppendOutcome(
            recorded=int(was_recorded), skipped=int(not was_recorded)
        )
        if pending.observation.observation_id == observation.observation_id:
            return outcome
        current = self._append_observation(
            signature=signature,
            issue_number=issue_number,
            observation=observation,
            classification=classification,
        )
        return ObservationAppendOutcome(
            recorded=outcome.recorded + current.recorded,
            skipped=outcome.skipped + current.skipped,
        )

    def _admit_observation(
        self,
        *,
        signature: str,
        issue_number: int,
        observation: "PatternObservation",
        classification: CaseFileClassification,
    ) -> PatternReservation | ObservationAppendOutcome:
        """Return one acquired token or the complete replay/recovery outcome.

        The authorized case file rides INTO the reservation rather than being
        checked after it: shared authority applies the one
        :func:`~..ports.pattern_registry.require_canonical_case_file` rule inside
        the same compare-and-swap, so a signature whose registry row names a
        different issue leaves no reservation behind, exactly as on the
        retirement path (#7247 review A1).
        """
        reservation = self._registry.reserve_observation(
            signature=signature,
            observation=observation,
            classification=classification,
            issue_number=issue_number,
        )
        if reservation.state is PatternReservationState.COMMITTED:
            return ObservationAppendOutcome(recorded=0, skipped=1)
        if reservation.state is PatternReservationState.HELD:
            raise PatternRegistryError(
                f"pattern {signature!r} evidence is being published by"
                f" {reservation.entry.claimant_id}; retry after"
                f" {reservation.entry.expires_at}"
            )
        if reservation.state is PatternReservationState.PUBLISHING:
            return self._recover_started_observation(
                signature=signature,
                issue_number=issue_number,
                requested=observation,
                classification=classification,
                entry=reservation.entry,
            )
        if reservation.state is PatternReservationState.RECOVERABLE:
            self._before_write()
            reservation = self._registry.take_over_observation(
                signature=signature,
                stale_reservation_id=reservation.entry.reservation_id,
            )
            if reservation.state is not PatternReservationState.ACQUIRED:
                raise PatternRegistryError(
                    f"pattern {signature!r} evidence changed during recovery"
                )
        return reservation

    def _recover_started_observation(
        self,
        *,
        signature: str,
        issue_number: int,
        requested: "PatternObservation",
        classification: CaseFileClassification,
        entry: "PatternRegistryEntry",
    ) -> ObservationAppendOutcome:
        """Finalize a proven comment; retain an unprovable in-flight write."""
        pending = entry.pending_observation
        if pending is None:
            raise PatternRegistryError("started evidence publication has no payload")
        receipt = self._repository_host.find_issue_comment_receipt(
            issue_number, body=pending.observation.comment
        )
        if receipt is None:
            raise AmbiguousPatternPublicationError(
                f"pattern {signature!r} evidence publication started at"
                f" {entry.publication_started_at}, but its comment is not yet"
                " observable; preserving publication state to prevent a"
                " duplicate comment"
            )
        self._before_write()
        recorded = self._registry.finalize_observation(
            signature=signature,
            reservation_id=entry.reservation_id,
        )
        recovered = ObservationAppendOutcome(
            recorded=int(recorded), skipped=int(not recorded)
        )
        if pending.observation.observation_id == requested.observation_id:
            return recovered
        current = self._append_observation(
            signature=signature,
            issue_number=issue_number,
            observation=requested,
            classification=classification,
        )
        return ObservationAppendOutcome(
            recorded=recovered.recorded + current.recorded,
            skipped=recovered.skipped + current.skipped,
        )
