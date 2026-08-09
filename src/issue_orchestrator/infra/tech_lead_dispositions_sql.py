"""SQL for the failure-investigation disposition ledger (#6971).

One row per diagnosed-and-parked issue: the OPEN tracker that owns its remedy.
The stuck sweep reads it to skip issues whose diagnosis already exists, and the
disposition owner deletes a row the moment its binding lapses.

Its write rule is the opposite of every other ledger here, which is why it gets
its own file rather than joining them: the durable ledgers in
``tech_lead_authority_store`` are create-once (a consent binding, an identity
map — recording a different payload is a conflict). This row means "the latest
completed investigation's conclusion", so a newer investigation must SUPERSEDE
it; refusing the update would freeze an issue on a tracker that may already be
closed, which is exactly the parked-forever state the release rule exists to
prevent.

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


def upsert(tx: sqlite3.Connection, disposition: TechLeadDisposition) -> None:
    """Record an issue's disposition, superseding any previous binding."""
    tx.execute(
        "INSERT INTO tech_lead_dispositions"
        " (issue_number, tracker_issue_number, disposition, recorded_at)"
        " VALUES (?, ?, ?, ?)"
        " ON CONFLICT(issue_number) DO UPDATE SET"
        " tracker_issue_number = excluded.tracker_issue_number,"
        " disposition = excluded.disposition,"
        " recorded_at = excluded.recorded_at",
        (
            disposition.issue_number,
            disposition.tracker_issue_number,
            json.dumps(disposition.to_dict(), sort_keys=True),
            disposition.recorded_at,
        ),
    )


def select(conn: sqlite3.Connection, issue_number: int) -> TechLeadDisposition | None:
    """One issue's disposition, or None when it is not parked."""
    row = conn.execute(
        "SELECT disposition FROM tech_lead_dispositions WHERE issue_number = ?",
        (issue_number,),
    ).fetchone()
    return None if row is None else _from_row(row)


def delete(tx: sqlite3.Connection, issue_number: int) -> int:
    """Release an issue's disposition; returns how many rows were removed."""
    return tx.execute(
        "DELETE FROM tech_lead_dispositions WHERE issue_number = ?",
        (issue_number,),
    ).rowcount


def select_all(conn: sqlite3.Connection) -> tuple[TechLeadDisposition, ...]:
    """Every disposition — the sweep's ownership ledger read."""
    rows = conn.execute(
        "SELECT disposition FROM tech_lead_dispositions ORDER BY issue_number",
    ).fetchall()
    return tuple(_from_row(row) for row in rows)
