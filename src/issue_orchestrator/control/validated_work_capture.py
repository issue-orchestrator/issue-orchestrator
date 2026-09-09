"""Shared PARKED custody transaction ordering for automatic and historical intake."""

from ..domain.validated_work import ValidatedWorkEvidence, ValidatedWorkFailure, ValidatedWorkState
from ..domain.validated_work_escrow import EscrowArtifacts, escrow_locator, evidence_pins
from ..domain.validated_work_store import EvidenceAdmission, AdmissionOutcome
from ..ports.validated_work_escrow import ValidatedWorkEscrow
from ..ports.validated_work_preservation import ValidatedWorkAdmissionStore


class ParkedEvidenceCustody:
    def __init__(self, escrow: ValidatedWorkEscrow, store: ValidatedWorkAdmissionStore) -> None:
        self._escrow = escrow
        self._store = store

    def capture(self, evidence: ValidatedWorkEvidence, sources: EscrowArtifacts, *, reason: str, failure: ValidatedWorkFailure | None) -> AdmissionOutcome:
        pins = evidence_pins(evidence)
        proposed = EvidenceAdmission(
            evidence, ValidatedWorkState.PARKED, failure, reason,
            escrow_locator(evidence), pins[0][0], pins[1][0] if len(pins) == 2 else "",
            evidence.observations.captured_at,
        )
        # Capture returns original observations when a prior rename survived a crash.
        durable = self._escrow.capture(proposed, sources)
        return self._store.admit(durable)
