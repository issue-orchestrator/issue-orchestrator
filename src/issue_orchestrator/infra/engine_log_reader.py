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

Log times are the writing host's local wall clock (``logging``'s ``asctime``),
with no offset. The audit reads the state directory of an engine on this
host, so they are read in this host's local zone. The one hour a year that
local time repeats (a daylight-saving fall-back) is resolved by the log's own
order: a file written in time order that steps back into an ambiguous hour
has entered its second occurrence (:func:`resolve_local_time`).
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .logging_config import CONTEXT_LOG_FIELDS, MESSAGE_CONTINUATION

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
    #: False when ``at`` is a guess: a local time in the fall-back hour with
    #: no earlier entry to say which occurrence it is. Such an entry's instant
    #: must not decide anything time-based.
    certain: bool = True
    #: The exception line closing a traceback logged with this entry
    #: (``logger.exception``), which is what tells two failures apart when
    #: their messages are the same; ``""`` without one.
    exception: str = ""


@dataclass(frozen=True, slots=True)
class EngineLogExcerpt:
    """Which part of the log was read."""

    path: Path
    #: The file's size when it was read; later growth is not read.
    size: int
    bytes_read: int
    #: True when the file is longer than the tail that was read.
    truncated: bool


_TRACEBACK = "Traceback (most recent call last):"

#: How far a log may step backwards (thread interleaving) before an
#: ambiguous local time is read as the repeated hour's second occurrence.
_OUT_OF_ORDER = timedelta(minutes=1)


def resolve_local_time(local: datetime, previous: datetime | None) -> tuple[datetime, bool]:
    """The UTC instant of a naive local log time given the entry before it, and
    whether that instant is certain.

    Unambiguous local times have one instant. In a repeated hour the first
    occurrence is taken unless it would put this entry before the previous
    one: then the log has moved on into the second occurrence. With no
    previous entry (a tail that starts inside the repeated hour) nothing says
    which occurrence it is, and the instant is uncertain.
    """
    first = local.replace(fold=0).astimezone(UTC)
    second = local.replace(fold=1).astimezone(UTC)
    if first == second:
        return first, True
    if previous is None:
        return first, False
    return (first if first >= previous - _OUT_OF_ORDER else second), True


def parse_log_line(line: str, *, previous: datetime | None = None) -> EngineLogEntry | None:
    """The entry a log line starts, or None for a continuation line.

    ``previous`` is the instant of the entry before it in the same log, which
    resolves a local time the fall-back hour makes ambiguous.
    """
    match = _ROTATING.match(line) or _CONTEXT.match(line)
    if match is None:
        return None
    at, certain = resolve_local_time(
        datetime.strptime(match["at"], "%Y-%m-%d %H:%M:%S"), previous
    )
    return EngineLogEntry(
        at=at,
        level=match["level"],
        logger=match["logger"],
        message=match["message"],
        certain=certain,
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
    """Entries with the traceback each one carries folded into it.

    An entry is yielded once the next one starts (or the read ends), because
    its traceback follows it on continuation lines.
    """
    with excerpt.path.open("rb") as handle:
        handle.seek(excerpt.size - excerpt.bytes_read)
        if excerpt.truncated:
            handle.readline()
        remaining = excerpt.size - handle.tell()
        previous: datetime | None = None
        pending: EngineLogEntry | None = None
        traceback = False
        exception = ""
        for raw in handle:
            if remaining <= 0:
                break
            remaining -= len(raw)
            line = raw.decode("utf-8", errors="replace").rstrip("\n")
            entry = parse_log_line(line, previous=previous)
            if entry is None:
                # A framed record's continuation lines carry the frame; an
                # older engine's did not. Either way the traceback is read.
                line = line.removeprefix(MESSAGE_CONTINUATION)
                traceback = traceback or line.startswith(_TRACEBACK)
                if traceback and line.strip() and not line.startswith((" ", "\t")):
                    exception = line.strip()
                continue
            if pending is not None:
                yield replace(pending, exception=exception)
            pending, traceback, exception = entry, False, ""
            if entry.certain:
                previous = entry.at
        if pending is not None:
            yield replace(pending, exception=exception)


__all__ = [
    "EngineLogEntry",
    "EngineLogExcerpt",
    "log_excerpt",
    "parse_log_line",
    "resolve_local_time",
    "read_log",
]
