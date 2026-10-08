"""SQL for the shared needs-human block's generations (#8688).

A generation is one episode of the block: opened when an acquisition puts the
label on afresh, ended with the label (see ``needs_human_generation`` in
:mod:`.pending_work_claim_schema`). Kept beside the store rather than in it, so
the store's transactions stay the one place that calls these.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone

#: How long after the owner opens a generation its own label write may land
#: (#8774 r1 F1). The write follows the open at once; a standing application
#: dated later than this is a person's re-application, not the owner's write.
OWN_WRITE_WINDOW = timedelta(minutes=5)


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
    conn: sqlite3.Connection, issue_number: int, *, event_id: int, applied_at: str
) -> None:
    """Bind the generation to GitHub's standing application of its label.

    No generation: one is opened from the event, dated by it. An unbound one
    is bound. One bound to a DIFFERENT event is stale: the label was removed
    and re-applied outside the owner. The person who cleared it ended every
    cause of that generation, so its cause rows and removal intent are retired
    with it, in this transaction, and a new generation is opened (#8774). A
    release of a retired cause then finds no row and leaves the person's new
    block alone. An unbound generation the owner opened is bound only to an
    application dated within :data:`OWN_WRITE_WINDOW` of its opening: its own
    write, whose binding could not be read at the time. A later one is a
    re-application, and retires it the same way.
    """
    row = conn.execute(
        "SELECT label_event_id, opened_at, adopted FROM needs_human_generation"
        " WHERE issue_number = ?",
        (issue_number,),
    ).fetchone()
    if row is not None and row[0] == event_id:
        return
    if row is not None and row[0] is None and _own_write(row[1], bool(row[2]), applied_at):
        conn.execute(
            "UPDATE needs_human_generation SET label_event_id = ? WHERE issue_number = ?",
            (event_id, issue_number),
        )
        return
    if row is not None:
        end_generation(conn, issue_number)  # re-applied by hand: every old cause ended
    conn.execute(
        "INSERT INTO needs_human_generation (issue_number, opened_at, label_event_id, adopted)"
        " VALUES (?, ?, ?, 1)",
        (issue_number, applied_at, event_id),
    )


def _own_write(opened_at: str, adopted: bool, applied_at: str) -> bool:
    """Whether the application dated ``applied_at`` can be the write that put
    the label on for a generation opened at ``opened_at``. An adopted
    generation was opened from an application, so any binds it."""
    if adopted:
        return True
    opened = datetime.fromisoformat(opened_at)
    applied = datetime.fromisoformat(applied_at)
    return applied <= opened + OWN_WRITE_WINDOW


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
