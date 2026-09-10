"""Receipt SQL within the authority store's existing connection and transaction.

These helpers never open a second database or commit independently. The owning
store serializes writes, including the immutable approved-instruction check.
"""

from __future__ import annotations

import json
import sqlite3

from ..domain.scoped_rework import ReworkReceipt
from ..ports.tech_lead_authority import TechLeadOpConflictError


def load_receipt(connection: sqlite3.Connection, key: str) -> ReworkReceipt | None:
    row = connection.execute(
        "SELECT receipt FROM tech_lead_rework_receipts WHERE request_key = ?", (key,)
    ).fetchone()
    return ReworkReceipt.from_dict(json.loads(row[0])) if row is not None else None


def save_receipt(transaction: sqlite3.Connection, receipt: ReworkReceipt) -> None:
    previous = load_receipt(transaction, receipt.request.key)
    if previous is not None and previous.request != receipt.request:
        raise TechLeadOpConflictError("Cannot replace approved rework instruction")
    transaction.execute(
        "INSERT INTO tech_lead_rework_receipts (request_key, receipt) VALUES (?, ?) "
        "ON CONFLICT(request_key) DO UPDATE SET receipt = excluded.receipt",
        (receipt.request.key, json.dumps(receipt.to_dict(), sort_keys=True)),
    )


def list_receipts(connection: sqlite3.Connection) -> tuple[ReworkReceipt, ...]:
    rows = connection.execute(
        "SELECT receipt FROM tech_lead_rework_receipts ORDER BY request_key"
    ).fetchall()
    return tuple(ReworkReceipt.from_dict(json.loads(row[0])) for row in rows)
