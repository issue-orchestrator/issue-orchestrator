"""Admission-only composition using the same transactions, lineage and snapshots."""

from pathlib import Path
from contextlib import contextmanager
from collections.abc import Iterator
import sqlite3
from ..domain.completion_intake import CompletionIntakeError
from ..domain.validated_work import ValidatedWorkState
from ..domain.validated_work_commands import ValidatedWorkDispositionBatch
from ..domain.validated_work_store import AdmissionOutcome, EvidenceAdmission, EvidenceLookup, EvidenceRow, EvidenceAdmissionSelection
from ..ports.validated_work_verification import ValidatedWorkAncestry, ValidatedWorkArtifactVerifier
from .validated_work_admission import EvidenceAdmissionWriter
from .validated_work_lineage import LineageClassifier
from .validated_work_rows import DispositionDatabase, disposition
from .validated_work_snapshots import DispositionSnapshots


class SqliteValidatedWorkIntakeStore:
    def __init__(self, path: Path, ancestry: ValidatedWorkAncestry, artifacts: ValidatedWorkArtifactVerifier) -> None:
        self._db = DispositionDatabase(path)
        self._snapshots = DispositionSnapshots(self._db)
        self._admission = EvidenceAdmissionWriter(LineageClassifier(ancestry, artifacts))

    @contextmanager
    def _admission_write(self, admission: EvidenceAdmission) -> Iterator[sqlite3.Connection]:
        if admission.initial_state is not ValidatedWorkState.PARKED:
            raise ValueError("admission-only owner requires parked capture")
        try:
            with self._db.transaction(write=True) as conn:
                yield conn
        except sqlite3.Error as exc:
            raise CompletionIntakeError("parked admission unavailable") from exc

    def admit(self, admission: EvidenceAdmission) -> AdmissionOutcome:
        with self._admission_write(admission) as conn:
            status = self._admission.admit(conn, admission)
            return AdmissionOutcome(status, disposition(conn, admission.evidence.record_id))

    def admit_selected(self, admission: EvidenceAdmission, expected_current: str | None,
                       selection: EvidenceAdmissionSelection) -> AdmissionOutcome | None:
        with self._admission_write(admission) as conn:
            status = self._admission.admit_selected(conn, admission, expected_current, selection)
            return None if status is None else AdmissionOutcome(status, disposition(conn, admission.evidence.record_id))

    def for_issue(self, issue_number: int) -> ValidatedWorkDispositionBatch:
        return self._snapshots.for_issue(issue_number)

    def has_unresolved_work(self, issue_number: int) -> bool:
        return self._snapshots.has_unresolved_work(issue_number)

    def evidence_for_id(self, evidence_id: str) -> EvidenceLookup | None:
        return self._snapshots.evidence_for_id(evidence_id)

    def retained_evidence(self, issue_number: int) -> tuple[EvidenceRow, ...]:
        return self._snapshots.retained_evidence(issue_number)
