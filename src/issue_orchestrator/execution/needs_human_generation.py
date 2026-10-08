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

from ..ports.pending_work_claim_store import GenerationBinding


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


def bind_generation(
    conn: sqlite3.Connection,
    issue_number: int,
    *,
    event_id: int,
    applied_at: str,
    own_write: bool,
) -> GenerationBinding:
    """Bind the generation to GitHub's standing application of its label.

    No generation: one is opened from the event, dated by it (``ADOPTED``).
    One bound to this event stands (``CURRENT``), as does an unbound one the
    owner binds to its OWN write (``own_write``: the read right after the
    owner put the label on, under its gate). Any other binding ENDS the
    generation: one bound to a different event was removed and re-applied
    outside the owner, and an unbound one whose own write was never verified
    cannot be told apart from that (#8774 r2 F1), so it fails closed. The
    person who cleared the label ended every cause of that generation, so its
    cause rows and removal intent are retired with it, in this transaction,
    and a new generation is opened from the event (#8774).
    """
    row = conn.execute(
        "SELECT label_event_id FROM needs_human_generation WHERE issue_number = ?",
        (issue_number,),
    ).fetchone()
    if row is not None and (row[0] == event_id or (row[0] is None and own_write)):
        conn.execute(
            "UPDATE needs_human_generation SET label_event_id = ? WHERE issue_number = ?",
            (event_id, issue_number),
        )
        return GenerationBinding.CURRENT
    if row is not None:
        end_generation(conn, issue_number)  # re-applied by hand: every old cause ended
    conn.execute(
        "INSERT INTO needs_human_generation (issue_number, opened_at, label_event_id, adopted)"
        " VALUES (?, ?, ?, 1)",
        (issue_number, applied_at, event_id),
    )
    return GenerationBinding.ADOPTED if row is None else GenerationBinding.ENDED


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
