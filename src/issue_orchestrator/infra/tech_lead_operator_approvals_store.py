"""SQLite adapters for the approval owner's records (#7763).

:class:`~..ports.approval_evidence.OperatorApprovalRecords` and
:class:`~..ports.approval_evidence.ProposalIssueIndex`.

Lives in the tech-lead authority database beside the proposal-op ledger the
approvals unlock, and shares that store's connection, write lock and
transaction boundary, like the charter ledger.
"""

from __future__ import annotations

import sqlite3
from contextlib import AbstractContextManager
from collections.abc import Iterable
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


class SqliteProposalIssueIndex:
    """One row per proposal issue the approval scope must keep finding."""

    def __init__(
        self,
        *,
        connection: Callable[[], sqlite3.Connection],
        transaction: Callable[[], AbstractContextManager[sqlite3.Connection]],
    ) -> None:
        self._connection = connection
        self._transaction = transaction

    def index_proposals(self, numbers: Iterable[int]) -> None:
        rows = [(int(number),) for number in numbers]
        if not rows:
            return
        with self._transaction() as tx:
            tx.executemany(
                "INSERT INTO tech_lead_proposal_index (issue_number) VALUES (?)"
                " ON CONFLICT(issue_number) DO UPDATE SET active = 1",
                rows,
            )

    def indexed_proposals(self) -> frozenset[int]:
        return self._numbers("SELECT issue_number FROM tech_lead_proposal_index WHERE declined = 0 AND active = 1")

    def known_proposals(self) -> frozenset[int]:
        return self._numbers("SELECT issue_number FROM tech_lead_proposal_index")

    def declined_proposals(self) -> frozenset[int]:
        return self._numbers("SELECT issue_number FROM tech_lead_proposal_index WHERE declined = 1")

    def is_declined(self, number: int) -> bool:
        row = self._connection().execute(
            "SELECT 1 FROM tech_lead_proposal_index WHERE issue_number = ? AND declined = 1",
            (int(number),),
        ).fetchone()
        return row is not None

    def _numbers(self, query: str) -> frozenset[int]:
        rows = self._connection().execute(query).fetchall()
        return frozenset(int(row["issue_number"]) for row in rows)

    def decline_proposals(self, numbers: Iterable[int]) -> None:
        rows = [(int(number),) for number in numbers]
        if not rows:
            return
        with self._transaction() as tx:
            tx.executemany(
                "INSERT INTO tech_lead_proposal_index (issue_number, declined) VALUES (?, 1)"
                " ON CONFLICT(issue_number) DO UPDATE SET declined = 1",
                rows,
            )

    def retire_proposals(self, numbers: Iterable[int]) -> None:
        rows = [(int(number),) for number in numbers]
        if not rows:
            return
        with self._transaction() as tx:
            tx.executemany(
                "UPDATE tech_lead_proposal_index SET active = 0 WHERE issue_number = ?",
                rows,
            )
