"""SQLite startup checks and backup utilities."""

from __future__ import annotations

import logging
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .config import Config
from .sqlite_registry import list_sqlite_databases, SQLiteDatabase

logger = logging.getLogger(__name__)


def apply_pragmas(conn: sqlite3.Connection) -> None:
    """Apply durability pragmas."""
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = FULL")


def quick_check_db(path: Path) -> tuple[bool, str]:
    try:
        conn = sqlite3.connect(str(path), timeout=2.0)
        row = conn.execute("PRAGMA quick_check").fetchone()
        conn.close()
    except sqlite3.DatabaseError as exc:
        return False, f"quick_check error: {exc}"
    if row and row[0] == "ok":
        return True, "quick_check: ok"
    return False, f"quick_check: {row[0] if row else 'unknown'}"


def enforce_pragmas_on_startup(config: Config) -> None:
    """Touch each DB and apply WAL/FULL pragmas if the file exists."""
    for db in list_sqlite_databases(config):
        if not db.enforce_pragmas or not db.enabled_fn(config):
            continue
        path = db.path_fn(config)
        if not path.exists():
            continue
        try:
            conn = sqlite3.connect(str(path), timeout=2.0)
            apply_pragmas(conn)
            conn.close()
        except sqlite3.DatabaseError as exc:
            logger.warning("[startup] Failed to apply sqlite pragmas (%s): %s", db.key, exc)


@dataclass(frozen=True)
class BackupResult:
    db: SQLiteDatabase
    path: Path
    performed: bool
    reason: str


def _backup_root(config: Config) -> Path:
    return config.repo_root / ".issue-orchestrator" / "backups" / "sqlite"


def _daily_dir(root: Path, key: str) -> Path:
    return root / key / "daily"


def _weekly_dir(root: Path, key: str) -> Path:
    return root / key / "weekly"


def _latest_backup_mtime(path: Path) -> float | None:
    if not path.exists():
        return None
    latest: float | None = None
    for entry in path.rglob("*.db"):
        try:
            mtime = entry.stat().st_mtime
        except FileNotFoundError:
            continue
        if latest is None or mtime > latest:
            latest = mtime
    return latest


@dataclass(frozen=True)
class BackupStatus:
    db: SQLiteDatabase
    latest_mtime: float | None
    due: bool
    reason: str


def _backup_due(config: Config, backup_root: Path, db: SQLiteDatabase, now: float) -> bool:
    latest = _latest_backup_mtime(backup_root / db.key)
    if latest is None:
        return True
    cadence_seconds = config.sqlite_backup.cadence_hours * 3600
    return now - latest >= cadence_seconds


def get_backup_statuses(config: Config) -> list[BackupStatus]:
    """Return backup status per DB without performing backups."""
    backup_root = _backup_root(config)
    now = datetime.now(timezone.utc).timestamp()
    statuses: list[BackupStatus] = []

    if not config.sqlite_backup.enabled:
        for db in list_sqlite_databases(config):
            statuses.append(BackupStatus(db=db, latest_mtime=None, due=False, reason="disabled"))
        return statuses

    for db in list_sqlite_databases(config):
        if not db.backup or not db.enabled_fn(config):
            statuses.append(BackupStatus(db=db, latest_mtime=None, due=False, reason="not enabled"))
            continue

        src_path = db.path_fn(config)
        if not src_path.exists():
            statuses.append(BackupStatus(db=db, latest_mtime=None, due=False, reason="missing"))
            continue

        latest = _latest_backup_mtime(backup_root / db.key)
        if latest is None:
            statuses.append(BackupStatus(db=db, latest_mtime=None, due=True, reason="none"))
            continue

        cadence_seconds = config.sqlite_backup.cadence_hours * 3600
        due = now - latest >= cadence_seconds
        statuses.append(BackupStatus(db=db, latest_mtime=latest, due=due, reason="cadence" if due else "ok"))

    return statuses


def _backup_filename(prefix: str, now_dt: datetime) -> str:
    return f"{prefix}.db"


def _backup_db(src_path: Path, dst_path: Path) -> None:
    dst_path.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(str(src_path))
    dst = sqlite3.connect(str(dst_path))
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()


def _rotate_backups(dir_path: Path, keep: int) -> None:
    if keep <= 0 or not dir_path.exists():
        return
    backups = sorted(dir_path.glob("*.db"), key=lambda p: p.name, reverse=True)
    for entry in backups[keep:]:
        try:
            entry.unlink()
        except FileNotFoundError:
            continue


def _ensure_weekly_copy(daily_file: Path, weekly_dir: Path, now_dt: datetime) -> None:
    iso_year, iso_week, _ = now_dt.isocalendar()
    weekly_name = f"{iso_year}-W{iso_week:02d}.db"
    weekly_file = weekly_dir / weekly_name
    if weekly_file.exists():
        return
    weekly_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(daily_file, weekly_file)


def run_backups_if_due(config: Config) -> list[BackupResult]:
    """Run backups for enabled DBs if cadence elapsed."""
    results: list[BackupResult] = []
    if not config.sqlite_backup.enabled:
        for db in list_sqlite_databases(config):
            results.append(BackupResult(db=db, path=db.path_fn(config), performed=False, reason="disabled"))
        return results

    backup_root = _backup_root(config)
    now_dt = datetime.now(timezone.utc)
    now = now_dt.timestamp()

    for db in list_sqlite_databases(config):
        if not db.backup or not db.enabled_fn(config):
            results.append(BackupResult(db=db, path=db.path_fn(config), performed=False, reason="not enabled"))
            continue

        src_path = db.path_fn(config)
        if not src_path.exists():
            results.append(BackupResult(db=db, path=src_path, performed=False, reason="missing"))
            continue

        if not _backup_due(config, backup_root, db, now):
            results.append(BackupResult(db=db, path=src_path, performed=False, reason="cadence"))
            continue

        daily_dir = _daily_dir(backup_root, db.key)
        weekly_dir = _weekly_dir(backup_root, db.key)
        daily_name = _backup_filename(now_dt.date().isoformat(), now_dt)
        daily_file = daily_dir / daily_name

        try:
            _backup_db(src_path, daily_file)
            _ensure_weekly_copy(daily_file, weekly_dir, now_dt)
            _rotate_backups(daily_dir, config.sqlite_backup.retention_daily)
            _rotate_backups(weekly_dir, config.sqlite_backup.retention_weekly)
            results.append(BackupResult(db=db, path=daily_file, performed=True, reason="ok"))
        except sqlite3.DatabaseError as exc:
            logger.warning("[backup] Failed to backup %s: %s", db.key, exc)
            results.append(BackupResult(db=db, path=daily_file, performed=False, reason="error"))

    return results
