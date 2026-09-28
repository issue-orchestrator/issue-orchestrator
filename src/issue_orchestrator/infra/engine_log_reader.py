"""Read an engine's repo log back as entries, read-only and bounded (#7490).

The log is the one record of what the engine did that no store keeps: every
failure it retried, every GitHub request it made. ``io engine-audit`` reads it
with this module and nothing else, so there is one parser for the two line
layouts :mod:`.logging_config` writes (``ROTATING_LOG_FORMAT`` and
``CONTEXT_LOG_FORMAT``). A line in neither layout (a traceback, a wrapped
message) continues the entry above it and is not an entry of its own.

Bounded: only the last ``tail_bytes`` of the file are read, so a log that was
never rotated (porchpin's reached 280 MB) costs the same as a small one. The
excerpt says whether it started mid-file, so a reader knows its counts cover
only the tail.

Log times are the writing host's local wall clock (``logging``'s ``asctime``).
The audit reads the state directory of an engine on this host, so they are
read in this host's local zone.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .logging_config import CONTEXT_LOG_FIELDS

_TIME = r"(?P<at>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})(?:,\d{3})?"
_LEVEL = r"(?P<level>DEBUG|INFO|WARNING|ERROR|CRITICAL)"
#: ``ROTATING_LOG_FORMAT``: ``<time> [<LEVEL>] <logger>: <message>``.
_ROTATING = re.compile(_TIME + r" \[" + _LEVEL + r"\] (?P<logger>\S+): (?P<message>.*)$")
#: ``CONTEXT_LOG_FORMAT``: ``<time> [<pid>] <logger> <LEVEL>:[ [<k=v> ...]] <message>``.
#: The context is recognised by its field names, so a message that itself
#: starts with a bracket (``[LOOP] ...``) is not mistaken for one.
_CONTEXT_FIELDS = "|".join(CONTEXT_LOG_FIELDS)
_CONTEXT = re.compile(
    _TIME
    + r" \[\d+\] (?P<logger>\S+) "
    + _LEVEL
    + r":(?: \[(?:" + _CONTEXT_FIELDS + r")=[^\]]*\])? (?P<message>.*)$"
)


@dataclass(frozen=True, slots=True)
class EngineLogEntry:
    at: datetime
    level: str
    logger: str
    message: str


@dataclass(frozen=True, slots=True)
class EngineLogExcerpt:
    """Which part of the log was read."""

    path: Path
    #: The file's size when it was read; later growth is not read.
    size: int
    bytes_read: int
    #: True when the file is longer than the tail that was read.
    truncated: bool


def parse_log_line(line: str) -> EngineLogEntry | None:
    """The entry a log line starts, or None for a continuation line."""
    match = _ROTATING.match(line) or _CONTEXT.match(line)
    if match is None:
        return None
    return EngineLogEntry(
        # Local wall clock (see the module docstring), held as UTC like every
        # other instant in the audit.
        at=datetime.strptime(match["at"], "%Y-%m-%d %H:%M:%S").astimezone().astimezone(UTC),
        level=match["level"],
        logger=match["logger"],
        message=match["message"],
    )


def log_excerpt(path: Path, *, tail_bytes: int) -> EngineLogExcerpt:
    """What :func:`read_log` reads of ``path`` as it is now."""
    if tail_bytes <= 0:
        raise ValueError("the log tail must be a positive number of bytes")
    size = path.stat().st_size
    return EngineLogExcerpt(
        path=path, size=size, bytes_read=min(size, tail_bytes), truncated=size > tail_bytes
    )


def read_log(
    path: Path, *, tail_bytes: int
) -> tuple[EngineLogExcerpt, Iterator[EngineLogEntry]]:
    """The last ``tail_bytes`` of ``path``: what was read, and its entries oldest first.

    Opened for reading only. The excerpt is fixed before reading, so a log
    that grows meanwhile is read up to the size the excerpt reports. When
    only a tail is read, its first line (which may start mid-line) is dropped
    rather than misread.
    """
    excerpt = log_excerpt(path, tail_bytes=tail_bytes)
    return excerpt, _entries(excerpt)


def _entries(excerpt: EngineLogExcerpt) -> Iterator[EngineLogEntry]:
    with excerpt.path.open("rb") as handle:
        handle.seek(excerpt.size - excerpt.bytes_read)
        if excerpt.truncated:
            handle.readline()
        remaining = excerpt.size - handle.tell()
        for raw in handle:
            if remaining <= 0:
                return
            remaining -= len(raw)
            entry = parse_log_line(raw.decode("utf-8", errors="replace").rstrip("\n"))
            if entry is not None:
                yield entry


__all__ = [
    "EngineLogEntry",
    "EngineLogExcerpt",
    "log_excerpt",
    "parse_log_line",
    "read_log",
]
