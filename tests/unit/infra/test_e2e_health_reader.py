"""Read-only e2e-health fact reader tests (seeded temp E2EDB).

Seeds a real ``E2EDB`` with runs / per-test results / failure issues, then reads
the aggregate health facts over the strictly read-only reader and (end to end)
projects them into ``BoardE2EHealth``. Also proves the read never mutates the
db and that a missing/table-less db raises for the caller to swallow.
"""

import hashlib
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from issue_orchestrator.domain.board_snapshot import BoardE2EHealth
from issue_orchestrator.infra.e2e_db import E2EDB
from issue_orchestrator.infra.e2e_health_reader import read_e2e_health_facts

# 18h after the newest seeded run (2026-07-18T00:00) -> clearly past 3x240min.
NOW = datetime(2026, 7, 18, 18, 0, 0, tzinfo=timezone.utc)


def _seed_run(
    db: E2EDB,
    *,
    started_at: str,
    status: str,
    outcomes: list[tuple[str, str]],
    orchestrator_id: str = "orch",
) -> int:
    """Create a finished run with the given per-test outcomes and started_at.

    ``started_at`` is forced via a direct UPDATE so recency ordering is
    deterministic (``start_run`` stamps wall-clock time internally).
    """
    run_id = db.start_run("/repo", orchestrator_id, ["tests/e2e"], "sha", "main")
    for nodeid, outcome in outcomes:
        db.upsert_test_result(run_id, nodeid, outcome, 1.0, None)
    db.finish_run(run_id, status, exit_code=0, duration_seconds=12.5)
    conn = sqlite3.connect(db.db_path)
    conn.execute("UPDATE e2e_runs SET started_at = ? WHERE id = ?", (started_at, run_id))
    conn.commit()
    conn.close()
    return run_id


@pytest.fixture
def seeded_db(tmp_path: Path) -> E2EDB:
    """A db with 3 runs (newest-first: warning, failed, passed) + failure issues."""
    db = E2EDB(tmp_path / "e2e.db")
    _seed_run(
        db,
        started_at="2026-07-16T00:00:00+00:00",
        status="passed",
        outcomes=[("t::chronic", "passed"), ("t::solid", "passed")],
    )
    _seed_run(
        db,
        started_at="2026-07-17T00:00:00+00:00",
        status="failed",
        outcomes=[("t::chronic", "failed"), ("t::flaky", "error"), ("t::solid", "passed")],
    )
    _seed_run(
        db,
        started_at="2026-07-18T00:00:00+00:00",
        status="warning",
        outcomes=[("t::chronic", "failed"), ("t::solid", "passed")],
    )
    # t::chronic failed twice (runs 2 + 3) -> chronic (>= 2); tracked + unresolved.
    db.record_failure_issue("t::chronic", 6822, 100, 2, "sha")
    return db


class TestRecentRunFacts:
    def test_newest_first_with_per_run_counts(self, seeded_db: E2EDB) -> None:
        runs, _ = read_e2e_health_facts(seeded_db.db_path, recent_run_limit=8)

        assert [r.status for r in runs] == ["warning", "failed", "passed"]
        # Newest run: t::chronic failed, t::solid passed.
        assert (runs[0].passed_count, runs[0].failed_count) == (1, 1)
        # Middle run: error counts as failed; one passed.
        assert (runs[1].passed_count, runs[1].failed_count) == (1, 2)
        assert (runs[2].passed_count, runs[2].failed_count) == (2, 0)
        assert runs[0].duration_seconds == 12.5

    def test_limit_bounds_rows(self, seeded_db: E2EDB) -> None:
        runs, _ = read_e2e_health_facts(seeded_db.db_path, recent_run_limit=2)

        assert [r.status for r in runs] == ["warning", "failed"]

    def test_run_without_test_rows_keeps_zero_counts(self, tmp_path: Path) -> None:
        db = E2EDB(tmp_path / "e2e.db")
        _seed_run(db, started_at="2026-07-18T00:00:00+00:00", status="error", outcomes=[])

        runs, _ = read_e2e_health_facts(db.db_path, recent_run_limit=8)

        assert (runs[0].passed_count, runs[0].failed_count) == (0, 0)


class TestChronicFailureFacts:
    def test_recurring_only_with_tracking_status(self, seeded_db: E2EDB) -> None:
        _, chronic = read_e2e_health_facts(seeded_db.db_path, recent_run_limit=8)

        # t::flaky failed once -> below min-fails; t::chronic failed twice.
        assert [c.nodeid for c in chronic] == ["t::chronic"]
        assert chronic[0].fail_count == 2
        assert chronic[0].tracking_issue == 6822
        assert chronic[0].tracking_resolved is False

    def test_resolved_tracking_is_reflected(self, seeded_db: E2EDB) -> None:
        seeded_db.resolve_failure_issue("t::chronic", "manual")

        _, chronic = read_e2e_health_facts(seeded_db.db_path, recent_run_limit=8)

        assert chronic[0].tracking_resolved is True

    def test_untracked_chronic_has_no_issue(self, tmp_path: Path) -> None:
        db = E2EDB(tmp_path / "e2e.db")
        for day in (16, 17):
            _seed_run(
                db,
                started_at=f"2026-07-{day}T00:00:00+00:00",
                status="failed",
                outcomes=[("t::orphan", "failed")],
            )

        _, chronic = read_e2e_health_facts(db.db_path, recent_run_limit=8)

        assert chronic[0].nodeid == "t::orphan"
        assert chronic[0].tracking_issue is None


class TestReadOnly:
    def test_read_does_not_mutate_the_db(self, seeded_db: E2EDB) -> None:
        before = hashlib.md5(seeded_db.db_path.read_bytes()).hexdigest()

        read_e2e_health_facts(seeded_db.db_path, recent_run_limit=8)

        after = hashlib.md5(seeded_db.db_path.read_bytes()).hexdigest()
        assert before == after

    def test_table_less_db_raises_for_caller_to_swallow(self, tmp_path: Path) -> None:
        empty = tmp_path / "empty.db"
        sqlite3.connect(empty).close()  # a real sqlite file, but no e2e tables

        with pytest.raises(sqlite3.Error):
            read_e2e_health_facts(empty, recent_run_limit=8)


class TestEndToEndProjection:
    def test_seeded_db_projects_the_neglect_signals(self, seeded_db: E2EDB) -> None:
        runs, chronic = read_e2e_health_facts(seeded_db.db_path, recent_run_limit=8)

        health = BoardE2EHealth.project(
            now=NOW,
            enabled=True,
            expected_interval_minutes=240,
            runs=runs,
            chronic_failures=chronic,
            quarantine_count=0,
        )

        # Newest run 12h old > 3*240min -> off-cadence.
        assert health.stale is True
        # warning, failed, then passed -> streak of 2.
        assert health.nonpassing_streak == 2
        assert health.last_run.status == "warning"
        assert health.chronic_failures[0].nodeid == "t::chronic"
        assert health.chronic_failures[0].fail_count == 2
