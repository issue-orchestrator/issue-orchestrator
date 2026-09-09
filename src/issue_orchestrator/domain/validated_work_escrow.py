"""Immutable capture and named artifact locations, independent of store rows."""

import hashlib
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from .validated_work import AdmittedArtifact, ArtifactSlot, ValidatedWorkEvidence
from .validated_work_store import EvidenceAdmission


ARTIFACT_FILENAMES = MappingProxyType(
    {
        ArtifactSlot.COMPLETION: "completion.json",
        ArtifactSlot.VALIDATION: "validation.json",
        ArtifactSlot.EXCHANGE_SUMMARY: "exchange-summary.md",
    }
)


def evidence_artifacts(evidence: ValidatedWorkEvidence) -> tuple[AdmittedArtifact, ...]:
    identity = evidence.identity
    required = (identity.completion_artifact, identity.validation_artifact)
    return (
        required
        if identity.exchange_summary_artifact is None
        else (*required, identity.exchange_summary_artifact)
    )


def escrow_locator(evidence: ValidatedWorkEvidence) -> str:
    return f"{evidence.identity.key.issue_number}/{evidence.evidence_id}"


def evidence_pins(evidence: ValidatedWorkEvidence) -> tuple[tuple[str, str], ...]:
    key = evidence.identity.key
    # ':' is forbidden by git-check-ref-format. This encoding is lossless for v1 IDs.
    suffix = f"{key.issue_number}/{evidence.evidence_id.replace(':', '-')}"
    pins = ((f"refs/issue-orchestrator/validated/{suffix}", key.validated_head_sha),)
    observed = evidence.observations.worktree_head_sha
    if observed != key.validated_head_sha:
        return (*pins, (f"refs/issue-orchestrator/observed/{suffix}", observed))
    return pins


def validate_capture_locations(admission: EvidenceAdmission) -> None:
    pins = evidence_pins(admission.evidence)
    if (admission.escrow_dir, admission.pinned_ref, admission.observed_ref) != (
        escrow_locator(admission.evidence),
        pins[0][0],
        pins[1][0] if len(pins) == 2 else "",
    ):
        raise ValueError("capture locations must match exact evidence identity")


@dataclass(frozen=True, slots=True)
class EscrowArtifacts:
    """Intake-owned, named source files. Audit observations are never read as input."""

    completion: Path
    validation: Path
    exchange_summary: Path | None

    def for_slot(self, slot: ArtifactSlot) -> Path:
        match slot:
            case ArtifactSlot.COMPLETION:
                return self.completion
            case ArtifactSlot.VALIDATION:
                return self.validation
            case ArtifactSlot.EXCHANGE_SUMMARY:
                if self.exchange_summary is None:
                    raise ValueError("missing exchange summary source")
                return self.exchange_summary


@dataclass(frozen=True, slots=True)
class EscrowProblem:
    locator: str
    detail: str


@dataclass(frozen=True, slots=True)
class EscrowReport:
    repaired: tuple[str, ...] = ()
    problems: tuple[EscrowProblem, ...] = ()


@dataclass(frozen=True, slots=True)
class VerifiedEscrowCapture:
    """Immutable artifact bytes authenticated against one capture identity."""

    admission: EvidenceAdmission
    completion: bytes
    validation: bytes
    exchange_summary: bytes | None

    def __post_init__(self) -> None:
        for artifact in evidence_artifacts(self.admission.evidence):
            data = self.for_slot(artifact.slot)
            if type(data) is not bytes:
                raise ValueError("capture artifact bytes must be immutable")
            if len(data) != artifact.byte_size or hashlib.sha256(data).hexdigest() != artifact.sha256:
                raise ValueError(f"artifact bytes do not match capture: {artifact.slot}")
        if self.admission.evidence.identity.exchange_summary_artifact is None and self.exchange_summary is not None:
            raise ValueError("capture contains an unadmitted exchange summary")

    def for_slot(self, slot: ArtifactSlot) -> bytes:
        match slot:
            case ArtifactSlot.COMPLETION:
                return self.completion
            case ArtifactSlot.VALIDATION:
                return self.validation
            case ArtifactSlot.EXCHANGE_SUMMARY:
                if self.exchange_summary is None:
                    raise ValueError("capture lacks admitted exchange summary")
                return self.exchange_summary
