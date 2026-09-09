"""Durable evidence, lineage and attempt values; no storage handles cross ports."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .validated_work import (
    EvidenceRole as EvidenceRole,
    PublicationProvenance as PublicationProvenance,
    FinalizationPhase as FinalizationPhase,
    DispositionPhase as DispositionPhase,
    PublishValidatedHeadStatus as PublishValidatedHeadStatus,
    ResolutionKind,
    RemoteBaselineStatus,
    ValidatedWorkEvidence,
    ValidatedWorkFailure,
    ValidatedWorkKey,
    ValidatedWorkState,
    require_positive,
    require_sha,
    require_text,
)
from .validated_work_claim import ProcessIdentity
from .validated_work_commands import (
    ValidatedWorkAuthoritySnapshot,
    ValidatedWorkDisposition,
)


class EvidenceAdmissionSelection(StrEnum):
    CURRENT = "current"
    RETAIN = "retain"


class AdmissionStatus(StrEnum):
    ADMITTED = "admitted"
    CONVERGED = "converged"
    ATTACHED = "attached"
    RETAINED = "retained"
    ALREADY_RECOVERED = "already_recovered"
    REOPENED = "reopened"
    SUPERSEDES = "supersedes"


@dataclass(frozen=True, slots=True)
class EvidenceAdmission:
    """Already-attested capture and its immutable admission audit (slice 1c producer)."""

    evidence: ValidatedWorkEvidence
    initial_state: ValidatedWorkState
    initial_failure: ValidatedWorkFailure | None
    initial_reason: str
    escrow_dir: str
    pinned_ref: str
    observed_ref: str
    admitted_at: str

    def __post_init__(self) -> None:
        if type(self.evidence) is not ValidatedWorkEvidence:
            raise ValueError("admission requires typed evidence")
        if self.initial_state not in {
            ValidatedWorkState.QUEUED,
            ValidatedWorkState.PARKED,
            ValidatedWorkState.FAILED,
        }:
            raise ValueError("admission must be queued, parked or failed")
        if type(self.initial_state) is not ValidatedWorkState:
            raise ValueError("admission state must be typed")
        if (
            self.initial_failure is not None
            and type(self.initial_failure) is not ValidatedWorkFailure
        ):
            raise ValueError("admission failure must be typed")
        if (
            self.initial_state is ValidatedWorkState.FAILED
            and self.initial_failure is None
        ):
            raise ValueError("failed admission requires failure")
        require_text(self.escrow_dir, "escrow_dir")
        if self.escrow_dir.startswith("/") or ".." in self.escrow_dir.split("/"):
            raise ValueError("escrow locator must be relative and contained")
        require_text(self.pinned_ref, "pinned_ref")
        require_text(self.admitted_at, "admitted_at")

    def require_capture_gate(self) -> None:
        """Applied only to NEW capture, never re-derived from refreshed observations."""
        if self.initial_state is ValidatedWorkState.QUEUED:
            identity, observations = self.evidence.identity, self.evidence.observations
            if (
                self.initial_failure is not None
                or not identity.branch_binding_verified
                or observations.worktree_head_sha != identity.key.validated_head_sha
                or observations.remote_baseline_status is not RemoteBaselineStatus.OBSERVED
            ):
                raise ValueError("approval-required evidence cannot be queued")


@dataclass(frozen=True, slots=True)
class EvidenceRow:
    admission: EvidenceAdmission
    base_state: ValidatedWorkState
    base_failure: ValidatedWorkFailure | None
    base_reason: str
    role: EvidenceRole
    observation_revision: int
    role_changed_at: str
    released_at: str

    def __post_init__(self) -> None:
        if (
            type(self.admission) is not EvidenceAdmission
            or type(self.role) is not EvidenceRole
            or type(self.base_state) is not ValidatedWorkState
        ):
            raise ValueError("evidence row requires typed admission and role")
        if self.base_state not in {
            ValidatedWorkState.QUEUED,
            ValidatedWorkState.PARKED,
            ValidatedWorkState.FAILED,
        }:
            raise ValueError("evidence base gate must precede publication")
        if self.base_failure is not None and type(self.base_failure) is not ValidatedWorkFailure:
            raise ValueError("evidence base failure must be typed")
        if self.base_state is ValidatedWorkState.FAILED and self.base_failure is None:
            raise ValueError("failed evidence base gate requires a failure")
        require_text(self.base_reason, "evidence base reason")
        require_positive(self.observation_revision, "observation_revision", minimum=0)
        require_text(self.role_changed_at, "role_changed_at")

    @property
    def evidence_id(self) -> str:
        return self.admission.evidence.evidence_id

    @property
    def record_id(self) -> str:
        return self.admission.evidence.record_id

    @property
    def authority(self) -> ValidatedWorkAuthoritySnapshot:
        ev = self.admission.evidence
        key, obs = ev.identity.key, ev.observations
        return ValidatedWorkAuthoritySnapshot(
            self.record_id,
            self.evidence_id,
            self.observation_revision,
            key.validated_head_sha,
            key.branch_name,
            key.repo_slug,
            key.issue_number,
            obs.pr_number,
            obs.expected_remote_head_sha,
            obs.remote_baseline_status,
        )


@dataclass(frozen=True, slots=True)
class EvidenceLookup:
    evidence: EvidenceRow
    record: ValidatedWorkDisposition


@dataclass(frozen=True, slots=True)
class AdmissionOutcome:
    status: AdmissionStatus
    disposition: ValidatedWorkDisposition


@dataclass(frozen=True, slots=True)
class LineagePublication:
    lineage_key: str
    published_head_sha: str
    published_by_record_id: str
    published_via: PublicationProvenance
    published_pre_push_expected: str
    published_at: str

    def __post_init__(self) -> None:
        require_text(self.lineage_key, "lineage_key")
        require_text(self.published_by_record_id, "published_by_record_id")
        require_sha(self.published_head_sha)
        require_text(self.published_at, "published_at")
        if type(self.published_via) is not PublicationProvenance:
            raise ValueError("publication provenance must be typed")
        if self.published_pre_push_expected:
            require_sha(self.published_pre_push_expected)
            if self.published_via is PublicationProvenance.OBSERVED_MERGE:
                raise ValueError("an observed merge proves no pre-push baseline")


@dataclass(frozen=True, slots=True)
class PublicationResolution:
    record: ValidatedWorkDisposition
    lineage: LineagePublication
    resolved_ancestors: tuple[ValidatedWorkDisposition, ...]
    failed_ancestors: tuple[ValidatedWorkDisposition, ...]
    classified_waiters: tuple[ValidatedWorkDisposition, ...]


class LineageResolutionRefusal(StrEnum):
    STALE_CLAIM = "stale_claim"
    PUBLICATION_IN_FLIGHT = "publication_in_flight"
    NOT_A_DESCENDANT = "not_a_descendant"
    CONTAINMENT_UNPROVEN = "containment_unproven"
    FINALIZATION_INCOMPLETE = "finalization_incomplete"


@dataclass(frozen=True, slots=True)
class PublishAttempt:
    record_id: str
    attempt_no: int
    evidence_id: str
    target_head_sha: str
    expected_remote_head: str
    phase: DispositionPhase
    fence: int
    started_at: str
    outcome: PublishValidatedHeadStatus | None = None
    failure: ValidatedWorkFailure | None = None
    finished_at: str = ""

    @property
    def succeeded(self) -> bool:
        return self.outcome in {
            PublishValidatedHeadStatus.PUBLISHED,
            PublishValidatedHeadStatus.ALREADY_AT_TARGET,
        }

    def succeeded_for(self, authority: ValidatedWorkAuthoritySnapshot) -> bool:
        """A valid success proves exact work, or a freshly observed equivalent.

        The current owner may be a successor; its fence is not part of this
        durable publication fact's binding. A migrated or initially unobserved
        baseline cannot match a later authority refresh byte-for-byte. Fresh
        exact target-and-PR observation makes that baseline difference harmless:
        publication already succeeded and finalization is the only allowed work.
        """
        if not self.succeeded:
            return False
        if (
            self.record_id != authority.record_id
            or self.evidence_id != authority.evidence_id
            or self.target_head_sha != authority.validated_head_sha
        ):
            raise ValueError(
                "successful attempt does not match current publication evidence"
            )
        if self.expected_remote_head == (authority.expected_remote_head_sha or ""):
            return True
        if (
            authority.remote_baseline_status is RemoteBaselineStatus.OBSERVED
            and authority.expected_remote_head_sha == authority.validated_head_sha
            and authority.pr_number is not None
        ):
            return True
        raise ValueError(
            "successful attempt does not match current publication evidence"
        )

    def __post_init__(self) -> None:
        require_text(self.started_at, "started_at")
        require_positive(self.attempt_no, "attempt_no")
        require_positive(self.fence, "fence")
        require_text(self.record_id, "record_id")
        require_text(self.evidence_id, "evidence_id")
        require_sha(self.target_head_sha)
        if self.expected_remote_head:
            require_sha(self.expected_remote_head)
        if type(self.phase) is not DispositionPhase:
            raise ValueError("attempt phase must be typed")
        self._validate_outcome()

    def _validate_outcome(self) -> None:
        if self.outcome is None:
            if self.failure is not None or self.finished_at:
                raise ValueError("an outcome-less attempt cannot have completion facts")
            return
        if (
            type(self.outcome) is not PublishValidatedHeadStatus
            or self.outcome is PublishValidatedHeadStatus.SUPERSEDED
        ):
            raise ValueError("only a real, typed publisher outcome can be persisted")
        require_text(self.finished_at, "finished_at")
        if self.succeeded and self.failure is not None:
            raise ValueError("successful attempts cannot carry failure")
        if not self.succeeded and type(self.failure) is not ValidatedWorkFailure:
            raise ValueError("unsuccessful attempts require enumerated failure")


class AncestryRelation(StrEnum):
    EQUAL = "equal"
    ANCESTOR = "ancestor"
    DESCENDANT = "descendant"
    DIVERGENT = "divergent"
    LEFT_UNREACHABLE = "left_unreachable"
    RIGHT_UNREACHABLE = "right_unreachable"
    BOTH_UNREACHABLE = "both_unreachable"


@dataclass(frozen=True, slots=True)
class CommitReference:
    """Exact repository object with its retention ref, never a worktree HEAD."""

    key: ValidatedWorkKey
    pinned_ref: str


@dataclass(frozen=True, slots=True)
class ValidatedWorkRecord:
    """Store-owned facts needed by future owners, without exposing SQL rows."""

    disposition: ValidatedWorkDisposition
    current_evidence: EvidenceRow
    lineage_key: str
    superseded_by_record_id: str
    waits_on_record_id: str
    owner_fence: int
    owner: ProcessIdentity | None
    finalization_phase: FinalizationPhase
    publishing_started_at: str
    resolution_kind: ResolutionKind | None
    resolved_at: str
    created_at: str
    updated_at: str
    terminal_at: str
