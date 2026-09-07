"""Append-only JSONL publication and stable byte snapshots on local POSIX files.

All writers must cooperate. Lock the data inode, which must not be replaced or
truncated while in use. A reader holds a shared lock only to capture its extent;
the immutable prefix can then be read without delaying subsequent appends.
"""

from __future__ import annotations

import errno
import fcntl
import json
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

_LOCK_TIMEOUT_SECONDS = 5.0
_LOCK_POLL_SECONDS = 0.01
_READ_CHUNK_BYTES = 1024 * 1024


@contextmanager
def _publication_lock(fd: int, *, exclusive: bool) -> Iterator[None]:
    """Bound waiting for cooperating publishers; never retry other IO errors."""
    operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
    deadline = time.monotonic() + _LOCK_TIMEOUT_SECONDS
    while True:
        try:
            fcntl.flock(fd, operation | fcntl.LOCK_NB)
            break
        except OSError as error:
            if error.errno not in (errno.EACCES, errno.EAGAIN):
                raise
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    errno.ETIMEDOUT, "timed out acquiring JSONL publication lock"
                ) from error
            time.sleep(min(_LOCK_POLL_SECONDS, remaining))
    try:
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)


def append_jsonl(path: Path | None, record: dict[str, object]) -> None:
    """Publish one complete row, retaining single-write append semantics.

    The exclusive lock covers only the write. Serialization happens before it.
    A failed short write stays visible as corruption, never rolled back or hidden.
    """
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    line = (json.dumps(record, sort_keys=True) + "\n").encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        with _publication_lock(fd, exclusive=True):
            written = os.write(fd, line)
            if written != len(line):
                raise OSError(f"short JSONL write to {path}: {written} of {len(line)}")
    finally:
        os.close(fd)


def read_jsonl_snapshot(path: Path) -> bytes:
    """Read precisely one published extent, without chasing concurrent appends.

    Only absence at open means an empty journal. Later IO failures, including a
    file shortened below the captured extent, propagate rather than hiding data.
    The descriptor pins the same inode throughout acquisition and bulk reading.
    """
    try:
        fd = os.open(path, os.O_RDONLY)
    except FileNotFoundError:
        return b""
    try:
        with _publication_lock(fd, exclusive=False):
            remaining = os.fstat(fd).st_size
        chunks: list[bytes] = []
        while remaining:
            chunk = os.read(fd, min(remaining, _READ_CHUNK_BYTES))
            if not chunk:
                raise OSError(
                    errno.EIO, "JSONL file shortened below its snapshot extent", str(path)
                )
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)
