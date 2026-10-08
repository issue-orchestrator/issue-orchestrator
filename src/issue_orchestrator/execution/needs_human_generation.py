"""SQL for the shared needs-human block's generations (#8688).

A generation is one episode of the block: opened when an acquisition puts the
label on afresh, ended with the label (see ``needs_human_generation`` in
:mod:`.pending_work_claim_schema`). Kept beside the store rather than in it, so
the store's transactions stay the one place that calls these.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from datetime import datetime, timezone


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def end_generation(conn: sqlite3.Connection, issue_number: int) -> None:
    """Drop a generation's cause rows, removal intent and onset together."""
    conn.execute("DELETE FROM needs_human_cause WHERE issue_number = ?", (issue_number,))
    conn.execute("DELETE FROM needs_human_removal_intent WHERE issue_number = ?", (issue_number,))
    conn.execute("DELETE FROM needs_human_generation WHERE issue_number = ?", (issue_number,))


def open_generation(conn: sqlite3.Connection, issue_number: int) -> None:
    """A new episode of the shared block, dated now. Call after :func:`end_generation`."""
    conn.execute(
        "INSERT INTO needs_human_generation (issue_number, opened_at) VALUES (?, ?)",
        (issue_number, _now()),
    )


def adopt_generations(conn: sqlite3.Connection, issue_numbers: Sequence[int]) -> None:
    """Open a generation dated now for each issue that has none; keep the rest."""
    conn.executemany(
        "INSERT OR IGNORE INTO needs_human_generation (issue_number, opened_at, adopted)"
        " VALUES (?, ?, 1)",
        [(number, _now()) for number in issue_numbers],
    )


def read_episodes(conn: sqlite3.Connection, issue_numbers: Sequence[int]) -> dict[int, str]:
    """``{issue: "<opened_at>#<episode>"}`` for each issue with a generation."""
    wanted = set(issue_numbers)
    return {
        int(row[0]): f"{row[1]}#{row[2]}"
        for row in conn.execute(
            "SELECT issue_number, opened_at, episode FROM needs_human_generation"
        )
        if int(row[0]) in wanted
    }
