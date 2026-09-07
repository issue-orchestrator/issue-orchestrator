"""One admission owner orders immutable receipts, including orphan replay."""

from ..domain.validated_work_store import (EvidenceAdmission, EvidenceAdmissionSelection, AdmissionOutcome, EvidenceLookup, EvidenceRow)
from ..domain.validated_work_commands import ValidatedWorkDispositionBatch
from ..ports.completion_intake import CompletionIntakeLedger
from ..ports.validated_work_preservation import ValidatedWorkAdmissionBackend


class RankedEvidenceAdmission:
    def __init__(self, store: ValidatedWorkAdmissionBackend, intake: CompletionIntakeLedger) -> None:
        self._store = store
        self._intake = intake

    def admit(self, admission: EvidenceAdmission) -> AdmissionOutcome:
        incoming = admission.evidence
        incoming_order = self._intake.evidence_receive_sequence(incoming)
        while True:
            batch = self._store.for_issue(incoming.identity.key.issue_number)
            current = next((row for row in batch.dispositions if row.record_id == incoming.record_id), None)
            current_id = None if current is None else current.evidence_id
            selection = EvidenceAdmissionSelection.CURRENT
            if current_id is not None and current_id != incoming.evidence_id:
                retained = self._store.evidence_for_id(current_id)
                if retained is None:
                    raise RuntimeError("current evidence disappeared during admission")
                retained_order = self._intake.evidence_receive_sequence(retained.evidence.admission.evidence)
                if incoming_order <= retained_order:
                    selection = EvidenceAdmissionSelection.RETAIN
            outcome = self._store.admit_selected(admission, current_id, selection)
            if outcome is not None:
                return outcome

    def for_issue(self, issue_number: int) -> ValidatedWorkDispositionBatch:
        return self._store.for_issue(issue_number)

    def has_unresolved_work(self, issue_number: int) -> bool:
        return self._store.has_unresolved_work(issue_number)

    def evidence_for_id(self, evidence_id: str) -> EvidenceLookup | None:
        return self._store.evidence_for_id(evidence_id)

    def retained_evidence(self, issue_number: int) -> tuple[EvidenceRow, ...]:
        return self._store.retained_evidence(issue_number)
