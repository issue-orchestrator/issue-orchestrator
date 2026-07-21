"""Read-only e2e-health fact reader for the board-snapshot projection.

The triage tech lead's HEALTH REVIEW (ADR-0031) needs an AGGREGATE view of E2E
health — cadence, red streaks, chronic failures — on its primary input, the
board snapshot. This module reads those facts from the E2E results db
(``e2e_db.py`` / ``E2EDB``) over a STRICTLY READ-ONLY connection: it never runs
schema migrations and never opens a writable connection, so it is safe to point
at a db the E2E runner owns and writes concurrently, or at a foreign repo's db,
without perturbing it (``E2EDB(...)`` would run ``_ensure_schema`` — a write).

It projects nothing and decides nothing: it maps rows to typed domain facts and
hands them to :meth:`BoardE2EHealth.project`. Kept out of the 1600-line
``e2e_db.py`` hotspot deliberately (that file is a ratcheted line-budget
hotspot) and because "read-only projection feed" is a distinct concern.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from ..domain.board_snapshot import E2EChronicFailureFact, E2ERunHealthFact

# A nodeid is a chronic failure once it has failed in at least this many runs;
# a single failure is not yet "recurring".
CHRONIC_FAILURE_MIN_FAILS = 2

# Hard safety cap on chronic rows read from the db. The pure projection applies
# the smaller display cap and logs any truncation; this only bounds a
# pathological db (thousands of distinct chronic tests) from an unbounded read.
_CHRONIC_FETCH_CAP = 50

# Newest-first runs with per-run passed/failed tallies. ``failed_count`` counts
# both ``failed`` and ``error`` outcomes; the LEFT JOIN keeps runs that recorded
# no per-test rows (e.g. a collection ``error`` run) with zero counts.
_RECENT_RUN_HEALTH_SQL = """
    SELECT r.id AS id,
           r.status AS status,
           r.started_at AS started_at,
           r.duration_seconds AS duration_seconds,
           COALESCE(SUM(CASE WHEN t.outcome = 'passed' THEN 1 ELSE 0 END), 0) AS passed_count,
           COALESCE(SUM(CASE WHEN t.outcome IN ('failed', 'error') THEN 1 ELSE 0 END), 0) AS failed_count
    FROM e2e_runs r
    LEFT JOIN e2e_test_results t ON t.run_id = r.id
    GROUP BY r.id
    ORDER BY r.started_at DESC
    LIMIT ?
"""

# Recurring failing nodeids with their latest tracking issue (if any). The
# sub-select picks the most recent ``e2e_failure_issues`` row per nodeid
# (MAX(id)); it is 1:1 per nodeid so it does not inflate the fail count.
_CHRONIC_FAILURE_SQL = """
    SELECT t.nodeid AS nodeid,
           COUNT(*) AS fail_count,
           fi.github_issue_number AS tracking_issue,
           fi.resolved_at AS resolved_at
    FROM e2e_test_results t
    LEFT JOIN (
        SELECT nodeid, github_issue_number, resolved_at
        FROM e2e_failure_issues
        WHERE id IN (SELECT MAX(id) FROM e2e_failure_issues GROUP BY nodeid)
    ) fi ON fi.nodeid = t.nodeid
    WHERE t.outcome = 'failed'
    GROUP BY t.nodeid
    HAVING COUNT(*) >= ?
    ORDER BY fail_count DESC
    LIMIT ?
"""


def _connect_readonly(db_path: Path) -> sqlite3.Connection:
    """Open a strictly read-only connection.

    Uses a ``mode=ro`` URI and skips durability pragmas (``journal_mode=WAL``
    writes to the db header) so the read never mutates the target. Reads
    committed state, including of a WAL db another process is writing.
    """
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    return conn


def read_e2e_health_facts(
    db_path: Path,
    *,
    recent_run_limit: int,
    chronic_min_fails: int = CHRONIC_FAILURE_MIN_FAILS,
) -> tuple[list[E2ERunHealthFact], list[E2EChronicFailureFact]]:
    """Read newest-first run facts + chronic-failure facts over a read-only conn.

    Returns ``(runs, chronic_failures)``. Raises ``sqlite3.Error`` / ``OSError``
    on an unreadable or table-less db (e.g. an empty ``e2e.db``); the caller
    (the board-snapshot reader closure) owns the best-effort catch that maps
    those to a ``None`` e2e-health block.
    """
    conn = _connect_readonly(db_path)
    try:
        runs = [
            E2ERunHealthFact(
                id=row["id"],
                status=row["status"],
                started_at=row["started_at"],
                duration_seconds=row["duration_seconds"],
                passed_count=int(row["passed_count"]),
                failed_count=int(row["failed_count"]),
            )
            for row in conn.execute(_RECENT_RUN_HEALTH_SQL, (recent_run_limit,))
        ]
        chronic = [
            E2EChronicFailureFact(
                nodeid=row["nodeid"],
                fail_count=int(row["fail_count"]),
                tracking_issue=row["tracking_issue"],
                tracking_resolved=row["resolved_at"] is not None,
            )
            for row in conn.execute(
                _CHRONIC_FAILURE_SQL, (chronic_min_fails, _CHRONIC_FETCH_CAP)
            )
        ]
    finally:
        conn.close()
    return runs, chronic
