"""Local replica and single-instance implementations of the pattern registry."""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from typing import Callable

from ..domain.tech_lead_findings import (
    CaseFileClassification,
    CaseFileLifecycleTransition,
    PatternObservation,
    PendingCaseFile,
)
from ..ports.pattern_registry import (
    PendingPatternObservation,
    PendingPatternRetirement,
    PatternCaseFileRegistry,
    PatternRegistryEntry,
    PatternRegistryError,
    PatternRetirementPhase,
    PatternReservation,
    PatternReservationState,
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
        """Merge rolling-upgrade rows, then rebuild this client's local cache."""
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
    ) -> PatternReservation:
        outcome = self._shared.reserve_observation(
            signature=signature,
            observation=observation,
            classification=classification,
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
        self, *, signature: str, transition: CaseFileLifecycleTransition
    ) -> PatternRegistryEntry:
        return self._shared.record_lifecycle(signature=signature, transition=transition)

    def reserve_retirement(
        self,
        *,
        signature: str,
        transition: CaseFileLifecycleTransition,
        comment: str,
    ) -> PatternReservation:
        return self._shared.reserve_retirement(
            signature=signature, transition=transition, comment=comment
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
    ) -> PatternReservation:
        current = self.read(signature=signature)
        if current is None or not current.committed:
            raise PatternRegistryError(
                f"pattern {signature!r} has no committed case file"
            )
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
        self, *, signature: str, transition: CaseFileLifecycleTransition
    ) -> PatternRegistryEntry:
        if transition.terminal:
            raise ValueError("terminal lifecycle changes require retirement")
        current = self._require_committed(signature)
        lifecycle = self._lifecycle.setdefault(signature, current.lifecycle)
        for item in lifecycle:
            if item.transition_id != transition.transition_id:
                continue
            if item.same_intent(transition):
                return current
            raise PatternRegistryError(
                f"lifecycle transition {transition.transition_id!r} changed payload"
            )
        if lifecycle and lifecycle[-1].terminal:
            raise PatternRegistryError(
                f"pattern {signature!r} is terminal; reopening must be explicit"
            )
        self._lifecycle[signature] = (*lifecycle, transition)
        return self._require_committed(signature)

    def reserve_retirement(
        self,
        *,
        signature: str,
        transition: CaseFileLifecycleTransition,
        comment: str,
    ) -> PatternReservation:
        if not transition.terminal:
            raise ValueError("retirement requires a terminal disposition")
        current = self._require_committed(signature)
        for item in current.lifecycle:
            if item.transition_id == transition.transition_id:
                if item.same_intent(transition):
                    return PatternReservation(PatternReservationState.COMMITTED, current)
                raise PatternRegistryError(
                    f"lifecycle transition {transition.transition_id!r} changed payload"
                )
        existing = self._pending_retirements.get(signature)
        if existing is not None:
            reservation_id, pending, started = existing
            if (
                not pending.transition.same_intent(transition)
                or pending.comment != comment
            ):
                raise PatternRegistryError(
                    f"pattern {signature!r} has a different retirement in flight"
                )
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
        reservation_id = uuid.uuid4().hex
        self._pending_retirements[signature] = (
            reservation_id,
            PendingPatternRetirement(transition=transition, comment=comment),
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
