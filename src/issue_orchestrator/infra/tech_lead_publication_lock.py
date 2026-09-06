"""OS-backed publication exclusion for a repository authority store.

An open file description owns the lock across independent handles/processes.
The kernel releases it on process death; it never expires while a publisher
can still write. Lockfiles remain in place so all contenders use the same inode.
No SQLite transaction spans a remote request.
"""
from __future__ import annotations
import fcntl
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Generator


@contextmanager
def disposition_publication(db_path: Path, issue_number: int) -> Generator[bool]:
    if isinstance(issue_number, bool) or issue_number <= 0:
        raise ValueError("publication ownership requires a positive issue number")
    canonical = db_path.resolve()
    lock_path = canonical.with_name(f"{canonical.name}.disposition-{issue_number}.lock")
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    acquired = False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except BlockingIOError:
            pass
        yield acquired
    finally:
        os.close(fd)  # Closing releases ownership, including on BaseException.
