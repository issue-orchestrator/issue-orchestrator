"""Local replica and single-instance implementations of the pattern registry."""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from typing import Callable, NoReturn

from ..domain.tech_lead_findings import (
    CaseFileClassification,
    CaseFileLifecycleTransition,
    PatternObservation,
    PendingCaseFile,
)
from ..ports.pattern_registry import (
    admit_lifecycle_transition,
    PendingPatternObservation,
    PendingPatternRetirement,
    PatternCaseFileRegistry,
    PatternRegistryEntry,
    PatternRegistryError,
    PatternRetirementPhase,
    PatternReservation,
    PatternReservationState,
    require_canonical_case_file,
    require_resumable_retirement,
    require_reviewed_revision,
)
from ..ports.tech_lead_authority import TechLeadAuthorityStore


class MirroredPatternCaseFileRegistry(PatternCaseFileRegistry):
    """Shared authority with SQLite as its exact local planning replica."""

    def __init__(
        self,
        *,
        shared: PatternCaseFileRegistry,
        local: TechLeadAuthorityStore,
        claimant_id: str,
    ) -> None:
        self._shared = shared
        self._local = local
        self._claimant_id = claimant_id

    def synchronize(self) -> None:
        """Merge rolling-upgrade rows, then rebuild this client's local cache.

        This registry is write-through in BOTH directions, including on paths
        that read like reads: ``synchronize`` publishes this client's legacy
        rows to shared authority, and ``list_entries``/``read`` mirror every
        committed shared row into the local replica and discard any matching
        pending create intent. A caller that must not write therefore cannot
        use this class with a flag — it must not hold one at all; see
        :class:`ReadOnlyPatternCaseFileRegistry` (#7248 review F1).
        """
        seeds = tuple(
            self._seed(evidence) for evidence in self._local.list_pattern_evidence()
        )
        self._shared.seed_committed(seeds)
        for entry in self._shared.list_entries():
            if entry.committed:
                self._mirror(entry)

    def reserve(self, pending: PendingCaseFile) -> PatternReservation:
        # Preserve an interrupted pre-registry create's original payload before
        # allowing a newer observation to participate in recovery.
        if self._shared.read(signature=pending.signature) is None:
            old_pending = self._local.load_pending_case_file(
                signature=pending.signature
            )
            if old_pending is not None:
                self._shared.reserve(old_pending)
        outcome = self._shared.reserve(pending)
        if outcome.entry.committed:
            self._mirror(outcome.entry)
        return outcome

    def take_over(
        self, *, stale_reservation_id: str, pending: PendingCaseFile
    ) -> PatternReservation:
        outcome = self._shared.take_over(
            stale_reservation_id=stale_reservation_id, pending=pending
        )
        if outcome.entry.committed:
            self._mirror(outcome.entry)
        return outcome

    def finalize(
        self, *, signature: str, reservation_id: str, issue_number: int
    ) -> PatternRegistryEntry:
        entry = self._shared.finalize(
            signature=signature,
            reservation_id=reservation_id,
            issue_number=issue_number,
        )
        self._mirror(entry)
        return entry

    def begin_creation_publication(
        self, *, signature: str, reservation_id: str
    ) -> PatternReservation:
        outcome = self._shared.begin_creation_publication(
            signature=signature, reservation_id=reservation_id
        )
        if outcome.entry.committed:
            self._mirror(outcome.entry)
        return outcome

    def reserve_observation(
        self,
        *,
        signature: str,
        observation: "PatternObservation",
        classification: CaseFileClassification,
        issue_number: int,
    ) -> PatternReservation:
        outcome = self._shared.reserve_observation(
            signature=signature,
            observation=observation,
            classification=classification,
            issue_number=issue_number,
        )
        if outcome.entry.committed:
            self._mirror(outcome.entry)
        return outcome

    def take_over_observation(
        self, *, signature: str, stale_reservation_id: str
    ) -> PatternReservation:
        outcome = self._shared.take_over_observation(
            signature=signature, stale_reservation_id=stale_reservation_id
        )
        if outcome.entry.committed:
            self._mirror(outcome.entry)
        return outcome

    def begin_observation_publication(
        self, *, signature: str, reservation_id: str
    ) -> PatternReservation:
        outcome = self._shared.begin_observation_publication(
            signature=signature, reservation_id=reservation_id
        )
        if outcome.entry.committed:
            self._mirror(outcome.entry)
        return outcome

    def finalize_observation(self, *, signature: str, reservation_id: str) -> bool:
        recorded = self._shared.finalize_observation(
            signature=signature, reservation_id=reservation_id
        )
        entry = self._shared.read(signature=signature)
        if entry is None or not entry.committed:
            raise PatternRegistryError(
                f"shared pattern {signature!r} disappeared after evidence commit"
            )
        self._mirror(entry)
        return recorded

    def record_lifecycle(
        self,
        *,
        signature: str,
        transition: CaseFileLifecycleTransition,
        expected_revision: str | None = None,
    ) -> PatternRegistryEntry:
        return self._shared.record_lifecycle(
            signature=signature,
            transition=transition,
            expected_revision=expected_revision,
        )

    def reserve_retirement(
        self,
        *,
        signature: str,
        transition: CaseFileLifecycleTransition,
        comment: str,
        issue_number: int,
        expected_revision: str | None = None,
    ) -> PatternReservation:
        return self._shared.reserve_retirement(
            signature=signature,
            transition=transition,
            comment=comment,
            issue_number=issue_number,
            expected_revision=expected_revision,
        )

    def take_over_retirement(
        self, *, signature: str, stale_reservation_id: str
    ) -> PatternReservation:
        return self._shared.take_over_retirement(
            signature=signature, stale_reservation_id=stale_reservation_id
        )

    def begin_retirement_publication(
        self, *, signature: str, reservation_id: str
    ) -> PatternReservation:
        return self._shared.begin_retirement_publication(
            signature=signature, reservation_id=reservation_id
        )

    def confirm_retirement_comment(
        self, *, signature: str, reservation_id: str
    ) -> PatternRegistryEntry:
        return self._shared.confirm_retirement_comment(
            signature=signature, reservation_id=reservation_id
        )

    def finalize_retirement(
        self, *, signature: str, reservation_id: str
    ) -> PatternRegistryEntry:
        return self._shared.finalize_retirement(
            signature=signature, reservation_id=reservation_id
        )

    def read(self, *, signature: str) -> PatternRegistryEntry | None:
        entry = self._shared.read(signature=signature)
        if entry is not None and entry.committed:
            self._mirror(entry)
        return entry

    def has_observation(self, *, signature: str, observation_id: str) -> bool:
        return self._shared.has_observation(
            signature=signature, observation_id=observation_id
        )

    def list_entries(self) -> tuple[PatternRegistryEntry, ...]:
        entries = self._shared.list_entries()
        for entry in entries:
            if entry.committed:
                self._mirror(entry)
        return entries

    def seed_committed(self, entries: tuple[PatternRegistryEntry, ...]) -> None:
        self._shared.seed_committed(entries)
        for entry in self._shared.list_entries():
            if entry.committed:
                self._mirror(entry)

    def _seed(self, evidence: object) -> PatternRegistryEntry:
        from ..domain.tech_lead_findings import PatternEvidence

        assert isinstance(evidence, PatternEvidence)
        observed = list(
            self._local.list_pattern_observation_ids(signature=evidence.signature)
        )
        digest = hashlib.sha256(evidence.signature.encode()).hexdigest()[:16]
        while len(observed) < evidence.observation_count:
            observed.append(f"legacy:{digest}:{len(observed) + 1}")
        return PatternRegistryEntry(
            signature=evidence.signature,
            reservation_id=f"migration:{digest}",
            claimant_id=self._claimant_id,
            expires_at=datetime(1970, 1, 1, tzinfo=timezone.utc).isoformat(),
            pending=None,
            issue_number=evidence.case_file_issue_number,
            observation_ids=tuple(observed),
            classification=evidence.classification,
        )

    def _mirror(self, entry: PatternRegistryEntry) -> None:
        """Project every committed shared fact, including no pending create."""
        assert entry.issue_number is not None
        self._local.mirror_pattern(
            signature=entry.signature,
            issue_number=entry.issue_number,
            observation_ids=entry.observation_ids,
            fix_class=entry.classification.fix_class,
            area=entry.classification.area,
            diagnosis=entry.classification.diagnosis,
        )
        self._local.discard_pending_case_file(signature=entry.signature)


class ReadOnlyPatternCaseFileRegistry(PatternCaseFileRegistry):
    """A registry view whose writes are unavailable by construction, not by flag.

    THE preview boundary for a command that promised to write nothing. A dry
    run still needs durable authority — it can only tell an operator what an
    apply would do by reading the real registry — but every other registry in
    this module writes on paths that read like reads. The mirrored registry's
    ``list_entries`` and ``read`` project each committed shared row into the
    local SQLite replica and discard any matching pending create intent, and its
    ``synchronize`` publishes this client's legacy rows to shared authority.

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


class LocalPatternCaseFileRegistry(PatternCaseFileRegistry):
    """Single-process implementation used by explicit offline/test composition."""

    def __init__(
        self,
        local: TechLeadAuthorityStore,
        *,
        before_write: Callable[[], None] = lambda: None,
    ) -> None:
        self._local = local
        self._before_write = before_write
        self._pending_observations: dict[
            str, tuple[str, PendingPatternObservation]
        ] = {}
        self._lifecycle: dict[str, tuple[CaseFileLifecycleTransition, ...]] = {}
        self._pending_retirements: dict[
            str, tuple[str, PendingPatternRetirement, str | None]
        ] = {}

    def reserve(self, pending: PendingCaseFile) -> PatternReservation:
        committed = self.read(signature=pending.signature)
        if committed is not None and committed.committed:
            if (
                self._local.load_pending_case_file(signature=pending.signature)
                is not None
            ):
                self._before_write()
                self._local.discard_pending_case_file(signature=pending.signature)
            return PatternReservation(PatternReservationState.COMMITTED, committed)
        existing = self._local.load_pending_case_file(signature=pending.signature)
        if existing is not None:
            return PatternReservation(
                PatternReservationState.RECOVERABLE, self._reserved(existing)
            )
        self._local.record_pending_case_file(pending=pending)
        return PatternReservation(
            PatternReservationState.ACQUIRED, self._reserved(pending)
        )

    def take_over(
        self, *, stale_reservation_id: str, pending: PendingCaseFile
    ) -> PatternReservation:
        existing = self._local.load_pending_case_file(signature=pending.signature)
        if (
            existing is not None
            and self._reservation_id(existing) != stale_reservation_id
        ):
            return PatternReservation(
                PatternReservationState.HELD, self._reserved(existing)
            )
        self._local.discard_pending_case_file(signature=pending.signature)
        self._local.record_pending_case_file(pending=pending)
        return PatternReservation(
            PatternReservationState.ACQUIRED, self._reserved(pending)
        )

    def finalize(
        self, *, signature: str, reservation_id: str, issue_number: int
    ) -> PatternRegistryEntry:
        pending = self._local.load_pending_case_file(signature=signature)
        if pending is None or self._reservation_id(pending) != reservation_id:
            current = self.read(signature=signature)
            if current is not None and current.issue_number == issue_number:
                return current
            raise PatternRegistryError(f"pattern {signature!r} reservation changed")
        self._local.record_pattern(
            signature=signature,
            issue_number=issue_number,
            observation_id=pending.body_observation_id,
            fix_class=pending.fix_class,
            area=pending.area,
            diagnosis=pending.diagnosis,
        )
        self._before_write()
        self._local.discard_pending_case_file(signature=signature)
        entry = self.read(signature=signature)
        assert entry is not None
        return entry

    def begin_creation_publication(
        self, *, signature: str, reservation_id: str
    ) -> PatternReservation:
        current = self.read(signature=signature)
        if current is None:
            raise PatternRegistryError(f"pattern {signature!r} has no reservation")
        if current.committed:
            return PatternReservation(PatternReservationState.COMMITTED, current)
        if current.reservation_id != reservation_id:
            return PatternReservation(PatternReservationState.HELD, current)
        return PatternReservation(PatternReservationState.ACQUIRED, current)

    def reserve_observation(
        self,
        *,
        signature: str,
        observation: "PatternObservation",
        classification: CaseFileClassification,
        issue_number: int,
    ) -> PatternReservation:
        current = self.read(signature=signature)
        if current is None or not current.committed:
            raise PatternRegistryError(
                f"pattern {signature!r} has no committed case file"
            )
        require_canonical_case_file(current, issue_number)
        current.classification.merged_with(classification, signature=signature)
        if observation.observation_id in current.observation_ids:
            return PatternReservation(PatternReservationState.COMMITTED, current)
        if signature in self._pending_retirements:
            return PatternReservation(PatternReservationState.HELD, current)
        existing = self._pending_observations.get(signature)
        if existing is None:
            existing = (
                uuid.uuid4().hex,
                PendingPatternObservation(
                    observation=observation, classification=classification
                ),
            )
            self._pending_observations[signature] = existing
            state = PatternReservationState.ACQUIRED
        else:
            state = PatternReservationState.RECOVERABLE
        reservation_id, pending = existing
        return PatternReservation(
            state,
            replace(
                current,
                reservation_id=reservation_id,
                pending_observation=pending,
            ),
        )

    def take_over_observation(
        self, *, signature: str, stale_reservation_id: str
    ) -> PatternReservation:
        existing = self._pending_observations.get(signature)
        if existing is None:
            current = self.read(signature=signature)
            if current is None:
                raise PatternRegistryError(
                    f"pattern {signature!r} has no committed case file"
                )
            return PatternReservation(PatternReservationState.COMMITTED, current)
        reservation_id, pending = existing
        if reservation_id != stale_reservation_id:
            current = self.read(signature=signature)
            assert current is not None
            return PatternReservation(PatternReservationState.HELD, current)
        replacement = (uuid.uuid4().hex, pending)
        self._pending_observations[signature] = replacement
        current = self.read(signature=signature)
        assert current is not None
        return PatternReservation(PatternReservationState.ACQUIRED, current)

    def begin_observation_publication(
        self, *, signature: str, reservation_id: str
    ) -> PatternReservation:
        current = self.read(signature=signature)
        if current is None:
            raise PatternRegistryError(
                f"pattern {signature!r} has no committed case file"
            )
        pending = self._pending_observations.get(signature)
        if pending is None:
            return PatternReservation(PatternReservationState.COMMITTED, current)
        if pending[0] != reservation_id:
            return PatternReservation(PatternReservationState.HELD, current)
        return PatternReservation(PatternReservationState.ACQUIRED, current)

    def finalize_observation(self, *, signature: str, reservation_id: str) -> bool:
        existing = self._pending_observations.get(signature)
        if existing is None:
            return False
        current_id, pending = existing
        if current_id != reservation_id:
            raise PatternRegistryError(
                f"pattern {signature!r} evidence reservation changed"
            )
        recorded = self._local.note_pattern_observation(
            signature=signature,
            observation_id=pending.observation.observation_id,
            fix_class=pending.classification.fix_class,
            area=pending.classification.area,
            diagnosis=pending.classification.diagnosis,
        )
        del self._pending_observations[signature]
        return recorded

    def record_lifecycle(
        self,
        *,
        signature: str,
        transition: CaseFileLifecycleTransition,
        expected_revision: str | None = None,
    ) -> PatternRegistryEntry:
        if transition.terminal:
            raise ValueError("terminal lifecycle changes require retirement")
        current = self._require_committed(signature)
        if admit_lifecycle_transition(current, transition):
            return current
        if current.pending_observation or current.pending_retirement:
            raise PatternRegistryError(
                f"pattern {signature!r} has another lifecycle effect in flight"
            )
        require_reviewed_revision(current, expected_revision)
        self._lifecycle[signature] = (*current.lifecycle, transition)
        return self._require_committed(signature)

    def reserve_retirement(
        self,
        *,
        signature: str,
        transition: CaseFileLifecycleTransition,
        comment: str,
        issue_number: int,
        expected_revision: str | None = None,
    ) -> PatternReservation:
        if not transition.terminal:
            raise ValueError("retirement requires a terminal disposition")
        desired = PendingPatternRetirement(transition=transition, comment=comment)
        current = self._require_committed(signature)
        require_canonical_case_file(current, issue_number)
        if admit_lifecycle_transition(current, transition):
            return PatternReservation(PatternReservationState.COMMITTED, current)
        existing = self._pending_retirements.get(signature)
        if existing is not None:
            _reservation_id, _pending, started = existing
            pending = require_resumable_retirement(current, desired)
            state = (
                PatternReservationState.RECOVERABLE
                if pending.phase is PatternRetirementPhase.CLOSE
                else PatternReservationState.PUBLISHING
                if started is not None
                else PatternReservationState.RECOVERABLE
            )
            return PatternReservation(state, self._require_committed(signature))
        if signature in self._pending_observations:
            return PatternReservation(PatternReservationState.HELD, current)
        require_reviewed_revision(current, expected_revision)
        self._pending_retirements[signature] = (
            uuid.uuid4().hex,
            desired,
            None,
        )
        return PatternReservation(
            PatternReservationState.ACQUIRED, self._require_committed(signature)
        )

    def take_over_retirement(
        self, *, signature: str, stale_reservation_id: str
    ) -> PatternReservation:
        current = self._require_committed(signature)
        existing = self._pending_retirements.get(signature)
        if existing is None:
            return PatternReservation(PatternReservationState.COMMITTED, current)
        if existing[0] != stale_reservation_id:
            return PatternReservation(PatternReservationState.HELD, current)
        return PatternReservation(PatternReservationState.ACQUIRED, current)

    def begin_retirement_publication(
        self, *, signature: str, reservation_id: str
    ) -> PatternReservation:
        current = self._require_committed(signature)
        existing = self._pending_retirements.get(signature)
        if existing is None:
            return PatternReservation(PatternReservationState.COMMITTED, current)
        if existing[0] != reservation_id:
            return PatternReservation(PatternReservationState.HELD, current)
        if existing[1].phase is PatternRetirementPhase.CLOSE:
            return PatternReservation(PatternReservationState.RECOVERABLE, current)
        if existing[2] is not None:
            return PatternReservation(PatternReservationState.PUBLISHING, current)
        self._pending_retirements[signature] = (existing[0], existing[1], "started")
        return PatternReservation(
            PatternReservationState.ACQUIRED, self._require_committed(signature)
        )

    def confirm_retirement_comment(
        self, *, signature: str, reservation_id: str
    ) -> PatternRegistryEntry:
        existing = self._pending_retirements.get(signature)
        if existing is None:
            return self._require_committed(signature)
        if existing[0] != reservation_id:
            raise PatternRegistryError(f"pattern {signature!r} retirement changed")
        self._pending_retirements[signature] = (
            existing[0],
            replace(existing[1], phase=PatternRetirementPhase.CLOSE),
            None,
        )
        return self._require_committed(signature)

    def finalize_retirement(
        self, *, signature: str, reservation_id: str
    ) -> PatternRegistryEntry:
        existing = self._pending_retirements.get(signature)
        if existing is None:
            return self._require_committed(signature)
        if existing[0] != reservation_id:
            raise PatternRegistryError(f"pattern {signature!r} retirement changed")
        if existing[1].phase is not PatternRetirementPhase.CLOSE:
            raise PatternRegistryError(
                f"pattern {signature!r} retirement comment is not confirmed"
            )
        current = self._require_committed(signature)
        self._lifecycle[signature] = (*current.lifecycle, existing[1].transition)
        del self._pending_retirements[signature]
        return self._require_committed(signature)

    def read(self, *, signature: str) -> PatternRegistryEntry | None:
        evidence = self._local.load_pattern_evidence(signature=signature)
        if evidence is None:
            pending = self._local.load_pending_case_file(signature=signature)
            return self._reserved(pending) if pending is not None else None
        observations = self._local.list_pattern_observation_ids(signature=signature)
        if not observations:
            raise PatternRegistryError(
                f"local pattern {signature!r} has no observation identities"
            )
        entry = PatternRegistryEntry(
            signature=signature,
            reservation_id=f"local:{hashlib.sha256(signature.encode()).hexdigest()[:16]}",
            claimant_id="single-instance",
            expires_at=datetime(1970, 1, 1, tzinfo=timezone.utc).isoformat(),
            pending=None,
            issue_number=evidence.case_file_issue_number,
            observation_ids=observations,
            classification=evidence.classification,
            lifecycle=self._lifecycle.get(signature, ()),
        )
        retirement = self._pending_retirements.get(signature)
        if retirement is not None:
            return replace(
                entry,
                reservation_id=retirement[0],
                pending_retirement=retirement[1],
                publication_started_at=retirement[2],
            )
        pending = self._pending_observations.get(signature)
        if pending is None:
            return entry
        reservation_id, observation = pending
        return replace(
            entry,
            reservation_id=reservation_id,
            pending_observation=observation,
        )

    def _require_committed(self, signature: str) -> PatternRegistryEntry:
        current = self.read(signature=signature)
        if current is None or not current.committed:
            raise PatternRegistryError(f"pattern {signature!r} has no committed case file")
        return current

    def has_observation(self, *, signature: str, observation_id: str) -> bool:
        return self._local.has_pattern_observation(
            signature=signature, observation_id=observation_id
        )

    def list_entries(self) -> tuple[PatternRegistryEntry, ...]:
        return tuple(
            entry
            for evidence in self._local.list_pattern_evidence()
            if (entry := self.read(signature=evidence.signature)) is not None
        )

    def seed_committed(self, entries: tuple[PatternRegistryEntry, ...]) -> None:
        for entry in entries:
            if entry.committed:
                assert entry.issue_number is not None
                self._local.mirror_pattern(
                    signature=entry.signature,
                    issue_number=entry.issue_number,
                    observation_ids=entry.observation_ids,
                    fix_class=entry.classification.fix_class,
                    area=entry.classification.area,
                    diagnosis=entry.classification.diagnosis,
                )

    def _reserved(self, pending: PendingCaseFile) -> PatternRegistryEntry:
        return PatternRegistryEntry(
            signature=pending.signature,
            reservation_id=self._reservation_id(pending),
            claimant_id="single-instance",
            expires_at=datetime(1970, 1, 1, tzinfo=timezone.utc).isoformat(),
            pending=pending,
            issue_number=None,
            observation_ids=(),
            classification=CaseFileClassification(),
        )

    @staticmethod
    def _reservation_id(pending: PendingCaseFile) -> str:
        return "local:" + hashlib.sha256(repr(pending).encode()).hexdigest()[:24]
