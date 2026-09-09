"""One typed snapshot reader for full and admission-only store compositions."""

from ..domain.recovery_entry import RecoveryRecordRequest
from ..domain.validated_work import require_positive
from ..domain.validated_work_commands import ValidatedWorkDispositionBatch
from ..domain.validated_work_store import EvidenceLookup, EvidenceRow
from .validated_work_rows import DispositionDatabase, disposition, evidence_row


class DispositionSnapshots:
    def __init__(self, database: DispositionDatabase) -> None:
        self._db = database

    def for_issue(self, issue_number: int) -> ValidatedWorkDispositionBatch:
        with self._db.transaction() as conn:
            ids = conn.execute(
                "SELECT record_id FROM validated_work_records WHERE issue_number=? ORDER BY record_id",
                (issue_number,),
            ).fetchall()
            return ValidatedWorkDispositionBatch(
                issue_number,
                tuple(disposition(conn, r[0]) for r in ids),
                "durable dispositions",
            )

    def has_unresolved_work(self, issue_number: int) -> bool:
        # Materialize the typed batch before answering: an invalid excluded row
        # must never turn the teardown/reset safety probe into "no work".
        return self.for_issue(issue_number).unresolved

    def evidence_for_id(self, evidence_id: str) -> EvidenceLookup | None:
        with self._db.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM validated_work_evidence WHERE evidence_id=?",
                (evidence_id,),
            ).fetchone()
            return (
                None
                if row is None
                else EvidenceLookup(
                    evidence_row(row), disposition(conn, row["record_id"])
                )
            )


    def retained_evidence(self, issue_number: int) -> tuple[EvidenceRow, ...]:
        with self._db.transaction() as conn:
            return tuple(evidence_row(row) for row in conn.execute(
                "SELECT e.* FROM validated_work_evidence e JOIN validated_work_records r USING(record_id) "
                "WHERE r.issue_number=? AND e.released_at='' ORDER BY e.evidence_id", (issue_number,)))

    def recovery_requests(self, *, after_record_id: str, limit: int) -> tuple[RecoveryRecordRequest, ...]:
        require_positive(limit, "recovery batch size")
        with self._db.transaction() as conn:
            rows = conn.execute(
                "SELECT r.record_id, e.evidence_id FROM validated_work_records r "
                "JOIN validated_work_evidence e ON e.record_id=r.record_id "
                "WHERE r.record_id>? AND e.role='current' AND e.released_at='' "
                "AND ((r.state='queued' AND r.lineage_role='head') OR r.state='publishing') "
                "ORDER BY r.record_id LIMIT ?", (after_record_id, limit),
            ).fetchall()
            return tuple(RecoveryRecordRequest(row["record_id"], row["evidence_id"]) for row in rows)
