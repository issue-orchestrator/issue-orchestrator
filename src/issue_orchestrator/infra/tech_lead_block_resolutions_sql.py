"""SQL for the block-resolution discharge ledger (#7658).

One row per ``resolve_block`` decision: the needs-human causes it discharges
and whether that discharge is only BEGUN (write-ahead, before the label write)
or COMMITTED. ``resolved_causes`` is what keeps a block that was put back after
a resolve from being cleared a second time. Plain functions over a connection,
like ``tech_lead_shipped_fixes_sql``: the owning store keeps the connection,
the write lock, and the transaction boundary, and these must run inside them.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from ..ports.operator_decision_retries import DecisionRetryState


def begin(tx: sqlite3.Connection, *, decision_id: str, issue_number: int, causes: frozenset[str]) -> None:
    tx.execute(
        "INSERT OR REPLACE INTO tech_lead_block_resolutions"
        " (decision_id, issue_number, causes, state, recorded_at) VALUES (?, ?, ?, ?, ?)",
        (decision_id, issue_number, json.dumps(sorted(causes)), DecisionRetryState.BEGUN.value,
         datetime.now(timezone.utc).isoformat()),
    )


def commit(tx: sqlite3.Connection, *, decision_id: str) -> None:
    updated = tx.execute(
        "UPDATE tech_lead_block_resolutions SET state = ?, recorded_at = ? WHERE decision_id = ?",
        (DecisionRetryState.COMMITTED.value, datetime.now(timezone.utc).isoformat(), decision_id),
    ).rowcount
    if updated != 1:
        raise ValueError(f"no begun discharge for {decision_id} to commit")


def abandon(tx: sqlite3.Connection, *, decision_id: str) -> None:
    tx.execute("DELETE FROM tech_lead_block_resolutions WHERE decision_id = ?", (decision_id,))


def state(conn: sqlite3.Connection, *, decision_id: str) -> DecisionRetryState | None:
    row = conn.execute(
        "SELECT state FROM tech_lead_block_resolutions WHERE decision_id = ?", (decision_id,),
    ).fetchone()
    return None if row is None else DecisionRetryState(str(row[0]))


def resolved_causes(conn: sqlite3.Connection, *, issue_number: int) -> dict[str, frozenset[str]]:
    rows = conn.execute(
        "SELECT decision_id, causes FROM tech_lead_block_resolutions WHERE issue_number = ?",
        (issue_number,),
    ).fetchall()
    found: dict[str, set[str]] = {}
    for row in rows:
        for cause in json.loads(row["causes"]):
            found.setdefault(str(cause), set()).add(str(row["decision_id"]))
    return {cause: frozenset(ids) for cause, ids in found.items()}
