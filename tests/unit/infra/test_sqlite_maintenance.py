from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from issue_orchestrator.infra.config import Config
from issue_orchestrator.infra.sqlite_maintenance import run_backups_if_due
from issue_orchestrator.infra.repo_identity import state_dir
from issue_orchestrator.infra.sqlite_registry import list_sqlite_databases


def _create_sqlite_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS test (id INTEGER PRIMARY KEY)")
        conn.commit()
    finally:
        conn.close()


def test_run_backups_if_due_creates_backup(tmp_path):
    config = Config()
    config.repo_root = tmp_path

    publish_db = state_dir(tmp_path) / "goal_pilot.sqlite"
    session_db = state_dir(tmp_path) / "session_registry.sqlite"
    _create_sqlite_db(publish_db)
    _create_sqlite_db(session_db)

    results = run_backups_if_due(config)

    assert any(r.db.key == "goal_pilot" and r.performed for r in results)
    assert any(r.db.key == "session_registry" and r.performed for r in results)

    backup_root = tmp_path / ".issue-orchestrator" / "backups" / "sqlite"
    date_str = datetime.now(timezone.utc).date().isoformat()
    publish_daily = backup_root / "goal_pilot" / "daily" / f"{date_str}.db"
    session_daily = backup_root / "session_registry" / "daily" / f"{date_str}.db"

    assert publish_daily.exists()
    assert session_daily.exists()


def test_run_backups_if_due_respects_cadence(tmp_path):
    config = Config()
    config.repo_root = tmp_path

    publish_db = state_dir(tmp_path) / "goal_pilot.sqlite"
    _create_sqlite_db(publish_db)

    first = run_backups_if_due(config)
    assert any(r.db.key == "goal_pilot" and r.performed for r in first)

    second = run_backups_if_due(config)
    publish_result = next(r for r in second if r.db.key == "goal_pilot")
    assert publish_result.performed is False
    assert publish_result.reason == "cadence"


def test_retention_zero_disables_backups(tmp_path):
    config = Config()
    config.repo_root = tmp_path
    config.sqlite_backup.retention_daily = 0
    config.sqlite_backup.retention_weekly = 0

    publish_db = state_dir(tmp_path) / "goal_pilot.sqlite"
    _create_sqlite_db(publish_db)

    results = run_backups_if_due(config)
    publish_result = next(r for r in results if r.db.key == "goal_pilot")
    assert publish_result.performed is False
    assert publish_result.reason == "retention=0"

    backup_root = tmp_path / ".issue-orchestrator" / "backups" / "sqlite"
    assert not (backup_root / "goal_pilot").exists()


def test_sqlite_registry_includes_timeline_db(tmp_path):
    config = Config()
    config.repo_root = tmp_path

    databases = list_sqlite_databases(config)
    timeline = next((db for db in databases if db.key == "timeline"), None)

    assert timeline is not None
    assert timeline.path_fn(config) == state_dir(tmp_path) / "timeline.sqlite"
