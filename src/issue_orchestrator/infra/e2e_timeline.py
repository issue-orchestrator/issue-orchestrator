"""E2E timeline helpers for reading orchestrator events from worktree databases."""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)


def read_orchestrator_events_by_window(
    timeline_db_path: Path,
    started_at: str,
    finished_at: str | None,
) -> list[dict]:
    """Read orchestrator events from a timeline DB by time window.

    Used for reading agent events from the E2E worktree's timeline.
    The E2E worktree is isolated, so time-window filtering is sufficient
    (no instance_id needed). Only returns issue-keyed events (issue_number > 0).
    """
    try:
        uri = f"file:{timeline_db_path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        conn.row_factory = sqlite3.Row

        end_ts = finished_at or "9999-12-31T23:59:59Z"

        rows = conn.execute(
            """
            SELECT event_id, source_event, timestamp, event, data_json
            FROM timeline_events
            WHERE issue_number > 0
              AND timestamp >= ? AND timestamp <= ?
            ORDER BY sequence ASC
            """,
            (started_at, end_ts),
        ).fetchall()

        conn.close()
    except Exception:
        logger.debug("Could not read timeline from %s", timeline_db_path, exc_info=True)
        return []

    from ..ports.timeline_store import TimelineRecord
    from ..timeline import TimelineStream

    records = []
    for row in rows:
        data_json = row["data_json"] or "{}"
        try:
            data = json.loads(data_json)
        except (ValueError, TypeError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        records.append(
            TimelineRecord(
                event_id=str(row["event_id"]),
                timestamp=str(row["timestamp"]),
                event=str(row["event"]),
                data=data,
                source_event=str(row["source_event"] or ""),
            )
        )

    if not records:
        return []

    stream = TimelineStream.from_records(issue_number=0, records=records)
    return [evt.to_dict() for evt in stream.events]
