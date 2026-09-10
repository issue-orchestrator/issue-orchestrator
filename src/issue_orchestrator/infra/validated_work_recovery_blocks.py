"""Atomic aggregate observations and exact-generation cleanup acknowledgements."""

import sqlite3

from ..domain.recovery_block import (
    RecoveryBlockInterest,
    RecoveryBlockSnapshot,
    RecoveryCleanupKey,
)
from ..domain.validated_work import FinalizationPhase, ValidatedWorkState
from .validated_work_rows import (
    DispositionDatabase,
    current_evidence,
    disposition,
    has_successful_attempt,
    latest_attempt,
    record_row,
)


class RecoveryBlockPersistence:
    def __init__(self, database: DispositionDatabase) -> None:
        self._db = database

    def snapshot(self, repo_slug: str, issue_number: int) -> RecoveryBlockSnapshot:
        with self._db.transaction() as conn:
            rows = conn.execute(
                "SELECT DISTINCT r.record_id FROM validated_work_records r "
                "JOIN validated_work_evidence e ON e.record_id=r.record_id "
                "WHERE r.issue_number=? AND e.released_at='' ORDER BY r.record_id",
                (issue_number,),
            ).fetchall()
            return RecoveryBlockSnapshot(
                repo_slug,
                issue_number,
                tuple(self._interest(conn, row[0]) for row in rows),
            )

    def begin_label_cleanup(
        self, keys: tuple[RecoveryCleanupKey, ...], label: str
    ) -> bool:
        if not keys or not label.strip():
            raise ValueError("cleanup intent requires evidence and a label")
        with self._db.transaction(write=True) as conn:
            if not all(self._eligible(conn, key) for key in keys):
                raise ValueError("cleanup intent refused changed or ineligible records")
            replay = any(
                conn.execute(
                    "SELECT 1 FROM validated_work_block_cleanup_intent "
                    "WHERE record_id=? AND evidence_id=? AND attempt_no=? AND label=?",
                    (key.record_id, key.evidence_id, key.attempt_no, label),
                ).fetchone() is not None
                for key in keys
            )
            conn.executemany(
                "INSERT OR IGNORE INTO validated_work_block_cleanup_intent "
                "(record_id, evidence_id, attempt_no, label) VALUES (?, ?, ?, ?)",
                [(key.record_id, key.evidence_id, key.attempt_no, label) for key in keys],
            )
            return not replay

    def _eligible(self, conn: sqlite3.Connection, key: RecoveryCleanupKey) -> bool:
        item = self._interest(conn, key.record_id)
        routed = (
            item.disposition.state is ValidatedWorkState.PUBLISHING
            and item.phase is FinalizationPhase.REVIEW_ROUTED
            and item.disposition.pr_number is not None
            and has_successful_attempt(conn, key.record_id)
        )
        return item.cleanup_key == key and (item.permits_captured_cleanup or routed)

    def acknowledge(self, keys: tuple[RecoveryCleanupKey, ...]) -> bool:
        with self._db.transaction(write=True) as conn:
            if not all(self._eligible(conn, key) for key in keys):
                return False
            conn.executemany(
                "INSERT OR IGNORE INTO validated_work_block_cleanup(record_id, evidence_id, attempt_no) VALUES (?, ?, ?)",
                [(key.record_id, key.evidence_id, key.attempt_no) for key in keys],
            )
            return True

    def _interest(
        self, conn: sqlite3.Connection, record_id: str
    ) -> RecoveryBlockInterest:
        row = record_row(conn, record_id)
        current = current_evidence(conn, record_id)
        attempt = latest_attempt(conn, record_id)
        key = RecoveryCleanupKey(
            record_id, current.evidence_id, 0 if attempt is None else attempt.attempt_no
        )
        recorded = conn.execute(
            "SELECT 1 FROM validated_work_block_cleanup WHERE record_id=? AND evidence_id=? AND attempt_no=?",
            (key.record_id, key.evidence_id, key.attempt_no),
        ).fetchone()
        item = RecoveryBlockInterest(
            disposition(conn, record_id),
            FinalizationPhase(row["finalization_phase"]),
            current.admission.evidence.observations.observed_blocking_labels,
            key,
            recorded is not None,
        )
        if (
            item.disposition.state is ValidatedWorkState.PUBLISHING
            and not item.holds_recovery
        ):
            if (
                not has_successful_attempt(conn, record_id)
                or item.disposition.pr_number is None
            ):
                raise ValueError(
                    "released publishing interest lacks durable publication"
                )
        return item
