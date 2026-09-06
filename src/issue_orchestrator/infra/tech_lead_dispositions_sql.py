"""SQL for the failure-investigation disposition ledger (#6971).

One row per diagnosed-and-parked issue: the OPEN tracker that owns its remedy.
The stuck sweep reads it to skip issues whose diagnosis already exists, and the
disposition owner retains lapsed/recovered tombstones to reject stale commands.

The compare-and-transition statement binds every write to the exact prior JSON
row. An admitted command survives session cleanup; stale writers cannot replace
a lapsed binding or recovered tombstone. The lifecycle owns state transitions.

Plain functions over a connection, like ``tech_lead_pending_intents``: the
owning store keeps the connection, the write lock, and the transaction
boundary, and these must run inside them.
"""

from __future__ import annotations

import json
import sqlite3

from ..domain.tech_lead_session import TechLeadDisposition


def _from_row(row: sqlite3.Row) -> TechLeadDisposition:
    """Project one stored row onto its typed value.

    Malformed content raises loudly — the store is orchestrator-owned, so
    corruption is a bug, never agent input to fail-safe around.
    """
    return TechLeadDisposition.from_dict(json.loads(row["disposition"]))


def select(conn: sqlite3.Connection, issue_number: int) -> TechLeadDisposition | None:
    """One issue's disposition, or None when it is not parked."""
    row = conn.execute(
        "SELECT disposition FROM tech_lead_dispositions WHERE issue_number = ?",
        (issue_number,),
    ).fetchone()
    return None if row is None else _from_row(row)


def select_all(conn: sqlite3.Connection) -> tuple[TechLeadDisposition, ...]:
    """Every disposition — the sweep's ownership ledger read."""
    rows = conn.execute(
        "SELECT disposition FROM tech_lead_dispositions ORDER BY issue_number",
    ).fetchall()
    return tuple(_from_row(row) for row in rows)


def transition(
    tx: sqlite3.Connection,
    previous: TechLeadDisposition | None,
    disposition: TechLeadDisposition,
) -> bool:
    """CAS in the SQL predicate, safe across independent store connections."""
    encoded = json.dumps(disposition.to_dict(), sort_keys=True)
    values = (disposition.tracker_issue_number, encoded, disposition.recorded_at, disposition.issue_number)
    if previous is None:
        return tx.execute(
            "INSERT OR IGNORE INTO tech_lead_dispositions "
            "(tracker_issue_number, disposition, recorded_at, issue_number) VALUES (?, ?, ?, ?)",
            values,
        ).rowcount == 1
    row = tx.execute("SELECT disposition FROM tech_lead_dispositions WHERE issue_number = ?",
        (disposition.issue_number,)).fetchone()
    if row is None or _from_row(row) != previous:
        return False
    return tx.execute(
        "UPDATE tech_lead_dispositions SET tracker_issue_number = ?, disposition = ?, recorded_at = ? "
        "WHERE issue_number = ? AND disposition = ?", (*values, row["disposition"]),
    ).rowcount == 1
