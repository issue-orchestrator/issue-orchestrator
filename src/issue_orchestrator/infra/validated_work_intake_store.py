"""Admission-only composition of the existing disposition transaction owners.

Intake cannot acquire publication claims and therefore does not manufacture a
liveness implementation. The full store and this adapter share schema, writer,
lineage policy and transaction implementation.
"""

from pathlib import Path
import sqlite3

from ..domain.validated_work import ValidatedWorkState
from ..domain.completion_intake import CompletionIntakeError
from ..domain.validated_work_store import AdmissionOutcome, EvidenceAdmission
from ..ports.validated_work_verification import (
    ValidatedWorkAncestry,
    ValidatedWorkArtifactVerifier,
)
from .validated_work_admission import EvidenceAdmissionWriter
from .validated_work_lineage import LineageClassifier
from .validated_work_rows import DispositionDatabase, disposition


class SqliteValidatedWorkIntakeStore:
    def __init__(
        self,
        path: Path,
        ancestry: ValidatedWorkAncestry,
        artifacts: ValidatedWorkArtifactVerifier,
    ) -> None:
        self._db = DispositionDatabase(path)
        self._admission = EvidenceAdmissionWriter(
            LineageClassifier(ancestry, artifacts)
        )

    def admit(self, admission: EvidenceAdmission) -> AdmissionOutcome:
        try:
            return self._admit_parked(admission)
        except sqlite3.Error as exc:
            raise CompletionIntakeError(
                "historical parked admission unavailable"
            ) from exc

    def _admit_parked(self, admission: EvidenceAdmission) -> AdmissionOutcome:
        if admission.initial_state is not ValidatedWorkState.PARKED:
            raise CompletionIntakeError("historical intake may only admit parked work")
        with self._db.transaction(write=True) as conn:
            current = conn.execute(
                "SELECT state FROM validated_work_records WHERE record_id=?",
                (admission.evidence.record_id,),
            ).fetchone()
            if current is not None and current[0] in {"publishing", "recovered"}:
                raise CompletionIntakeError(
                    "historical intake cannot retarget active or resolved publication"
                )
            status = self._admission.admit(conn, admission)
            result = disposition(conn, admission.evidence.record_id)
            if result.state is not ValidatedWorkState.PARKED:
                raise CompletionIntakeError(
                    "historical admission did not establish parked state"
                )
            return AdmissionOutcome(status, result)
