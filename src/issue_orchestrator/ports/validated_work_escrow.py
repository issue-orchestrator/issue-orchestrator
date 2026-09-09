"""Escrow capabilities: immutable bytes, diagnostic inventory and gated release."""

from typing import Protocol

from ..domain.validated_work_escrow import EscrowArtifacts, EscrowProblem, EscrowReport, VerifiedEscrowCapture
from ..domain.validated_work_store import EvidenceAdmission, EvidenceLookup, EvidenceRow


class EvidenceReader(Protocol):
    def evidence_for_id(self, evidence_id: str) -> EvidenceLookup | None: ...


class EvidenceReleaser(Protocol):
    def release(self, evidence: EvidenceRow) -> None: ...


class ValidatedWorkEscrow(EvidenceReleaser, Protocol):
    def capture(
        self, admission: EvidenceAdmission, sources: EscrowArtifacts
    ) -> EvidenceAdmission: ...

    def inspect(self, locator: str) -> EvidenceAdmission: ...

    def read_capture(self, locator: str) -> VerifiedEscrowCapture: ...

    def ensure_pins(self, admission: EvidenceAdmission) -> None: ...

    def verify_pins(self, admission: EvidenceAdmission) -> None: ...

    def inventory(self) -> tuple[tuple[str, ...], EscrowReport]: ...

    def orphan_pins(
        self, admissions: tuple[EvidenceAdmission, ...]
    ) -> tuple[EscrowProblem, ...]: ...
