"""Copy a live engine database without touching it (#7490).

An audit of a running engine must not be able to change what it audits. A
SQLite connection to the live file, even ``mode=ro``, is not enough: a reader
of a WAL database marks its read lock in the shared ``-shm`` file, and may
create that file. So the live files are never opened by SQLite at all. Their
bytes are copied, the main file and its ``-wal`` together, and the copy is the
only thing SQLite ever opens.

A byte copy of a database being written can be torn, so a copy is kept only
if every file it came from has the same identity, size and modification time
after the copy as before (no commit, no checkpoint landed meanwhile) and the
copy passes SQLite's ``quick_check``. The copy is then opened, which replays
its copied log, and switched to a rollback journal so later read-only opens
need no sidecars. A database that changes under every attempt is reported
unreadable rather than copied torn.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path

from ..domain.read_only_sqlite import ReadOnlySqliteAccessError, ReadOnlySqliteFailure

#: Copies attempted before a database that keeps changing under the copy is
#: reported unreadable. An engine commits every few seconds and a copy of its
#: largest file takes well under one, so a quiet interval comes quickly.
SNAPSHOT_ATTEMPTS = 10

_Identity = tuple[int, int, int, int] | None


def snapshot_sqlite(live: Path, destination: Path, *, timeout: float) -> Path:
    """Copy ``live`` (and its write-ahead log) to ``destination``; return ``destination``.

    Raises :class:`~..domain.read_only_sqlite.ReadOnlySqliteAccessError`:
    ``DATABASE_ABSENT`` when ``live`` does not exist (the caller decides what
    that means), ``UNREADABLE`` when no consistent copy could be taken.
    ``destination`` must not exist yet. ``timeout`` bounds opening the copy.
    """
    if destination.exists():
        raise FileExistsError(f"snapshot destination already exists: {destination}")
    for _attempt in range(SNAPSHOT_ATTEMPTS):
        if _consistent_copy(live, destination, timeout=timeout):
            return destination
    raise ReadOnlySqliteAccessError(
        ReadOnlySqliteFailure.UNREADABLE,
        f"{live} changed during each of {SNAPSHOT_ATTEMPTS} copies",
    )


def _consistent_copy(live: Path, destination: Path, *, timeout: float) -> bool:
    log = _log_beside(live)
    copied_log = _log_beside(destination)
    before = (_identity(live, required=True), _identity(log, required=False))
    try:
        shutil.copyfile(live, destination)
        if before[1] is not None:
            shutil.copyfile(log, copied_log)
    except FileNotFoundError:
        # The log was checkpointed away (or the file replaced) mid-copy.
        _discard(destination)
        return False
    if (_identity(live, required=True), _identity(log, required=False)) != before:
        _discard(destination)
        return False
    try:
        with closing(sqlite3.connect(destination, timeout=timeout)) as copy:
            # Replays the copied log into the copy, then drops the log.
            copy.execute("PRAGMA journal_mode=DELETE")
            intact = copy.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    except sqlite3.DatabaseError:
        intact = False
    if not intact:
        _discard(destination)
    return intact


def _identity(path: Path, *, required: bool) -> _Identity:
    try:
        found = os.stat(path)
    except FileNotFoundError as error:
        if not required:
            return None
        raise ReadOnlySqliteAccessError(
            ReadOnlySqliteFailure.DATABASE_ABSENT, f"SQLite database is absent: {path}"
        ) from error
    return (found.st_dev, found.st_ino, found.st_size, found.st_mtime_ns)


def _discard(destination: Path) -> None:
    for path in (destination, _log_beside(destination), destination.with_name(destination.name + "-shm")):
        path.unlink(missing_ok=True)


def _log_beside(path: Path) -> Path:
    return path.with_name(path.name + "-wal")


__all__ = ["SNAPSHOT_ATTEMPTS", "snapshot_sqlite"]
