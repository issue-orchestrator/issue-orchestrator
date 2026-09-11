"""GitHub-ref backed Tech Lead pattern registry (#6789).

The whole bounded registry lives in one compare-and-swap ref.  Its record is
independent of editable issue prose, and a new client can reconstruct every
canonical signature mapping with one ref and one commit read.
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Callable

from ...domain.tech_lead_findings import (
    CaseFileClassification,
    CaseFileLifecycleTransition,
    PatternObservation,
    PendingCaseFile,
)
from ...infra import gh_audit
from ...ports.pattern_registry import (
    PatternCaseFileRegistry,
    PendingPatternObservation,
    PendingPatternRetirement,
    PatternRegistryEntry,
    PatternRegistryError,
    PatternRetirementPhase,
    PatternReservation,
    PatternReservationState,
    require_canonical_case_file,
)
from .ref_store import GitRefCasStore, GitRefSnapshot
from .pattern_registry_codec import format_entries, parse_entries

if TYPE_CHECKING:
    from .http_client import GitHubHttpClient


PATTERN_REGISTRY_REF_PREFIX = "refs/issue-orchestrator/registry"
PATTERN_REGISTRY_REF_KEY = "tech-lead-patterns"
MAX_CAS_ATTEMPTS = 5


class GitHubRefPatternRegistry(PatternCaseFileRegistry):
    """Atomic pattern registry over the repository's Git database."""

    def __init__(
        self,
        client: "GitHubHttpClient",
        *,
        claimant_id: str,
        lease_seconds: int,
        clock: Callable[[], datetime] | None = None,
        ref_prefix: str = PATTERN_REGISTRY_REF_PREFIX,
    ) -> None:
        if lease_seconds <= 0:
            raise ValueError("pattern registry lease_seconds must be positive")
        self._claimant_id = claimant_id
        self._lease_seconds = lease_seconds
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._store = GitRefCasStore(client, ref_prefix=ref_prefix)

    def reserve(self, pending: PendingCaseFile) -> PatternReservation:
        for _ in range(MAX_CAS_ATTEMPTS):
            snapshot, entries = self._load()
            current = entries.get(pending.signature)
            if current is not None:
                if current.committed:
                    return PatternReservation(
                        PatternReservationState.COMMITTED, current
                    )
                if current.publication_started_at is not None:
                    return PatternReservation(
                        PatternReservationState.PUBLISHING, current
                    )
                if not self._expired(current):
                    # A claimant ID names a process/configuration, not this
                    # call's ownership token.  Seeing an existing reservation
                    # from ourselves therefore means restart/retry recovery;
                    # only the successful CAS below returns ACQUIRED.
                    state = (
                        PatternReservationState.RECOVERABLE
                        if current.claimant_id == self._claimant_id
                        else PatternReservationState.HELD
                    )
                    return PatternReservation(state, current)
                return PatternReservation(PatternReservationState.RECOVERABLE, current)
            entry = self._reservation(pending)
            entries[pending.signature] = entry
            if self._commit(snapshot, entries):
                return PatternReservation(PatternReservationState.ACQUIRED, entry)
        raise PatternRegistryError("pattern registry kept changing during reservation")

    def take_over(
        self, *, stale_reservation_id: str, pending: PendingCaseFile
    ) -> PatternReservation:
        for _ in range(MAX_CAS_ATTEMPTS):
            snapshot, entries = self._load()
            current = entries.get(pending.signature)
            if current is None:
                return self.reserve(pending)
            if current.committed:
                return PatternReservation(PatternReservationState.COMMITTED, current)
            if current.publication_started_at is not None:
                return PatternReservation(PatternReservationState.PUBLISHING, current)
            if current.reservation_id != stale_reservation_id or not self._expired(
                current
            ):
                return PatternReservation(PatternReservationState.HELD, current)
            entry = self._reservation(pending)
            entries[pending.signature] = entry
            if self._commit(snapshot, entries):
                return PatternReservation(PatternReservationState.ACQUIRED, entry)
        raise PatternRegistryError(
            "pattern registry kept changing during stale takeover"
        )

    def finalize(
        self, *, signature: str, reservation_id: str, issue_number: int
    ) -> PatternRegistryEntry:
        if issue_number <= 0:
            raise ValueError("case-file issue_number must be positive")
        for _ in range(MAX_CAS_ATTEMPTS):
            snapshot, entries = self._load()
            current = entries.get(signature)
            if current is None:
                raise PatternRegistryError(f"pattern {signature!r} has no reservation")
            if current.committed:
                if current.issue_number != issue_number:
                    raise PatternRegistryError(
                        f"pattern {signature!r} is already committed to"
                        f" issue #{current.issue_number}"
                    )
                return current
            if current.reservation_id != reservation_id:
                raise PatternRegistryError(
                    f"pattern {signature!r} reservation ownership changed"
                )
            assert current.pending is not None
            entry = replace(
                current,
                pending=None,
                publication_started_at=None,
                issue_number=issue_number,
                observation_ids=(current.pending.body_observation_id,),
                classification=CaseFileClassification(
                    fix_class=current.pending.fix_class,
                    area=current.pending.area,
                    diagnosis=current.pending.diagnosis,
                ),
            )
            entries[signature] = entry
            if self._commit(snapshot, entries):
                return entry
        raise PatternRegistryError("pattern registry kept changing during finalization")

    def begin_creation_publication(
        self, *, signature: str, reservation_id: str
    ) -> PatternReservation:
        """Fence the token and make ambiguous creation non-reissuable."""
        for _ in range(MAX_CAS_ATTEMPTS):
            snapshot, entries = self._load()
            current = entries.get(signature)
            if current is None:
                raise PatternRegistryError(f"pattern {signature!r} has no reservation")
            if current.committed:
                return PatternReservation(PatternReservationState.COMMITTED, current)
            if current.reservation_id != reservation_id:
                return PatternReservation(PatternReservationState.HELD, current)
            if current.publication_started_at is not None:
                return PatternReservation(PatternReservationState.PUBLISHING, current)
            entry = replace(
                current,
                publication_started_at=self._aware_now().isoformat(),
            )
            entries[signature] = entry
            if self._commit(snapshot, entries):
                return PatternReservation(PatternReservationState.ACQUIRED, entry)
        raise PatternRegistryError(
            "pattern registry kept changing while starting publication"
        )

    def reserve_observation(
        self,
        *,
        signature: str,
        observation: PatternObservation,
        classification: CaseFileClassification,
    ) -> PatternReservation:
        for _ in range(MAX_CAS_ATTEMPTS):
            snapshot, entries = self._load()
            current = entries.get(signature)
            if current is None or not current.committed:
                raise PatternRegistryError(
                    f"pattern {signature!r} has no committed case file"
                )
            current.classification.merged_with(classification, signature=signature)
            if observation.observation_id in current.observation_ids:
                return PatternReservation(PatternReservationState.COMMITTED, current)
            if current.pending_retirement is not None:
                return PatternReservation(PatternReservationState.HELD, current)
            pending = current.pending_observation
            if pending is not None:
                if current.publication_started_at is not None:
                    return PatternReservation(
                        PatternReservationState.PUBLISHING, current
                    )
                state = (
                    PatternReservationState.RECOVERABLE
                    if self._expired(current)
                    else PatternReservationState.HELD
                )
                return PatternReservation(state, current)
            entry = replace(
                current,
                reservation_id=uuid.uuid4().hex,
                claimant_id=self._claimant_id,
                expires_at=(
                    self._aware_now() + timedelta(seconds=self._lease_seconds)
                ).isoformat(),
                pending_observation=PendingPatternObservation(
                    observation=observation, classification=classification
                ),
            )
            entries[signature] = entry
            if self._commit(snapshot, entries):
                return PatternReservation(PatternReservationState.ACQUIRED, entry)
        raise PatternRegistryError(
            "pattern registry kept changing during evidence admission"
        )

    def take_over_observation(
        self, *, signature: str, stale_reservation_id: str
    ) -> PatternReservation:
        for _ in range(MAX_CAS_ATTEMPTS):
            snapshot, entries = self._load()
            current = entries.get(signature)
            if current is None or not current.committed:
                raise PatternRegistryError(
                    f"pattern {signature!r} has no committed case file"
                )
            if current.pending_observation is None:
                return PatternReservation(PatternReservationState.COMMITTED, current)
            if current.publication_started_at is not None:
                return PatternReservation(PatternReservationState.PUBLISHING, current)
            if current.reservation_id != stale_reservation_id or (
                not self._expired(current)
            ):
                return PatternReservation(PatternReservationState.HELD, current)
            entry = self._reassigned_observation_entry(current)
            entries[signature] = entry
            if self._commit(snapshot, entries):
                return PatternReservation(PatternReservationState.ACQUIRED, entry)
        raise PatternRegistryError(
            "pattern registry kept changing during evidence takeover"
        )

    def begin_observation_publication(
        self, *, signature: str, reservation_id: str
    ) -> PatternReservation:
        for _ in range(MAX_CAS_ATTEMPTS):
            snapshot, entries = self._load()
            current = entries.get(signature)
            if current is None or not current.committed:
                raise PatternRegistryError(
                    f"pattern {signature!r} has no committed case file"
                )
            if current.pending_observation is None:
                return PatternReservation(PatternReservationState.COMMITTED, current)
            if current.reservation_id != reservation_id:
                return PatternReservation(PatternReservationState.HELD, current)
            if current.publication_started_at is not None:
                return PatternReservation(PatternReservationState.PUBLISHING, current)
            entry = replace(
                current,
                publication_started_at=self._aware_now().isoformat(),
            )
            entries[signature] = entry
            if self._commit(snapshot, entries):
                return PatternReservation(PatternReservationState.ACQUIRED, entry)
        raise PatternRegistryError(
            "pattern registry kept changing while starting evidence publication"
        )

    def finalize_observation(self, *, signature: str, reservation_id: str) -> bool:
        for _ in range(MAX_CAS_ATTEMPTS):
            snapshot, entries = self._load()
            current = entries.get(signature)
            if current is None or not current.committed:
                raise PatternRegistryError(
                    f"pattern {signature!r} has no committed case file"
                )
            pending = current.pending_observation
            if pending is None:
                return False
            if current.reservation_id != reservation_id:
                raise PatternRegistryError(
                    f"pattern {signature!r} evidence reservation changed"
                )
            observation_id = pending.observation.observation_id
            if observation_id in current.observation_ids:
                entries[signature] = replace(
                    current,
                    pending_observation=None,
                    publication_started_at=None,
                )
                if self._commit(snapshot, entries):
                    return False
                continue
            entry = replace(
                current,
                pending_observation=None,
                publication_started_at=None,
                observation_ids=(*current.observation_ids, observation_id),
                classification=current.classification.merged_with(
                    pending.classification, signature=signature
                ),
            )
            entries[signature] = entry
            if self._commit(snapshot, entries):
                return True
        raise PatternRegistryError(
            "pattern registry kept changing during evidence commit"
        )

    def read(self, *, signature: str) -> PatternRegistryEntry | None:
        return self._load()[1].get(signature)

    def has_observation(self, *, signature: str, observation_id: str) -> bool:
        entry = self.read(signature=signature)
        return entry is not None and observation_id in entry.observation_ids

    def record_lifecycle(
        self, *, signature: str, transition: CaseFileLifecycleTransition
    ) -> PatternRegistryEntry:
        if transition.terminal:
            raise ValueError("terminal lifecycle changes require retirement")
        for _ in range(MAX_CAS_ATTEMPTS):
            snapshot, entries = self._load()
            current = self._committed(entries, signature)
            replay = self._lifecycle_replay(current, transition)
            if replay is not None:
                return replay
            if current.pending_observation or current.pending_retirement:
                raise PatternRegistryError(
                    f"pattern {signature!r} has another lifecycle effect in flight"
                )
            if current.lifecycle and current.lifecycle[-1].terminal:
                raise PatternRegistryError(
                    f"pattern {signature!r} is terminal; reopening requires an"
                    " explicit reopen transition"
                )
            entry = replace(current, lifecycle=(*current.lifecycle, transition))
            entries[signature] = entry
            if self._commit(snapshot, entries):
                return entry
        raise PatternRegistryError("pattern registry kept changing during lifecycle update")

    def reserve_retirement(
        self,
        *,
        signature: str,
        transition: CaseFileLifecycleTransition,
        comment: str,
        issue_number: int,
    ) -> PatternReservation:
        if not transition.terminal:
            raise ValueError("retirement requires a terminal disposition")
        desired = PendingPatternRetirement(transition=transition, comment=comment)
        for _ in range(MAX_CAS_ATTEMPTS):
            snapshot, entries = self._load()
            current = self._committed(entries, signature)
            require_canonical_case_file(current, issue_number)
            replay = self._lifecycle_replay(current, transition)
            if replay is not None:
                return PatternReservation(PatternReservationState.COMMITTED, replay)
            if current.lifecycle and current.lifecycle[-1].terminal:
                raise PatternRegistryError(
                    f"pattern {signature!r} already has terminal disposition"
                    f" {current.disposition!r}"
                )
            pending = current.pending_retirement
            if pending is not None:
                return self._existing_retirement(current, desired)
            if current.pending_observation is not None:
                return PatternReservation(PatternReservationState.HELD, current)
            entry = replace(
                current,
                reservation_id=uuid.uuid4().hex,
                claimant_id=self._claimant_id,
                expires_at=(
                    self._aware_now() + timedelta(seconds=self._lease_seconds)
                ).isoformat(),
                pending_retirement=desired,
            )
            entries[signature] = entry
            if self._commit(snapshot, entries):
                return PatternReservation(PatternReservationState.ACQUIRED, entry)
        raise PatternRegistryError("pattern registry kept changing during retirement")

    def _existing_retirement(
        self,
        current: PatternRegistryEntry,
        desired: PendingPatternRetirement,
    ) -> PatternReservation:
        pending = current.pending_retirement
        assert pending is not None
        if not pending.transition.same_intent(desired.transition):
            raise PatternRegistryError(
                f"pattern {current.signature!r} has a different retirement in flight"
            )
        if pending.comment != desired.comment:
            raise PatternRegistryError(
                f"pattern {current.signature!r} retirement comment changed"
            )
        if pending.phase is PatternRetirementPhase.CLOSE:
            return PatternReservation(PatternReservationState.RECOVERABLE, current)
        if current.publication_started_at is not None:
            return PatternReservation(PatternReservationState.PUBLISHING, current)
        state = (
            PatternReservationState.RECOVERABLE
            if current.claimant_id == self._claimant_id or self._expired(current)
            else PatternReservationState.HELD
        )
        return PatternReservation(state, current)

    def take_over_retirement(
        self, *, signature: str, stale_reservation_id: str
    ) -> PatternReservation:
        for _ in range(MAX_CAS_ATTEMPTS):
            snapshot, entries = self._load()
            current = self._committed(entries, signature)
            pending = current.pending_retirement
            if pending is None:
                return PatternReservation(PatternReservationState.COMMITTED, current)
            if pending.phase is PatternRetirementPhase.CLOSE:
                return PatternReservation(PatternReservationState.RECOVERABLE, current)
            if current.publication_started_at is not None:
                return PatternReservation(PatternReservationState.PUBLISHING, current)
            if current.reservation_id != stale_reservation_id or not self._expired(current):
                return PatternReservation(PatternReservationState.HELD, current)
            entry = replace(
                current,
                reservation_id=uuid.uuid4().hex,
                claimant_id=self._claimant_id,
                expires_at=(
                    self._aware_now() + timedelta(seconds=self._lease_seconds)
                ).isoformat(),
            )
            entries[signature] = entry
            if self._commit(snapshot, entries):
                return PatternReservation(PatternReservationState.ACQUIRED, entry)
        raise PatternRegistryError("pattern registry kept changing during retirement takeover")

    def begin_retirement_publication(
        self, *, signature: str, reservation_id: str
    ) -> PatternReservation:
        for _ in range(MAX_CAS_ATTEMPTS):
            snapshot, entries = self._load()
            current = self._committed(entries, signature)
            if current.pending_retirement is None:
                return PatternReservation(PatternReservationState.COMMITTED, current)
            if current.reservation_id != reservation_id:
                return PatternReservation(PatternReservationState.HELD, current)
            if current.pending_retirement.phase is PatternRetirementPhase.CLOSE:
                return PatternReservation(PatternReservationState.RECOVERABLE, current)
            if current.publication_started_at is not None:
                return PatternReservation(PatternReservationState.PUBLISHING, current)
            entry = replace(current, publication_started_at=self._aware_now().isoformat())
            entries[signature] = entry
            if self._commit(snapshot, entries):
                return PatternReservation(PatternReservationState.ACQUIRED, entry)
        raise PatternRegistryError("pattern registry kept changing while starting retirement")

    def confirm_retirement_comment(
        self, *, signature: str, reservation_id: str
    ) -> PatternRegistryEntry:
        for _ in range(MAX_CAS_ATTEMPTS):
            snapshot, entries = self._load()
            current = self._committed(entries, signature)
            pending = current.pending_retirement
            if pending is None:
                return current
            if current.reservation_id != reservation_id:
                raise PatternRegistryError(f"pattern {signature!r} retirement changed")
            if pending.phase is PatternRetirementPhase.CLOSE:
                return current
            entry = replace(
                current,
                pending_retirement=replace(pending, phase=PatternRetirementPhase.CLOSE),
                publication_started_at=None,
            )
            entries[signature] = entry
            if self._commit(snapshot, entries):
                return entry
        raise PatternRegistryError("pattern registry kept changing after retirement comment")

    def finalize_retirement(
        self, *, signature: str, reservation_id: str
    ) -> PatternRegistryEntry:
        for _ in range(MAX_CAS_ATTEMPTS):
            snapshot, entries = self._load()
            current = self._committed(entries, signature)
            pending = current.pending_retirement
            if pending is None:
                return current
            if current.reservation_id != reservation_id:
                raise PatternRegistryError(f"pattern {signature!r} retirement changed")
            if pending.phase is not PatternRetirementPhase.CLOSE:
                raise PatternRegistryError(
                    f"pattern {signature!r} retirement comment is not confirmed"
                )
            entry = replace(
                current,
                lifecycle=(*current.lifecycle, pending.transition),
                pending_retirement=None,
                publication_started_at=None,
            )
            entries[signature] = entry
            if self._commit(snapshot, entries):
                return entry
        raise PatternRegistryError("pattern registry kept changing during retirement commit")

    def list_entries(self) -> tuple[PatternRegistryEntry, ...]:
        entries = self._load()[1]
        return tuple(entries[key] for key in sorted(entries))

    def seed_committed(self, entries: tuple[PatternRegistryEntry, ...]) -> None:
        """Merge local rows once when deploying the shared registry.

        Existing shared rows win identity.  A different canonical issue is a
        hard conflict; matching rows merge evidence and classification so two
        clients upgrading concurrently cannot discard either local history.
        """
        seeds = entries
        for _ in range(MAX_CAS_ATTEMPTS):
            snapshot, current_entries = self._load()
            changed = False
            for seed in seeds:
                if not seed.committed:
                    raise ValueError("only committed pattern rows can be seeded")
                current = current_entries.get(seed.signature)
                if current is None:
                    current_entries[seed.signature] = seed
                    changed = True
                    continue
                if current.issue_number != seed.issue_number:
                    raise PatternRegistryError(
                        f"pattern {seed.signature!r} maps to both issue"
                        f" #{current.issue_number} and #{seed.issue_number}"
                    )
                merged = current.classification.merged_with(
                    seed.classification, signature=seed.signature
                )
                observations = tuple(
                    dict.fromkeys((*current.observation_ids, *seed.observation_ids))
                )
                combined = replace(
                    current, classification=merged, observation_ids=observations
                )
                if combined != current:
                    current_entries[seed.signature] = combined
                    changed = True
            if snapshot is not None and not changed:
                return
            if self._commit(snapshot, current_entries):
                return
        raise PatternRegistryError("pattern registry kept changing during local import")

    def _reservation(self, pending: PendingCaseFile) -> PatternRegistryEntry:
        now = self._aware_now()
        return PatternRegistryEntry(
            signature=pending.signature,
            reservation_id=uuid.uuid4().hex,
            claimant_id=self._claimant_id,
            expires_at=(now + timedelta(seconds=self._lease_seconds)).isoformat(),
            pending=pending,
            issue_number=None,
            observation_ids=(),
            classification=CaseFileClassification(),
        )

    @staticmethod
    def _committed(
        entries: dict[str, PatternRegistryEntry], signature: str
    ) -> PatternRegistryEntry:
        current = entries.get(signature)
        if current is None or not current.committed:
            raise PatternRegistryError(f"pattern {signature!r} has no committed case file")
        return current

    @staticmethod
    def _lifecycle_replay(
        current: PatternRegistryEntry, transition: CaseFileLifecycleTransition
    ) -> PatternRegistryEntry | None:
        for recorded in current.lifecycle:
            if recorded.transition_id != transition.transition_id:
                continue
            if not recorded.same_intent(transition):
                raise PatternRegistryError(
                    f"lifecycle transition {transition.transition_id!r} changed payload"
                )
            return current
        return None

    def _reassigned_observation_entry(
        self, entry: PatternRegistryEntry
    ) -> PatternRegistryEntry:
        return replace(
            entry,
            reservation_id=uuid.uuid4().hex,
            claimant_id=self._claimant_id,
            expires_at=(
                self._aware_now() + timedelta(seconds=self._lease_seconds)
            ).isoformat(),
            publication_started_at=None,
        )

    def _expired(self, entry: PatternRegistryEntry) -> bool:
        expires = datetime.fromisoformat(entry.expires_at)
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        return expires <= self._aware_now()

    def _aware_now(self) -> datetime:
        now = self._clock()
        return now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)

    def _load(self) -> tuple[GitRefSnapshot | None, dict[str, PatternRegistryEntry]]:
        try:
            with gh_audit.context(
                reason=gh_audit.AuditReason.GH_READ,
                issue_key=PATTERN_REGISTRY_REF_KEY,
                scope=gh_audit.AuditScope.UNKNOWN,
            ):
                snapshot = self._store.read(PATTERN_REGISTRY_REF_KEY)
            return snapshot, parse_entries(snapshot.message) if snapshot else {}
        except PatternRegistryError:
            raise
        except Exception as exc:
            raise PatternRegistryError(
                f"could not read shared pattern registry: {exc}"
            ) from exc

    def _commit(
        self,
        snapshot: GitRefSnapshot | None,
        entries: dict[str, PatternRegistryEntry],
    ) -> bool:
        message = format_entries(entries)
        try:
            with gh_audit.context(
                reason=gh_audit.AuditReason.GH_WRITE,
                issue_key=PATTERN_REGISTRY_REF_KEY,
                scope=gh_audit.AuditScope.UNKNOWN,
            ):
                if snapshot is None:
                    return self._store.create(PATTERN_REGISTRY_REF_KEY, message)
                return self._store.update(snapshot, message)
        except Exception as exc:
            raise PatternRegistryError(
                f"could not update shared pattern registry: {exc}"
            ) from exc
