"""Strict file I/O and durability primitives shared by capture and release."""

import os
import stat
import uuid
from pathlib import Path


def fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def durable_directory(path: Path) -> None:
    if path.exists():
        if path.is_symlink() or not path.is_dir():
            raise ValueError("escrow directory must not be a symlink or file")
        return
    durable_directory(path.parent)
    path.mkdir(exist_ok=True)
    fsync_directory(path.parent)


def read_regular(path: Path, size: int) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size != size:
            raise ValueError(f"artifact is not a regular file of expected size: {path}")
        data = stream.read(size + 1)
    if len(data) != size:
        raise ValueError(f"artifact changed while reading: {path}")
    return data


def write_durable(path: Path, data: bytes) -> None:
    with path.open("xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def publish_durable(path: Path, data: bytes, *, staging_root: Path) -> None:
    """Publish complete bytes atomically without replacing an existing name.

    Interrupted private staging remains in the escrow owner's diagnostic .tmp
    namespace. A replay uses a fresh staging allocation, so a torn write cannot
    masquerade as a final receipt/artifact or strand its publication workspace.
    """
    durable_directory(staging_root)
    staging = staging_root / uuid.uuid4().hex
    durable_directory(staging)
    temporary = staging / path.name
    write_durable(temporary, data)
    fsync_directory(staging)
    os.link(temporary, path, follow_symlinks=False)
    fsync_directory(path.parent)
    temporary.unlink()
    staging.rmdir()
    fsync_directory(staging_root)
