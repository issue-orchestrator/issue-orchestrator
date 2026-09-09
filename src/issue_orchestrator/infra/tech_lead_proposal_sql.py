"""Proposal SQL under the authority store's connection and transaction owner."""

from __future__ import annotations
import json
import sqlite3
from datetime import datetime, timezone
from ..domain.tech_lead_proposal_creation import PendingTechLeadProposal
from ..domain.tech_lead_session import StoredTechLeadOp
from ..ports.tech_lead_authority import TechLeadOpConflictError


def record_op(tx: sqlite3.Connection, number: int, op: StoredTechLeadOp) -> None:
    row = tx.execute(
        "SELECT op FROM tech_lead_proposal_ops WHERE issue_number = ?", (number,)
    ).fetchone()
    if row is not None:
        if StoredTechLeadOp.from_dict(json.loads(row[0])) == op:
            return
        raise TechLeadOpConflictError(
            f"a different tech_lead op is already recorded for proposal issue #{number}"
        )
    tx.execute(
        "INSERT INTO tech_lead_proposal_ops (issue_number, op, recorded_at) VALUES (?, ?, ?)",
        (
            number,
            json.dumps(op.to_dict(), sort_keys=True),
            datetime.now(timezone.utc).isoformat(),
        ),
    )


def load_pending(tx: sqlite3.Connection, key: str) -> PendingTechLeadProposal | None:
    row = tx.execute(
        "SELECT intent FROM tech_lead_pending_proposals WHERE creation_key = ?", (key,)
    ).fetchone()
    return (
        PendingTechLeadProposal.from_dict(json.loads(row[0]))
        if row is not None
        else None
    )


def record_pending(tx: sqlite3.Connection, intent: PendingTechLeadProposal) -> None:
    previous = load_pending(tx, intent.key)
    if previous is not None:
        if previous != intent:
            raise TechLeadOpConflictError("Cannot replace a pending proposal creation")
        return
    tx.execute(
        "INSERT INTO tech_lead_pending_proposals (creation_key, intent) VALUES (?, ?)",
        (intent.key, json.dumps(intent.to_dict(), sort_keys=True)),
    )


def list_pending(tx: sqlite3.Connection) -> tuple[PendingTechLeadProposal, ...]:
    return tuple(PendingTechLeadProposal.from_dict(json.loads(row[0])) for row in
                 tx.execute("SELECT intent FROM tech_lead_pending_proposals ORDER BY creation_key"))
