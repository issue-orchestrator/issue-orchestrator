"""The engine audit's log reading: one parser, one signature normaliser (#7490).

Log lines here are written by ``logging`` itself, through the formats the
engine's handlers use, so the parser is tested against what the engine
actually writes rather than against hand-typed lines.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from issue_orchestrator.infra.engine_log_reader import parse_log_line, read_log
from issue_orchestrator.infra.logging_config import (
    CONTEXT_LOG_FORMAT,
    ROTATING_LOG_DATEFMT,
    ROTATING_LOG_FORMAT,
    ContextFormatter,
)
from issue_orchestrator.observation.engine_log_census import census_log
from issue_orchestrator.observation.no_progress import (
    ENGINE_SUBJECT,
    normalize_signature,
    subject_of_text,
)

T0 = datetime(2026, 9, 28, 9, 0, 0).astimezone()


def _line(
    message: str,
    *,
    at: datetime = T0,
    level: int = logging.WARNING,
    logger: str = "issue_orchestrator.control.x",
    formatter: logging.Formatter | None = None,
    **extra: object,
) -> str:
    record = logging.LogRecord(logger, level, __file__, 1, message, None, None)
    record.__dict__.update(extra)
    record.created = at.timestamp()
    record.msecs = 0
    formatter = formatter or logging.Formatter(ROTATING_LOG_FORMAT, datefmt=ROTATING_LOG_DATEFMT)
    return formatter.format(record)


def _entries(*lines: str):
    return [entry for line in lines if (entry := parse_log_line(line)) is not None]


# -- normalisation ---------------------------------------------------------


def test_the_same_failure_with_fresh_ids_has_one_signature() -> None:
    first = (
        "Recovery drain operation failed for record r1:e2aeb3df4f5ffa6af7b8f9f0ffe"
        " at 2026-09-28T10:12:34.625114+00:00 run 023183ec-966c-4575-bcbd-c77d91e9015d"
        " after 3 attempts"
    )
    second = (
        "Recovery drain operation failed for record r7:0a1b2c3d4e5f60718293a4b5c6d"
        " at 2026-09-27T01:02:03+00:00 run 9f1c2d3e-4b5a-6789-abcd-ef0123456789"
        " after 12 attempts"
    )

    assert normalize_signature(first) == normalize_signature(second)
    assert normalize_signature(first) == (
        "Recovery drain operation failed for record rN:<sha> at <time> run <id> after N attempts"
    )


def test_normalisation_keeps_the_words_that_tell_failures_apart() -> None:
    assert normalize_signature("pattern 'sig-3' is terminal ('shipped')") == (
        "pattern 'sig-N' is terminal ('shipped')"
    )
    # A short hex-looking word is a word, not an id.
    assert normalize_signature("e2e lane failed") == "eNe lane failed"


@pytest.mark.parametrize(
    ("message", "subject"),
    [
        ("Reconciliation failed for issue #410: Has forbidden labels", "#410"),
        ("[issue-353] SESSION COMPLETE: status=TIMED_OUT", "#353"),
        ("CLEANUP: issue=353 path=/x/y", "#353"),
        ("Retrying label fetch for issue 12 after error", "#12"),
        ("Tech Lead completion rejected for #77; see #80", "#77"),
        ("Merge queue refused PR #12 for issue #4", "PR #12"),
        ("Could not read promoted issue porchpin/porchpin#9", ENGINE_SUBJECT),
        ("[LOOP] Planning cycle took 39.5s", ENGINE_SUBJECT),
    ],
)
def test_a_log_message_names_its_subject_in_the_event_spelling(message, subject) -> None:
    assert subject_of_text(message) == subject


# -- parsing ---------------------------------------------------------------


def test_both_engine_log_layouts_parse_to_the_same_entry() -> None:
    rotating = _line("[LOOP] Tick took 3.0s")
    context = _line("[LOOP] Tick took 3.0s", formatter=ContextFormatter(CONTEXT_LOG_FORMAT))

    parsed = [parse_log_line(rotating), parse_log_line(context)]

    assert [(e.at, e.level, e.logger, e.message) for e in parsed] == [
        (T0, "WARNING", "issue_orchestrator.control.x", "[LOOP] Tick took 3.0s")
    ] * 2


def test_the_context_layout_strips_its_context_but_not_a_bracketed_message() -> None:
    line = _line(
        "[LOOP] Tick took 3.0s",
        formatter=ContextFormatter(CONTEXT_LOG_FORMAT),
        run_id="023183ec-966c",
        tick_id=33,
    )

    entry = parse_log_line(line)

    assert "tick_id=33" in line
    assert entry is not None and entry.message == "[LOOP] Tick took 3.0s"


def test_a_traceback_line_is_not_an_entry() -> None:
    assert parse_log_line('  File "x.py", line 3, in <module>') is None
    assert parse_log_line("issue_orchestrator.ports.PatternRegistryError: boom") is None


def test_a_bounded_read_drops_the_partial_first_line(tmp_path: Path) -> None:
    log = tmp_path / "orchestrator.log"
    lines = [_line(f"failure {n}", at=T0 + timedelta(seconds=n)) for n in range(50)]
    log.write_text("\n".join(lines) + "\n", encoding="utf-8")
    # The tail starts five bytes before the end of the second-to-last line.
    tail = len(lines[-1]) + 1 + 5

    excerpt, entries = read_log(log, tail_bytes=tail)
    read = list(entries)

    assert excerpt.truncated is True
    assert excerpt.bytes_read == tail
    assert [e.message for e in read] == ["failure 49"]


def test_a_small_log_is_read_whole(tmp_path: Path) -> None:
    log = tmp_path / "orchestrator.log"
    log.write_text(_line("only") + "\n", encoding="utf-8")

    excerpt, entries = read_log(log, tail_bytes=1 << 20)

    assert excerpt.truncated is False
    assert [e.message for e in entries] == ["only"]


# -- census ----------------------------------------------------------------


def test_repeats_are_counted_per_subject_and_since_its_last_state_change() -> None:
    failure = "Reconciliation failed for issue #{n}: Has forbidden labels"
    entries = _entries(
        *(_line(failure.format(n=410), at=T0 + timedelta(minutes=m)) for m in range(6)),
        *(_line(failure.format(n=411), at=T0 + timedelta(minutes=m)) for m in range(3)),
        _line("[LOOP] Planning cycle took 9.1s", at=T0),
        _line("[LOOP] Planning cycle took 12.7s", at=T0 + timedelta(minutes=1)),
        _line("just information", level=logging.INFO),
    )

    census = census_log(
        entries,
        window_start=T0 - timedelta(hours=1),
        # #410's labels changed after its third failure.
        last_state_change={"#410": T0 + timedelta(minutes=2, seconds=30)},
    )

    got = {(s.subject, s.signature): (s.count, s.since_state_change) for s in census.signatures}
    assert got == {
        ("#410", "Reconciliation failed for issue #N: Has forbidden labels"): (6, 3),
        ("#411", "Reconciliation failed for issue #N: Has forbidden labels"): (3, 3),
        (ENGINE_SUBJECT, "[LOOP] Planning cycle took N.Ns"): (2, 2),
    }


def test_entries_before_the_window_are_not_counted() -> None:
    entries = _entries(
        _line("boom", at=T0 - timedelta(hours=2), level=logging.ERROR),
        _line("boom", at=T0, level=logging.ERROR),
    )

    census = census_log(entries, window_start=T0 - timedelta(hours=1), last_state_change={})

    assert [(s.count, s.first_seen) for s in census.signatures] == [(1, T0.isoformat())]
    assert census.first_entry_at == T0


def _fetch(mode: str, calls: int, duration: int, at: datetime) -> str:
    return _line(
        f"[FETCH-COST] mode={mode} trigger=scheduled gh_calls={calls} gh_errors=0"
        f" duration_ms={duration} refreshed_issues=51",
        at=at,
        level=logging.INFO,
    )


def _get(issue: int, at: datetime = T0) -> str:
    return _line(
        f'HTTP Request: GET https://api.github.com/repos/o/r/issues/{issue} "HTTP/1.1 200 OK"',
        at=at,
        level=logging.INFO,
        logger="httpx",
    )


def _iteration(n: int, at: datetime = T0) -> str:
    return _line(f"[LOOP] Iteration {n} - active=3 paused=False", at=at, level=logging.INFO)


def test_fetch_cost_splits_refreshes_by_mode() -> None:
    entries = _entries(
        _fetch("full", 57, 16000, T0),
        _fetch("incremental", 82, 30000, T0 + timedelta(minutes=30)),
        _fetch("incremental", 105, 34000, T0 + timedelta(minutes=45)),
        _fetch("incremental", 93, 31000, T0 + timedelta(hours=1)),
    )

    cost = census_log(entries, window_start=T0, last_state_change={}).fetch_cost

    modes = {m.mode: m for m in cost.by_mode}
    assert (modes["full"].refreshes, modes["full"].gh_calls_median) == (1, 57)
    assert (
        modes["incremental"].refreshes,
        modes["incremental"].gh_calls_median,
        modes["incremental"].gh_calls_max,
        modes["incremental"].duration_ms_max,
    ) == (3, 93, 105, 34000)
    # 82 + 105 + 93 calls arrived over the hour after the first refresh.
    assert cost.refresh_calls_per_hour == pytest.approx(280.0)


def test_an_iteration_that_reads_one_issue_twice_is_a_repeat_read() -> None:
    entries = _entries(
        _get(9),  # before any iteration marker: counted, not attributed
        _iteration(1),
        _get(1),
        _get(2),
        _get(1),
        _line('HTTP Request: GET https://api.github.com/repos/o/r/issues/1/comments "HTTP/1.1 200 OK"',
              level=logging.INFO, logger="httpx"),
        _iteration(2, T0 + timedelta(minutes=1)),
        _get(1, T0 + timedelta(minutes=1)),
        _get(2, T0 + timedelta(minutes=1)),
    )

    cost = census_log(entries, window_start=T0, last_state_change={}).fetch_cost

    assert (cost.cycles, cost.cycles_with_repeat_reads, cost.issue_get_lines) == (2, 1, 6)
    assert cost.worst_cycle is not None
    assert (cost.worst_cycle.single_issue_gets, cost.worst_cycle.distinct_issues) == (3, 2)


def test_no_request_lines_reads_as_unmeasured_not_as_clean() -> None:
    cost = census_log(_entries(_iteration(1)), window_start=T0, last_state_change={}).fetch_cost

    assert (cost.cycles, cost.issue_get_lines, cost.worst_cycle) == (1, 0, None)


def test_timezone_of_log_times_is_the_local_zone() -> None:
    entry = parse_log_line(_line("x"))

    assert entry is not None
    assert entry.at.astimezone(UTC) == T0.astimezone(UTC)
