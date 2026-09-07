"""SQL for the shipped-fix operational memory (#6781 amendment).

An area-tagged record of every fix that actually MERGED, written at merge time
and read back (newest first, bounded) into the health review's board snapshot.
It is the "have we been here before?" half of pattern evidence: a case file says
a problem recurs, this says a fix already shipped on that same area.

Unlike the ledgers around it this grants nothing and gates nothing — it is
recall, not authority — so its SQL sits apart from the rows that constrain what
a session may do. Plain functions over a connection, like
``tech_lead_pending_intents``: the owning store keeps the connection, the write
lock, and the transaction boundary, and these must run inside them.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from ..domain.tech_lead_session import TechLeadShippedFixSummary
from ..ports.tech_lead_authority import TechLeadShippedFixConflictError


def insert(
    tx: sqlite3.Connection, *, issue_number: int, title: str, pr_url: str, area: str
) -> bool:
    """Record one merged fix create-once; False when it was already recorded.

    PR + area are the evidence identity. Titles are human metadata and may be
    edited between a durable write and a crash-retry, so a differing title
    reconciles to the recorded one; differing evidence raises
    :class:`TechLeadShippedFixConflictError`.
    """
    row = tx.execute(
        "SELECT pr_url, area FROM tech_lead_shipped_fixes WHERE issue_number = ?",
        (issue_number,),
    ).fetchone()
    if row is not None:
        if str(row["pr_url"]) == pr_url and str(row["area"]) == area:
            return False
        raise TechLeadShippedFixConflictError(
            "different shipped-fix evidence is already recorded for"
            f" issue #{issue_number}"
        )
    tx.execute(
        "INSERT INTO tech_lead_shipped_fixes "
        "(issue_number, title, pr_url, area, merged_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            issue_number,
            title,
            pr_url,
            area,
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    return True


def select_recent(
    conn: sqlite3.Connection, *, limit: int
) -> tuple[TechLeadShippedFixSummary, ...]:
    """The newest durable shipped-fix facts, bounded for the board snapshot."""
    if limit <= 0:
        raise ValueError("shipped-fix limit must be positive")
    rows = conn.execute(
        "SELECT issue_number, title, pr_url, area, merged_at "
        "FROM tech_lead_shipped_fixes "
        "ORDER BY merged_at DESC, issue_number DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return tuple(
        TechLeadShippedFixSummary(
            issue_number=int(row["issue_number"]),
            title=str(row["title"]),
            pr_url=str(row["pr_url"]),
            area=str(row["area"]),
            merged_at=str(row["merged_at"]),
        )
        for row in rows
    )
