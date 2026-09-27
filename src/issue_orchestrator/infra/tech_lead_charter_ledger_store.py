"""SQLite adapter for the ``TechLeadCharterLedger`` port (#7330).

The charter decision ledger lives in the tech-lead authority database, next to
the proposal-op ledger whose approval and decline it links back to, and shares
that store's connection, write lock and transaction boundary (the store is the
composition-root owner of the file). The full record is stored as JSON; the
columns a reader filters on are denormalized beside it.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import AbstractContextManager
from typing import Callable, Iterable

from ..domain.tech_lead_charter import CharterRole
from ..domain.tech_lead_charter_decisions import (
    CharterProposalLifecycle,
    TechLeadCharterDecision,
)
from ..ports.tech_lead_charter_ledger import (
    check_read_limit,
    keep_linked_lifecycle,
    links_to_proposal,
)

_UPSERT = (
    "INSERT INTO tech_lead_charter_decisions (decision_id, run_id, action_id,"
    " anchor_issue_number, target_number, proposal_issue_number, role,"
    " decided_at, record) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
    " ON CONFLICT(decision_id) DO UPDATE SET run_id = excluded.run_id,"
    " action_id = excluded.action_id,"
    " anchor_issue_number = excluded.anchor_issue_number,"
    " target_number = excluded.target_number,"
    " proposal_issue_number = excluded.proposal_issue_number,"
    " role = excluded.role, decided_at = excluded.decided_at,"
    " record = excluded.record"
)


def _row_values(decision: TechLeadCharterDecision) -> tuple[object, ...]:
    return (
        decision.decision_id,
        decision.run_id,
        decision.action_id,
        decision.anchor_issue_number,
        decision.target_number,
        decision.proposal_issue_number,
        decision.role.value,
        decision.decided_at,
        json.dumps(decision.to_dict(), sort_keys=True),
    )


def _decode(rows: Iterable[sqlite3.Row]) -> tuple[TechLeadCharterDecision, ...]:
    return tuple(TechLeadCharterDecision.from_dict(json.loads(row["record"])) for row in rows)


class SqliteTechLeadCharterLedger:
    """Durable charter decisions over the authority store's connection."""

    def __init__(
        self,
        *,
        connection: Callable[[], sqlite3.Connection],
        transaction: Callable[[], AbstractContextManager[sqlite3.Connection]],
    ) -> None:
        self._connection = connection
        self._transaction = transaction

    def record_decisions(self, decisions: Iterable[TechLeadCharterDecision]) -> None:
        with self._transaction() as tx:
            for decision in decisions:
                row = tx.execute(
                    "SELECT record FROM tech_lead_charter_decisions WHERE decision_id = ?",
                    (decision.decision_id,),
                ).fetchone()
                existing = (
                    TechLeadCharterDecision.from_dict(json.loads(row["record"]))
                    if row is not None
                    else None
                )
                tx.execute(_UPSERT, _row_values(keep_linked_lifecycle(existing, decision)))

    def link_proposal_outcome(
        self,
        *,
        run_id: str,
        action_id: str,
        proposal_issue_number: int,
        lifecycle: CharterProposalLifecycle,
        at: str,
    ) -> int:
        with self._transaction() as tx:
            candidates = _decode(
                tx.execute(
                    "SELECT record FROM tech_lead_charter_decisions WHERE"
                    " (run_id = ? AND action_id = ?) OR proposal_issue_number = ?",
                    (run_id, action_id, proposal_issue_number),
                )
            )
            updated = 0
            for row in candidates:
                if not links_to_proposal(
                    row,
                    run_id=run_id,
                    action_id=action_id,
                    proposal_issue_number=proposal_issue_number,
                ):
                    continue
                linked = row.with_lifecycle(
                    lifecycle, at=at, proposal_issue_number=proposal_issue_number
                )
                tx.execute(_UPSERT, _row_values(linked))
                updated += 1
            return updated

    def list_for_issue(
        self, issue_number: int, *, limit: int = 100
    ) -> tuple[TechLeadCharterDecision, ...]:
        return _decode(
            self._connection().execute(
                "SELECT record FROM tech_lead_charter_decisions WHERE target_number = ?"
                " OR anchor_issue_number = ? OR proposal_issue_number = ?"
                " ORDER BY decided_at DESC, decision_id DESC LIMIT ?",
                (issue_number, issue_number, issue_number, check_read_limit(limit)),
            )
        )

    def list_for_role(
        self, role: CharterRole, *, limit: int = 100
    ) -> tuple[TechLeadCharterDecision, ...]:
        return _decode(
            self._connection().execute(
                "SELECT record FROM tech_lead_charter_decisions WHERE role = ?"
                " ORDER BY decided_at DESC, decision_id DESC LIMIT ?",
                (role.value, check_read_limit(limit)),
            )
        )

    def list_recent(self, *, limit: int = 100) -> tuple[TechLeadCharterDecision, ...]:
        return _decode(
            self._connection().execute(
                "SELECT record FROM tech_lead_charter_decisions"
                " ORDER BY decided_at DESC, decision_id DESC LIMIT ?",
                (check_read_limit(limit),),
            )
        )
