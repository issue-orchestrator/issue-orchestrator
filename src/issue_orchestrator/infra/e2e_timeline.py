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
            SELECT issue_number, event_id, source_event, timestamp, event, data_json
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

    from collections import defaultdict

    from ..ports.timeline_store import TimelineRecord
    from ..timeline import TimelineStream

    # Group records by issue_number so each event keeps its real identity.
    # Passing a single placeholder issue_number to TimelineStream.from_records
    # would lose per-issue identity and break downstream window matching.
    by_issue: dict[int, list[TimelineRecord]] = defaultdict(list)
    for row in rows:
        data_json = row["data_json"] or "{}"
        try:
            data = json.loads(data_json)
        except (ValueError, TypeError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        issue_num = int(row["issue_number"])
        by_issue[issue_num].append(
            TimelineRecord(
                event_id=str(row["event_id"]),
                timestamp=str(row["timestamp"]),
                event=str(row["event"]),
                data=data,
                source_event=str(row["source_event"] or ""),
            )
        )

    if not by_issue:
        return []

    all_events: list[dict] = []
    for issue_num, records in by_issue.items():
        stream = TimelineStream.from_records(issue_num, records)
        all_events.extend(evt.to_dict() for evt in stream.events)

    # Restore chronological order across issues
    all_events.sort(key=lambda e: e.get("timestamp", ""))
    return all_events
