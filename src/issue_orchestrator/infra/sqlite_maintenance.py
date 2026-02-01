"""SQLite startup checks and backup utilities."""

from __future__ import annotations

import json
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
        with sqlite3.connect(str(path), timeout=2.0) as conn:
            row = conn.execute("PRAGMA quick_check").fetchone()
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


def _write_failure(backup_root: Path, db: SQLiteDatabase, error: str, now_dt: datetime) -> None:
    path = _failure_file(backup_root, db.key)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "timestamp": now_dt.isoformat(),
        "error": error[:500],
    }
    path.write_text(json.dumps(payload))



def _clear_failure(backup_root: Path, db: SQLiteDatabase) -> None:
    path = _failure_file(backup_root, db.key)
    try:
        path.unlink()
    except FileNotFoundError:
        return


def _daily_dir(root: Path, key: str) -> Path:
    return root / key / "daily"


def _weekly_dir(root: Path, key: str) -> Path:
    return root / key / "weekly"



def _failure_file(root: Path, key: str) -> Path:
    return root / key / "last_failure.json"


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


def _read_failure(backup_root: Path, db: SQLiteDatabase) -> tuple[float, str] | None:
    path = _failure_file(backup_root, db.key)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        ts = data.get("timestamp")
        message = data.get("error")
        if not ts or not message:
            return None
        return datetime.fromisoformat(ts).timestamp(), str(message)
    except Exception:
        return None


@dataclass(frozen=True)
class BackupStatus:
    db: SQLiteDatabase
    latest_mtime: float | None
    due: bool
    reason: str
    detail: str | None = None


def _backup_due(config: Config, backup_root: Path, db: SQLiteDatabase, now: float) -> bool:
    latest = _latest_backup_mtime(backup_root / db.key)
    if latest is None:
        return True
    cadence_seconds = config.sqlite_backup.cadence_hours * 3600
    return now - latest >= cadence_seconds




def _status_for_db(
    config: Config,
    backup_root: Path,
    db: SQLiteDatabase,
    now: float,
) -> BackupStatus:
    if not db.backup or not db.enabled_fn(config):
        return BackupStatus(db=db, latest_mtime=None, due=False, reason="not enabled")

    src_path = db.path_fn(config)
    if not src_path.exists():
        return BackupStatus(db=db, latest_mtime=None, due=False, reason="missing")

    latest = _latest_backup_mtime(backup_root / db.key)
    failure = _read_failure(backup_root, db)

    if latest is None:
        if failure:
            return BackupStatus(db=db, latest_mtime=None, due=True, reason="error", detail=failure[1])
        return BackupStatus(db=db, latest_mtime=None, due=True, reason="none")

    cadence_seconds = config.sqlite_backup.cadence_hours * 3600
    due = now - latest >= cadence_seconds
    if failure and failure[0] > latest:
        return BackupStatus(db=db, latest_mtime=latest, due=due, reason="error", detail=failure[1])

    reason = "cadence" if due else "ok"
    return BackupStatus(db=db, latest_mtime=latest, due=due, reason=reason)


def get_backup_statuses(config: Config) -> list[BackupStatus]:
    """Return backup status per DB without performing backups."""
    backup_root = _backup_root(config)
    now = datetime.now(timezone.utc).timestamp()

    if not config.sqlite_backup.enabled:
        return [
            BackupStatus(db=db, latest_mtime=None, due=False, reason="disabled")
            for db in list_sqlite_databases(config)
        ]

    keep_daily, keep_weekly = _tier_flags(config)
    if not keep_daily and not keep_weekly:
        return [
            BackupStatus(db=db, latest_mtime=None, due=False, reason="retention=0")
            for db in list_sqlite_databases(config)
        ]

    return [
        _status_for_db(config, backup_root, db, now)
        for db in list_sqlite_databases(config)
    ]


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


def _delete_backups(dir_path: Path) -> None:
    if not dir_path.exists():
        return
    for entry in dir_path.glob("*.db"):
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


def _tier_flags(config: Config) -> tuple[bool, bool]:
    return config.sqlite_backup.retention_daily > 0, config.sqlite_backup.retention_weekly > 0


def _prepare_backup_dirs(
    backup_root: Path,
    db: SQLiteDatabase,
    keep_daily: bool,
    keep_weekly: bool,
) -> tuple[Path, Path]:
    daily_dir = _daily_dir(backup_root, db.key)
    weekly_dir = _weekly_dir(backup_root, db.key)
    if not keep_daily:
        _delete_backups(daily_dir)
    if not keep_weekly:
        _delete_backups(weekly_dir)
    return daily_dir, weekly_dir


def _skip_result(db: SQLiteDatabase, path: Path, reason: str) -> BackupResult:
    return BackupResult(db=db, path=path, performed=False, reason=reason)


def _backup_weekly_direct(
    src_path: Path,
    weekly_dir: Path,
    now_dt: datetime,
) -> Path:
    iso_year, iso_week, _ = now_dt.isocalendar()
    weekly_file = weekly_dir / f"{iso_year}-W{iso_week:02d}.db"
    if not weekly_file.exists():
        _backup_db(src_path, weekly_file)
    return weekly_file


def _backup_single_db(
    config: Config,
    db: SQLiteDatabase,
    backup_root: Path,
    now_dt: datetime,
    now: float,
) -> BackupResult:
    if not db.backup or not db.enabled_fn(config):
        return _skip_result(db, db.path_fn(config), "not enabled")

    src_path = db.path_fn(config)
    if not src_path.exists():
        return _skip_result(db, src_path, "missing")

    keep_daily, keep_weekly = _tier_flags(config)
    daily_dir, weekly_dir = _prepare_backup_dirs(backup_root, db, keep_daily, keep_weekly)
    if not keep_daily and not keep_weekly:
        return _skip_result(db, src_path, "retention=0")

    if not _backup_due(config, backup_root, db, now):
        return _skip_result(db, src_path, "cadence")

    daily_name = _backup_filename(now_dt.date().isoformat(), now_dt)
    daily_file = daily_dir / daily_name

    try:
        if keep_daily:
            _backup_db(src_path, daily_file)
            _rotate_backups(daily_dir, config.sqlite_backup.retention_daily)
        if keep_weekly:
            if keep_daily:
                _ensure_weekly_copy(daily_file, weekly_dir, now_dt)
            else:
                daily_file = _backup_weekly_direct(src_path, weekly_dir, now_dt)
            _rotate_backups(weekly_dir, config.sqlite_backup.retention_weekly)
        _clear_failure(backup_root, db)
        return BackupResult(db=db, path=daily_file, performed=True, reason="ok")
    except sqlite3.DatabaseError as exc:
        logger.warning("[backup] Failed to backup %s: %s", db.key, exc)
        _write_failure(backup_root, db, str(exc), now_dt)
        return BackupResult(db=db, path=daily_file, performed=False, reason="error")


def run_backups_if_due(config: Config) -> list[BackupResult]:
    """Run backups for enabled DBs if cadence elapsed."""
    if not config.sqlite_backup.enabled:
        return [
            BackupResult(db=db, path=db.path_fn(config), performed=False, reason="disabled")
            for db in list_sqlite_databases(config)
        ]

    backup_root = _backup_root(config)
    now_dt = datetime.now(timezone.utc)
    now = now_dt.timestamp()

    return [
        _backup_single_db(config, db, backup_root, now_dt, now)
        for db in list_sqlite_databases(config)
    ]
