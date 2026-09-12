"""Shared authority for Tech Lead pattern case-file identity and evidence."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from ..domain.tech_lead_findings import (
    CASE_FILE_ACTIVE,
    case_file_blocks_promotion,
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

    @property
    def retirement_pending(self) -> bool:
        """Whether a terminal retirement is admitted here but not finalized.

        Any pending retirement is a terminal intent:
        :class:`PendingPatternRetirement` refuses a non-terminal disposition at
        construction, so the presence of one IS the fact.
        """
        return self.pending_retirement is not None

    @property
    def blocks_promotion(self) -> bool:
        """Whether this signature has left the automatic promotion lane."""
        return case_file_blocks_promotion(
            self.disposition, retirement_pending=self.retirement_pending
        )

    def review_revision(self) -> str:
        """Fingerprint the settled facts an operator reviewed for this entry.

        Covers exactly what a reviewed disposition is a decision ABOUT — the
        canonical case file, its evidence identities, its merged classification,
        and its lifecycle history — and deliberately omits the lease fields
        (``reservation_id``, ``claimant_id``, ``expires_at``), which change on
        every unrelated client hand-off and would make every plan stale within
        minutes without any reviewed fact having moved.

        An entry with an external effect in flight has NO review revision: its
        settled state is not yet knowable, so a plan cannot be bound to it and
        asking is a caller error rather than a mismatch. Registries admit
        idempotent replay and resume an in-flight retirement before they reach
        :func:`require_reviewed_revision`, so this never fires on a retry of a
        write that was already admitted (#7248 review P2).
        """
        if self.pending is not None or any(
            (self.pending_observation is not None, self.pending_retirement is not None)
        ):
            raise PatternRegistryError(
                f"pattern {self.signature!r} has an external effect in flight"
            )
        payload = {
            "signature": self.signature,
            "issue_number": self.issue_number,
            "observation_ids": self.observation_ids,
            "classification": asdict(self.classification),
            "lifecycle": [asdict(item) for item in self.lifecycle],
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class PatternReservation:
    """Outcome of reserving one signature's create boundary."""

    state: PatternReservationState
    entry: PatternRegistryEntry


def require_canonical_case_file(entry: PatternRegistryEntry, issue_number: int) -> None:
    """Reject a caller whose authorized case file is not this signature's.

    THE rule, in one place, so the shared CAS registry, the single-process
    registry, and both write paths cannot drift on it. A case-file command is
    guarded against the issue named in the ACTION, while the evidence comment or
    the retirement close lands on the issue named in the REGISTRY. If those two
    identities disagree, the expected-state gate would authorize one issue while
    the owner mutates another, and durable memory would then record the first as
    settled. Every registry calls this inside the compare-and-swap that reserves
    the write, before any state is written, so a mismatch leaves no reservation
    behind to recover (#7247 review F2/A1).
    """
    if entry.issue_number != issue_number:
        raise PatternRegistryError(
            f"pattern {entry.signature!r} is registered to case file"
            f" #{entry.issue_number}, but this write is authorized for"
            f" #{issue_number}; refusing to mutate a different issue"
        )


def resolve_recorded_transition(
    entry: PatternRegistryEntry, requested: CaseFileLifecycleTransition
) -> CaseFileLifecycleTransition | None:
    """THE identity rule for one durable lifecycle transition, in one place.

    Returns the transition *entry* already records for ``requested``'s stable
    identity, or ``None`` when nothing is recorded under it. Raises when the
    identity IS recorded with a different payload: a ``transition_id`` names one
    reviewed intent, so two different ones cannot share it.

    Both questions that ask about a recorded transition go through this —
    :func:`admit_lifecycle_transition`, which needs to know whether a write is
    an idempotent replay, and the lifecycle owner's result resolution, which
    needs the recorded transition itself because ``same_intent`` excludes
    ``recorded_at`` and a retry must report authority's timestamp rather than
    its own. They previously spelled the same loop and the same rejection out
    separately, which is the drift class the other rules in this module exist
    to prevent.
    """
    for recorded in entry.lifecycle:
        if recorded.transition_id != requested.transition_id:
            continue
        if not recorded.same_intent(requested):
            raise PatternRegistryError(
                f"lifecycle transition {requested.transition_id!r} changed payload"
            )
        return recorded
    return None


def admit_lifecycle_transition(
    entry: PatternRegistryEntry, transition: CaseFileLifecycleTransition
) -> bool:
    """THE admission rule for one durable lifecycle write, in one place.

    Returns ``True`` when *transition* is ALREADY recorded on *entry* — the
    idempotent replay a retry must answer with the committed entry instead of a
    second write — and ``False`` when it is new and admissible. It raises when
    the write must not be admitted at all: a ``transition_id`` whose payload
    changed (two different reviewed intents cannot share one identity), or any
    further transition on a signature that already reached a terminal
    disposition.

    Both registries and both lifecycle write paths call this. The rule was
    previously written out once per path and had already drifted: the
    single-process registry omitted the already-terminal guard, so a second,
    different retirement of a retired signature was refused by shared authority
    but admitted locally — closing the case file twice and appending two
    terminal transitions, depending only on which registry a deployment runs
    (#7247 final abstraction pass).
    """
    if resolve_recorded_transition(entry, transition) is not None:
        return True
    if entry.lifecycle and entry.lifecycle[-1].terminal:
        raise PatternRegistryError(
            f"pattern {entry.signature!r} is terminal ({entry.disposition!r});"
            " reopening requires an explicit reopen transition"
        )
    return False


def require_resumable_retirement(
    entry: PatternRegistryEntry, desired: PendingPatternRetirement
) -> PendingPatternRetirement:
    """THE compatibility rule for resuming ONE in-flight terminal write.

    A retirement reserves durable state and then performs external effects, so a
    second attempt at the same signature is either a RESUME of the interrupted
    write or a different decision that must not inherit its reservation. What
    separates them is the whole pending payload: the transition's stable intent
    AND the exact comment body already promised to the case file. The comment is
    not decoration — it is the idempotency key the recovery path searches for
    (``find_issue_comment_receipt``), so resuming with a changed body would look
    for a receipt that cannot exist and republish over the one that does.

    Both registries and the reconciliation preflight call this. Preflight asking
    a weaker question than the write path is exactly the defect this centralizes
    away: a plan whose entry has a pending retirement with the same intent but a
    different comment passed preflight and was refused mid-apply, after earlier
    rows had already been commented on and closed, breaking the guarantee that
    the complete decision set is admitted before its first write (#7248 round 2
    review F2/A2).

    Returns the pending retirement so a caller that must then decide its
    reservation STATE — recoverable, publishing, held — works from the payload
    this rule just proved compatible.
    """
    pending = entry.pending_retirement
    if pending is None:
        raise PatternRegistryError(
            f"pattern {entry.signature!r} has no retirement in flight"
        )
    if not pending.transition.same_intent(desired.transition):
        raise PatternRegistryError(
            f"pattern {entry.signature!r} has a different retirement in flight"
        )
    if pending.comment != desired.comment:
        raise PatternRegistryError(
            f"pattern {entry.signature!r} retirement comment changed"
        )
    return pending


def require_reviewed_revision(
    entry: PatternRegistryEntry, expected_revision: str | None
) -> None:
    """THE staleness rule binding one reviewed plan to the state it reviewed.

    A reconciliation plan is authored against a registry an operator actually
    read. Between that review and the write, evidence can be reclassified, a
    lifecycle transition can land, or the case file can be reassigned — and the
    reviewed disposition is then a decision about facts that no longer exist.
    ``expected_revision`` is :meth:`PatternRegistryEntry.review_revision` as of
    the review, and every registry checks it INSIDE the compare-and-swap that
    admits the write, so a stale plan is refused atomically rather than in a
    check-then-write gap (#7248 review P2).

    ``None`` means the caller is not replaying a reviewed decision and opts out.
    Callers admit idempotent replay before reaching here, so re-running an
    already-recorded transition stays green even once the revision has moved on.

    Both registries and both lifecycle write paths call this, for the same
    reason ``admit_lifecycle_transition`` exists in one place: a rule written
    out once per path drifts, and a rule that drifts here decides whether a
    stale plan mutates GitHub based on which registry a deployment runs.
    """
    if expected_revision is None:
        return
    actual = entry.review_revision()
    if actual != expected_revision:
        raise PatternRegistryError(
            f"pattern {entry.signature!r} changed since lifecycle review"
            f" (reviewed {expected_revision}, now {actual})"
        )


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
        issue_number: int,
    ) -> PatternReservation:
        """Admit one exact evidence publication against its canonical case file.

        ``issue_number`` carries the same guarantee it carries into
        :meth:`reserve_retirement`, through :func:`require_canonical_case_file`:
        the caller's authorized case file is checked inside the reserving
        compare-and-swap, so a signature whose registry row names a different
        issue admits nothing rather than reserving first and being rejected
        afterwards (#7247 review A1, applied to both write paths).
        """
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
        self,
        *,
        signature: str,
        transition: CaseFileLifecycleTransition,
        expected_revision: str | None = None,
    ) -> PatternRegistryEntry:
        """Record an active/needs-human classification without GitHub mutation.

        ``expected_revision`` carries the guarantee described in
        :func:`require_reviewed_revision`: the reviewed state is re-checked
        inside the admitting compare-and-swap, so a plan written against facts
        that have since moved writes nothing.
        """
        ...

    def reserve_retirement(
        self,
        *,
        signature: str,
        transition: CaseFileLifecycleTransition,
        comment: str,
        issue_number: int,
        expected_revision: str | None = None,
    ) -> PatternReservation:
        """Reserve one exact terminal transition against its canonical case file.

        ``issue_number`` is the case file the CALLER was authorized to mutate.
        It is validated against ``current.issue_number`` inside the same
        compare-and-swap that reserves the retirement, so the guarded command's
        subject and the issue this retirement actually comments on and closes
        cannot diverge — no separate pre-read, and therefore no check/use gap
        (#7247 review F2/A1).

        ``expected_revision`` adds the second half of the same guarantee for a
        reviewed plan: see :func:`require_reviewed_revision`. It is checked once
        the reservation is known to be new — an in-flight retirement is a resume
        of the same reviewed intent, not a second decision.
        """
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
