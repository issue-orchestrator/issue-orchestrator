"""SQLite adapter for :class:`~..ports.approval_evidence.OperatorApprovalRecords` (#7763).

Lives in the tech-lead authority database beside the proposal-op ledger the
approvals unlock, and shares that store's connection, write lock and
transaction boundary, like the charter ledger.
"""

from __future__ import annotations

import sqlite3
from contextlib import AbstractContextManager
from typing import Callable

from ..domain.tech_lead_approval import OperatorApprovalRecord


class SqliteOperatorApprovalRecords:
    """One row per proposal issue: the label event an operator's approval made."""

    def __init__(
        self,
        *,
        connection: Callable[[], sqlite3.Connection],
        transaction: Callable[[], AbstractContextManager[sqlite3.Connection]],
    ) -> None:
        self._connection = connection
        self._transaction = transaction

    def record_operator_approval(self, record: OperatorApprovalRecord) -> None:
        with self._transaction() as tx:
            tx.execute(
                "INSERT INTO tech_lead_operator_approvals"
                " (issue_number, label_event_id, recorded_at) VALUES (?, ?, ?)"
                " ON CONFLICT(issue_number) DO UPDATE SET"
                " label_event_id = excluded.label_event_id,"
                " recorded_at = excluded.recorded_at",
                (record.issue_number, record.label_event_id, record.recorded_at),
            )

    def load_operator_approval(self, issue_number: int) -> OperatorApprovalRecord | None:
        row = self._connection().execute(
            "SELECT issue_number, label_event_id, recorded_at"
            " FROM tech_lead_operator_approvals WHERE issue_number = ?",
            (issue_number,),
        ).fetchone()
        if row is None:
            return None
        return OperatorApprovalRecord(
            issue_number=int(row["issue_number"]),
            label_event_id=int(row["label_event_id"]),
            recorded_at=str(row["recorded_at"]),
        )

    def discard_operator_approval(self, issue_number: int) -> None:
        with self._transaction() as tx:
            tx.execute(
                "DELETE FROM tech_lead_operator_approvals WHERE issue_number = ?",
                (issue_number,),
            )
