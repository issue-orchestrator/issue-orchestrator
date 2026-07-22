"""Composition-root wiring for the board snapshot's e2e-health reader.

``_make_e2e_health_reader`` closes over ``Config`` and resolves the repo's
``e2e.db`` + cadence + quarantine into a ``BoardE2EHealth`` projection. These
tests pin both sides of that boundary: a repo with no db (or an unreadable one)
degrades to ``None`` without raising, and a seeded db projects the real signal.
"""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from issue_orchestrator.entrypoints.bootstrap_tech_lead import _make_e2e_health_reader
from issue_orchestrator.infra.config import Config, E2EConfig
from issue_orchestrator.infra.e2e_db import E2EDB

NOW = datetime(2026, 7, 18, 18, 0, 0, tzinfo=timezone.utc)


def _config(repo_root: Path, *, enabled: bool = True, interval: int = 240) -> Config:
    return Config(
        repo_root=repo_root,
        e2e=E2EConfig(enabled=enabled, auto_run_interval_minutes=interval),
    )


def _db_path(repo_root: Path) -> Path:
    path = repo_root / ".issue-orchestrator" / "e2e.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def test_missing_e2e_db_yields_none(tmp_path: Path) -> None:
    reader = _make_e2e_health_reader(_config(tmp_path))

    assert reader(NOW) is None


def test_unreadable_table_less_db_degrades_to_none(tmp_path: Path) -> None:
    # A real sqlite file with no e2e tables: the read raises, the closure swallows.
    sqlite3.connect(_db_path(tmp_path)).close()

    reader = _make_e2e_health_reader(_config(tmp_path))

    assert reader(NOW) is None


def test_seeded_db_projects_the_signal(tmp_path: Path) -> None:
    db = E2EDB(_db_path(tmp_path))
    run_id = db.start_run("/repo", "orch", ["tests/e2e"], "sha", "main")
    db.upsert_test_result(run_id, "t::chronic", "failed", 1.0, None)
    db.finish_run(run_id, "failed", exit_code=1, duration_seconds=9.0)
    conn = sqlite3.connect(db.db_path)
    conn.execute(  # far in the past -> deterministically off-cadence
        "UPDATE e2e_runs SET started_at = ? WHERE id = ?",
        ("2020-01-01T00:00:00+00:00", run_id),
    )
    conn.commit()
    conn.close()

    health = _make_e2e_health_reader(_config(tmp_path, interval=240))(NOW)

    assert health is not None
    assert health.enabled is True
    assert health.expected_interval_minutes == 240
    assert health.stale is True
    assert health.nonpassing_streak == 1
    assert health.last_run is not None and health.last_run.status == "failed"


def test_disabled_e2e_still_projects_when_db_present(tmp_path: Path) -> None:
    """Disabled is a distinct signal (enabled=False, not stale), still reported."""
    db = E2EDB(_db_path(tmp_path))
    run_id = db.start_run("/repo", "orch", ["tests/e2e"], "sha", "main")
    db.finish_run(run_id, "passed", exit_code=0)

    health = _make_e2e_health_reader(_config(tmp_path, enabled=False))(NOW)

    assert health is not None
    assert health.enabled is False
    assert health.stale is False


@pytest.mark.parametrize("interval", [240, 0])
def test_quarantine_count_is_read_from_the_configured_file(
    tmp_path: Path, interval: int
) -> None:
    db = E2EDB(_db_path(tmp_path))
    run_id = db.start_run("/repo", "orch", ["tests/e2e"], "sha", "main")
    db.finish_run(run_id, "passed", exit_code=0)
    quarantine = tmp_path / "tests" / "e2e" / "quarantine.txt"
    quarantine.parent.mkdir(parents=True, exist_ok=True)
    quarantine.write_text("# header\ntests/e2e/test_a.py::t1\ntests/e2e/test_b.py::t2\n")

    health = _make_e2e_health_reader(_config(tmp_path, interval=interval))(NOW)

    assert health is not None
    assert health.quarantine_count == 2
