"""Detached facts for cold discovery of retained validated work."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .repository_engine_lifecycle import EngineIdentity, EngineStopAvailability
from .validated_work import (
    FinalizationPhase,
    LineageRole,
    UNRESOLVED_STATES,
    ValidatedWorkState,
    require_positive,
    require_sha,
    require_text,
)
from .validated_work_commands import AbandonStatus, ValidatedWorkDisposition


@dataclass(frozen=True, slots=True)
class ClaimOwnerFact:
    engine: EngineIdentity
    owner_fence: int
    stop_availability: EngineStopAvailability

    def __post_init__(self) -> None:
        if type(self.engine) is not EngineIdentity:
            raise ValueError("claim owner requires a typed engine")
        require_positive(self.owner_fence, "owner fence")
        if type(self.stop_availability) is not EngineStopAvailability:
            raise ValueError("claim owner requires typed stop availability")


@dataclass(frozen=True, slots=True)
class ValidatedWorkSnapshot:
    """One coherent, detached record/evidence/owner read snapshot."""

    disposition: ValidatedWorkDisposition
    record_id: str
    validated_head_sha: str
    worktree_head_sha: str
    branch_name: str
    expected_remote_head_sha: str | None
    superseded_evidence_ids: tuple[str, ...]
    attached_evidence_ids: tuple[str, ...]
    lineage_role: LineageRole
    escrow_retained: bool
    observation_revision: int
    waits_on_record_id: str
    owner: ClaimOwnerFact | None
    publish_attempts: int
    finalization_phase: FinalizationPhase
    updated_at: str
    can_recover: bool
    can_abandon: bool
    abandon_unavailable: AbandonStatus | None

    def __post_init__(self) -> None:
        self._validate_identity()
        self._validate_evidence()
        self._validate_lifecycle()
        self._validate_actions()

    def _validate_identity(self) -> None:
        if type(self.disposition) is not ValidatedWorkDisposition:
            raise ValueError("snapshot requires a typed disposition")
        if self.record_id != self.disposition.record_id:
            raise ValueError("snapshot and disposition record ids disagree")
        if self.validated_head_sha != self.disposition.key.validated_head_sha:
            raise ValueError("snapshot and disposition validated heads disagree")
        if self.branch_name != self.disposition.key.branch_name:
            raise ValueError("snapshot and disposition branches disagree")
        if self.lineage_role is not self.disposition.lineage_role:
            raise ValueError("snapshot and disposition lineage roles disagree")
        require_sha(self.validated_head_sha)

    def _validate_evidence(self) -> None:
        require_sha(self.worktree_head_sha)
        if self.expected_remote_head_sha is not None:
            require_sha(self.expected_remote_head_sha)
        self._validate_evidence_ids()
        if type(self.escrow_retained) is not bool:
            raise ValueError("escrow retention must be a boolean")
        require_positive(
            self.observation_revision, "observation revision", minimum=0
        )

    def _validate_lifecycle(self) -> None:
        if type(self.waits_on_record_id) is not str:
            raise ValueError("waits_on_record_id must be text")
        if self.owner is not None and type(self.owner) is not ClaimOwnerFact:
            raise ValueError("snapshot owner must be a typed owner fact")
        require_positive(self.publish_attempts, "publish attempts", minimum=0)
        if type(self.finalization_phase) is not FinalizationPhase:
            raise ValueError("snapshot requires a typed finalization phase")
        require_text(self.updated_at, "snapshot update time")

    def _validate_actions(self) -> None:
        if type(self.can_recover) is not bool or type(self.can_abandon) is not bool:
            raise ValueError("snapshot action availability must be boolean")
        if self.can_recover and (
            self.disposition.state
            not in {ValidatedWorkState.PARKED, ValidatedWorkState.FAILED}
            or self.lineage_role is not LineageRole.HEAD
            or self.owner is not None
            or not self.escrow_retained
        ):
            raise ValueError("recover availability contradicts retained-work facts")
        if self.can_abandon:
            if (
                self.disposition.state
                not in {ValidatedWorkState.PARKED, ValidatedWorkState.FAILED}
                or self.owner is not None
                or self.attached_evidence_ids
                or not self.escrow_retained
                or self.abandon_unavailable is not None
            ):
                raise ValueError("abandon availability contradicts retained-work facts")
        elif type(self.abandon_unavailable) is not AbandonStatus:
            raise ValueError("unavailable abandonment requires a typed refusal")
        if (
            self.abandon_unavailable is AbandonStatus.ATTACHED_EVIDENCE_PENDING
            and not self.attached_evidence_ids
        ):
            raise ValueError("attached-evidence refusal requires attached evidence")

    def _validate_evidence_ids(self) -> None:
        combined: list[str] = []
        for name, values in (
            ("superseded evidence", self.superseded_evidence_ids),
            ("attached evidence", self.attached_evidence_ids),
        ):
            if type(values) is not tuple:
                raise ValueError(f"{name} ids must be a tuple")
            for value in values:
                require_text(value, f"{name} id")
            combined.extend(values)
        if len(set(combined)) != len(combined):
            raise ValueError("snapshot evidence relations cannot repeat an id")


class WorkDiscoveryStatus(StrEnum):
    AVAILABLE = "available"
    DATABASE_ABSENT = "database_absent"
    UNREADABLE = "unreadable"
    UNSUPPORTED_SCHEMA = "unsupported_schema"


@dataclass(frozen=True, slots=True)
class ValidatedWorkDiscovery:
    status: WorkDiscoveryStatus
    records: tuple[ValidatedWorkSnapshot, ...]
    message: str

    def __post_init__(self) -> None:
        if type(self.status) is not WorkDiscoveryStatus:
            raise ValueError("discovery status must be typed")
        if type(self.records) is not tuple or any(
            type(record) is not ValidatedWorkSnapshot for record in self.records
        ):
            raise ValueError("discovery records must be typed snapshots in a tuple")
        require_text(self.message, "discovery message")
        if self.status is not WorkDiscoveryStatus.AVAILABLE and self.records:
            raise ValueError("unavailable discovery cannot carry records")
        record_ids = tuple(record.record_id for record in self.records)
        if len(set(record_ids)) != len(record_ids):
            raise ValueError("discovery cannot repeat a record")
        if any(
            record.disposition.state not in UNRESOLVED_STATES
            and record.owner is None
            for record in self.records
        ):
            raise ValueError(
                "discovery includes only unresolved or retained-owner records"
            )
