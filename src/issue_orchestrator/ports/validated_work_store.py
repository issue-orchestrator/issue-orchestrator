"""Transactional disposition and fence contracts. Implementations own all atomicity."""

from __future__ import annotations
from ..domain.validated_work import ValidatedWorkFailure, ValidatedWorkState
from ..domain.validated_work_claim import (
    ProcessIdentity,
    RetainedClaim,
    ValidatedWorkClaim,
)
from ..domain.validated_work_commands import (
    AbandonValidatedWorkCommand,
    AbandonValidatedWorkOutcome,
    ValidatedWorkAuthoritySnapshot,
    ValidatedWorkDisposition,
    ValidatedWorkDispositionBatch,
)
from ..domain.validated_work_store import (
    AdmissionOutcome,
    DispositionPhase,
    EvidenceAdmission,
    EvidenceLookup,
    EvidenceRow,
    FinalizationPhase,
    LineagePublication,
    LineageResolutionRefusal,
    PublicationResolution,
    PublishAttempt,
    PublishValidatedHeadStatus,
    ValidatedWorkRecord,
)
from ..domain.validated_work_remote_authority import (
    RemoteAuthorityDecision,
    RemoteAuthorityRefreshRequest,
)
from typing import Protocol


class ValidatedWorkStore(Protocol):
    def retained_evidence(self, issue_number: int) -> tuple[EvidenceRow, ...]: ...
    def admit(self, admission: EvidenceAdmission) -> AdmissionOutcome: ...

    def get(self, record_id: str) -> ValidatedWorkDisposition: ...

    def record_for_id(self, record_id: str) -> ValidatedWorkRecord:
        """Typed durable facts, including lineage links and retained ownership."""
        ...

    def abandon_if_current(
        self, command: AbandonValidatedWorkCommand
    ) -> AbandonValidatedWorkOutcome: ...

    def for_issue(self, issue_number: int) -> ValidatedWorkDispositionBatch: ...

    def has_unresolved_work(self, issue_number: int) -> bool: ...

    def evidence_for_id(self, evidence_id: str) -> EvidenceLookup | None: ...

    def attached_evidence(self, record_id: str) -> tuple[EvidenceRow, ...]: ...

    def evidence_for_retention(
        self, *, released_before: str
    ) -> tuple[EvidenceRow, ...]: ...

    def release_evidence_for_retention(
        self, evidence_id: str, *, released_before: str, released_at: str,
    ) -> bool:
        """Recheck resolved eligibility and serialize deletion against admission."""
        ...

    def lineage_publication(self, lineage_key: str) -> LineagePublication | None: ...

    def acquire_claim(
        self,
        record_id: str,
        *,
        expected_states: frozenset[ValidatedWorkState],
        evidence_id: str,
    ) -> ValidatedWorkClaim | None:
        """Mint a new secret/fence using constructor-bound liveness, never time.

        A current owner must retain its private handle: this never reconstructs
        or returns an existing claim, even to the same process.
        """
        ...

    def holds_claim(self, claim: ValidatedWorkClaim) -> bool: ...

    def relinquish_claim(self, claim: ValidatedWorkClaim) -> bool:
        """Owner-only at a quiescent stage boundary; a stop reservation refuses.

        The slice-4 execution owner must serialize the complete operation and
        retain the claim through joined child work before invoking this method.
        """
        ...

    def owner_of(self, record_id: str) -> ProcessIdentity | None: ...

    def retained_claims(
        self, states: frozenset[ValidatedWorkState]
    ) -> tuple[RetainedClaim, ...]: ...

    def retained_claim(
        self, record_id: str, states: frozenset[ValidatedWorkState]
    ) -> RetainedClaim | None: ...

    def refresh_remote_authority(
        self,
        claim: ValidatedWorkClaim,
        request: RemoteAuthorityRefreshRequest,
        decision: RemoteAuthorityDecision,
        *,
        refreshed_at: str,
    ) -> ValidatedWorkDisposition | None:
        """Atomically replace mutable remote facts and their recovery gate."""
        ...

    def resolve_attached_evidence(
        self, claim: ValidatedWorkClaim, *, record_id: str, resolved_at: str
    ) -> ValidatedWorkDisposition | None: ...

    def begin_publish_attempt(
        self,
        claim: ValidatedWorkClaim,
        *,
        expected_attempt_no: int,
        target_head_sha: str,
        expected_remote_head: str,
        phase: DispositionPhase,
        started_at: str,
        authority: ValidatedWorkAuthoritySnapshot | None = None,
    ) -> PublishAttempt | None: ...

    def publish_attempts(self, record_id: str) -> tuple[PublishAttempt, ...]: ...

    def record_attempt_outcome(
        self,
        claim: ValidatedWorkClaim,
        attempt: PublishAttempt,
        *,
        outcome: PublishValidatedHeadStatus,
        failure: ValidatedWorkFailure | None,
        finished_at: str,
    ) -> bool: ...

    def record_finalization_phase(
        self, claim: ValidatedWorkClaim, *, phase: FinalizationPhase, recorded_at: str
    ) -> bool: ...

    def finalization_phase(self, record_id: str) -> FinalizationPhase: ...

    def record_pr_number(
        self, claim: ValidatedWorkClaim, *, pr_number: int
    ) -> bool: ...

    def fail(
        self,
        claim: ValidatedWorkClaim,
        *,
        failure: ValidatedWorkFailure,
        reason: str,
        failed_at: str,
    ) -> bool: ...

    def resolve_published(
        self,
        claim: ValidatedWorkClaim,
        *,
        record_id: str,
        published_head_sha: str,
        pre_push_expected: str,
        finalized_at: str,
    ) -> PublicationResolution | LineageResolutionRefusal: ...

    def resolve_observed_merge(
        self, *, record_id: str, merged_head_sha: str, observed_at: str
    ) -> PublicationResolution | LineageResolutionRefusal: ...


class ValidatedWorkFence(Protocol):
    def holds_claim(self, claim: ValidatedWorkClaim) -> bool: ...
