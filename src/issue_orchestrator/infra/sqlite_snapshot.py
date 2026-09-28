"""Copy a live engine database without ever opening it for writing (#7490).

An audit of a running engine must not be able to change what it audits. The
live file is only ever opened ``mode=ro`` and copied with SQLite's online
backup; everything afterwards opens only the copy, so a store that runs its
own schema setup on open touches the copy, never the engine's file.

Two read paths, chosen by whether the database has a write-ahead log beside
it:

* **A ``-wal`` file exists** (a writer has the database open, or left
  committed pages in the log): read through the shared no-create,
  ``query_only`` profile (:func:`.sqlite_readonly.open_sqlite_readonly`),
  which sees one consistent snapshot, log included, while the engine commits.
* **No ``-wal`` file** (every writer closed it; the validated-work store opens
  a connection per operation, so this is its usual state): every committed
  page is in the main file, but a ``mode=ro`` connection cannot open a WAL
  database whose ``-shm`` it may not create. The main file is read
  ``immutable`` instead, and the copy is kept only if the file's identity,
  size and modification time are the same after the copy as before, which
  proves no checkpoint wrote into it meanwhile. A file that keeps changing
  under the copy is reported unreadable rather than copied torn.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import closing
from pathlib import Path

from ..domain.read_only_sqlite import ReadOnlySqliteAccessError, ReadOnlySqliteFailure
from .sqlite_readonly import open_sqlite_readonly

#: Copies attempted before a database that keeps changing under the copy
#: is reported unreadable.
SNAPSHOT_ATTEMPTS = 3


def snapshot_sqlite(live: Path, destination: Path, *, timeout: float) -> Path:
    """Copy ``live`` to ``destination`` read-only; return ``destination``.

    Raises :class:`~..domain.read_only_sqlite.ReadOnlySqliteAccessError` when
    ``live`` is absent or unreadable (the caller decides what an absent
    database means). ``destination`` must not exist yet.

    The read path is chosen afresh on every attempt: a writer that closes the
    database between the ``-wal`` check and the open removes the log it was
    chosen for, and the next attempt then reads the closed file.
    """
    if destination.exists():
        raise FileExistsError(f"snapshot destination already exists: {destination}")
    for _attempt in range(SNAPSHOT_ATTEMPTS):
        if _log_beside(live).exists():
            try:
                with closing(
                    open_sqlite_readonly(live, timeout=timeout, row_factory=sqlite3.Row)
                ) as source:
                    _backup(source, destination)
                return destination
            except ReadOnlySqliteAccessError as error:
                destination.unlink(missing_ok=True)
                if error.reason is not ReadOnlySqliteFailure.UNREADABLE or _log_beside(live).exists():
                    raise
        elif _quiescent_copy(live, destination, timeout=timeout):
            return destination
    raise ReadOnlySqliteAccessError(
        ReadOnlySqliteFailure.UNREADABLE,
        f"{live} changed during each of {SNAPSHOT_ATTEMPTS} copies",
    )


def _quiescent_copy(live: Path, destination: Path, *, timeout: float) -> bool:
    """Copy a database no writer holds open; False if a checkpoint touched it meanwhile."""
    before = _identity(live)
    try:
        with closing(
            sqlite3.connect(
                live.resolve().as_uri() + "?mode=ro&immutable=1", uri=True, timeout=timeout
            )
        ) as source:
            source.execute("PRAGMA query_only=ON")
            _backup(source, destination)
    except sqlite3.Error as error:
        destination.unlink(missing_ok=True)
        raise ReadOnlySqliteAccessError(
            ReadOnlySqliteFailure.UNREADABLE, f"SQLite read failed: {error}"
        ) from error
    # A writer that opens the database meanwhile commits into a new log, not
    # the main file, so the copy is still one consistent state; only a
    # checkpoint writing the main file under the copy can tear it.
    if _identity(live) == before:
        return True
    destination.unlink()
    return False


def _identity(live: Path) -> tuple[int, int, int, int]:
    try:
        found = os.stat(live)
    except FileNotFoundError as error:
        raise ReadOnlySqliteAccessError(
            ReadOnlySqliteFailure.DATABASE_ABSENT, f"SQLite database is absent: {live}"
        ) from error
    return (found.st_dev, found.st_ino, found.st_size, found.st_mtime_ns)


def _backup(source: sqlite3.Connection, destination: Path) -> None:
    with closing(sqlite3.connect(destination)) as copy:
        source.backup(copy)
        # The copy is ours: a rollback journal means a later read-only open
        # needs no -wal/-shm beside it.
        copy.execute("PRAGMA journal_mode=DELETE")


def _log_beside(live: Path) -> Path:
    return live.with_name(live.name + "-wal")


__all__ = ["SNAPSHOT_ATTEMPTS", "snapshot_sqlite"]
