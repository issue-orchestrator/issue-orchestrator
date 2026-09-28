"""What an engine's log says it kept doing (#7490): repeats and fetch cost.

One pass over the log entries in the audit window feeds two tallies:

* **no-progress signatures**: every ERROR/WARNING normalized to its shape
  (:func:`~.no_progress.normalize_signature`) and its subject
  (:func:`~.no_progress.subject_of_text`), counted in the window and since the
  subject last changed state on its timeline. A shape that keeps repeating
  for a subject whose state never moves is the engine getting nowhere; the
  engine's own board-wide failures (no subject) never have a state change.
* **GitHub fetch cost**: the ``[FETCH-COST]`` line each queue refresh logs,
  by mode, and the single-issue GETs each loop iteration made (httpx request
  lines between two ``[LOOP] Iteration`` lines), so an iteration that reads
  the same issue several times shows.

Facts only: the thresholds that turn these into anomalies live with the
other anomaly rules in :mod:`.engine_audit`.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from statistics import median

from ..contracts.engine_audit import (
    FetchCostSection,
    FetchModeCost,
    IssueReadCycle,
    LogSignature,
)
from ..infra.engine_log_reader import EngineLogEntry
from .no_progress import normalize_signature, subject_of_text

#: Levels whose repeats the census counts.
CENSUS_LEVELS = frozenset({"WARNING", "ERROR", "CRITICAL"})

# ``orchestrator_support`` logs these; the census parses their fields back.
_FETCH_COST = re.compile(
    r"\[FETCH-COST\] mode=(?P<mode>\S+) trigger=\S+ gh_calls=(?P<calls>\d+)"
    r" gh_errors=(?P<errors>\d+) duration_ms=(?P<duration>\d+)"
)
_ITERATION = re.compile(r"^\[LOOP\] Iteration \d+\b")
#: ``GET <api>/repos/<owner>/<repo>/issues/<n>`` exactly: not its comments,
#: events or labels.
_ISSUE_GET = re.compile(r'^HTTP Request: GET \S+/repos/[^/\s]+/[^/\s]+/issues/(\d+) "')
_HTTP_LOGGER = "httpx"


@dataclass
class _SignatureTally:
    count: int = 0
    since_state_change: int | None = 0
    first_seen: datetime | None = None
    last_seen: datetime | None = None


@dataclass
class _Cycle:
    started_at: datetime
    gets: list[int] = field(default_factory=list)


@dataclass(frozen=True)
class LogCensus:
    signatures: tuple[LogSignature, ...]
    fetch_cost: FetchCostSection
    #: The first entry read at all, before the window or in it.
    first_read_at: datetime | None
    #: Entries whose instant could not be told (a tail starting inside the
    #: fall-back hour); none of them was counted.
    uncertain_entries: int
    first_entry_at: datetime | None
    last_entry_at: datetime | None

    def covers(self, window_start: datetime) -> bool:
        """Whether the entries counted are the whole window.

        Only an entry at or before ``window_start`` proves nothing inside the
        window was cut off by the bounded tail or a rotation, and an entry
        whose instant is unknown may have been in it.
        """
        return (
            self.uncertain_entries == 0
            and self.first_read_at is not None
            and self.first_read_at <= window_start
        )


def census_log(
    entries: Iterable[EngineLogEntry],
    *,
    window_start: datetime,
    window_end: datetime,
    last_state_change: Mapping[str, datetime] | None,
) -> LogCensus:
    """Tally ``entries`` from ``window_start`` to ``window_end`` (see the module docstring).

    Entries after ``window_end`` (written while the audit ran) belong to the
    next audit, not to a report that says its window ended before them.

    ``last_state_change`` maps a subject (``#N``) to the last instant its
    timeline recorded a state change; a subject absent from it had none.
    None means the timeline was not read, so no count "since the last state
    change" can be claimed and every such count is None.
    """
    tallies: dict[tuple[str, str, str, str], _SignatureTally] = {}
    fetches: dict[str, list[tuple[int, int, int, datetime]]] = {}
    cycles: list[_Cycle] = []
    issue_gets = 0
    first_read: datetime | None = None
    first: datetime | None = None
    last: datetime | None = None
    uncertain = 0
    for entry in entries:
        if not entry.certain:
            # Which hour it belongs to is unknown, so it counts for nothing
            # time-based; the read is incomplete instead (see LogCensus).
            uncertain += 1
            continue
        first_read = entry.at if first_read is None else first_read
        if not window_start <= entry.at <= window_end:
            continue
        first = entry.at if first is None else first
        last = entry.at
        if entry.level in CENSUS_LEVELS:
            _tally(tallies, entry, last_state_change)
        if (cost := _FETCH_COST.search(entry.message)) is not None:
            fetches.setdefault(cost["mode"], []).append(
                (int(cost["calls"]), int(cost["errors"]), int(cost["duration"]), entry.at)
            )
        elif _ITERATION.match(entry.message):
            cycles.append(_Cycle(entry.at))
        elif entry.logger == _HTTP_LOGGER and (get := _ISSUE_GET.match(entry.message)):
            issue_gets += 1
            # Requests before the first iteration marker belong to no whole
            # iteration the window saw; they are counted, not attributed.
            if cycles:
                cycles[-1].gets.append(int(get.group(1)))
    return LogCensus(
        signatures=tuple(
            LogSignature(
                subject=subject,
                level=level,
                logger=logger,
                signature=signature,
                count=tally.count,
                since_state_change=tally.since_state_change,
                first_seen=_iso(tally.first_seen),
                last_seen=_iso(tally.last_seen),
            )
            for (subject, level, logger, signature), tally in sorted(
                tallies.items(), key=lambda kv: (-kv[1].count, kv[0])
            )
        ),
        fetch_cost=_fetch_cost(fetches, cycles, issue_gets),
        first_read_at=first_read,
        uncertain_entries=uncertain,
        first_entry_at=first,
        last_entry_at=last,
    )


def _tally(
    tallies: dict[tuple[str, str, str, str], _SignatureTally],
    entry: EngineLogEntry,
    last_state_change: Mapping[str, datetime] | None,
) -> None:
    subject = subject_of_text(entry.message)
    signature = normalize_signature(entry.message)
    if entry.exception:
        signature += " | " + normalize_signature(entry.exception)
    key = (subject, entry.level, entry.logger, signature)
    tally = tallies.setdefault(
        key, _SignatureTally(since_state_change=None if last_state_change is None else 0)
    )
    tally.count += 1
    if last_state_change is not None and tally.since_state_change is not None:
        changed = last_state_change.get(subject)
        if changed is None or entry.at > changed:
            tally.since_state_change += 1
    tally.first_seen = tally.first_seen or entry.at
    tally.last_seen = entry.at


def _fetch_cost(
    fetches: Mapping[str, list[tuple[int, int, int, datetime]]],
    cycles: list[_Cycle],
    issue_gets: int,
) -> FetchCostSection:
    by_mode = tuple(
        FetchModeCost(
            mode=mode,
            refreshes=len(rows),
            gh_calls_total=sum(r[0] for r in rows),
            gh_calls_median=float(median(r[0] for r in rows)),
            gh_calls_max=max(r[0] for r in rows),
            gh_errors_total=sum(r[1] for r in rows),
            duration_ms_median=float(median(r[2] for r in rows)),
            duration_ms_max=max(r[2] for r in rows),
        )
        for mode, rows in sorted(fetches.items())
    )
    refreshes = sorted((r for rows in fetches.values() for r in rows), key=lambda r: r[3])
    per_hour: float | None = None
    if len(refreshes) >= 2:
        hours = (refreshes[-1][3] - refreshes[0][3]).total_seconds() / 3600
        # Calls of every refresh after the first, over the time they took to
        # arrive: the first refresh opens the interval, it is not inside it.
        per_hour = sum(r[0] for r in refreshes[1:]) / hours if hours > 0 else None
    read_cycles = [
        IssueReadCycle(
            started_at=c.started_at.isoformat(),
            single_issue_gets=len(c.gets),
            distinct_issues=len(set(c.gets)),
        )
        for c in cycles
    ]
    repeating = [c for c in read_cycles if c.repeat_reads > 0]
    return FetchCostSection(
        by_mode=by_mode,
        refresh_calls_per_hour=per_hour,
        cycles=len(read_cycles),
        cycles_with_repeat_reads=len(repeating),
        worst_cycle=max(repeating, key=lambda c: (c.repeat_reads, c.started_at), default=None),
        issue_get_lines=issue_gets,
    )


def _iso(value: datetime | None) -> str:
    if value is None:
        raise ValueError("a counted signature has been seen")
    return value.isoformat()


__all__ = ["CENSUS_LEVELS", "LogCensus", "census_log"]
