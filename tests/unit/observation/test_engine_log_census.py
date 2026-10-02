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

from issue_orchestrator.domain.blocked_open_pr import BlockedPRSkipReason
from issue_orchestrator.domain.tech_lead_run import WITHDRAWN_SUBJECT_NO_LONGER_ELIGIBLE
from issue_orchestrator.infra.engine_log_reader import parse_log_line, read_log
from issue_orchestrator.infra.logging_config import (
    CONTEXT_LOG_FORMAT,
    ROTATING_LOG_DATEFMT,
    ROTATING_LOG_FORMAT,
    ContextFormatter,
    FramedFormatter,
)
from issue_orchestrator.observation.engine_log_census import census_log
from issue_orchestrator.observation.no_progress import (
    ENGINE_SUBJECT,
    REFUSAL_REASONS,
    WorkRefusal,
    normalize_signature,
    refusal_of_text,
    subject_of_text,
    subjects_of_text,
)

T0 = datetime(2026, 9, 28, 9, 0, 0).astimezone()
END = T0 + timedelta(days=1)


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
        ("Could not read promoted issue other/repo#9", "other/repo#9"),
        ("[LOOP] Planning cycle took 39.5s", ENGINE_SUBJECT),
    ],
)
def test_a_log_message_names_its_subject_in_the_event_spelling(message, subject) -> None:
    assert subject_of_text(message) == subject


def test_a_qualified_reference_to_the_engines_own_repo_is_its_issue() -> None:
    assert subject_of_text("Could not read promoted issue Owner/Repo#9", repo="owner/repo") == "#9"
    assert subject_of_text("Could not read promoted issue io/io#9", repo="owner/repo") == "io/io#9"


def test_one_warning_each_about_five_issues_is_not_a_repeat() -> None:
    entries = _entries(
        *(
            _line(f"Could not read promoted issue io/io#{n}; leaving it", at=T0 + timedelta(minutes=n))
            for n in range(5)
        )
    )

    census = census_log(
        entries, window_start=T0, window_end=END, last_state_change={}, repo="owner/repo"
    )

    assert sorted(s.subject for s in census.signatures) == [f"io/io#{n}" for n in range(5)]
    assert all(s.since_state_change == 1 for s in census.signatures)


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
        window_end=END,
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

    census = census_log(entries, window_start=T0 - timedelta(hours=1), window_end=END, last_state_change={})

    assert census.first_read_at == T0 - timedelta(hours=2)
    assert census.covers(T0 - timedelta(hours=1)) is True
    assert census.covers(T0 - timedelta(hours=3)) is False
    assert [(s.count, s.first_seen) for s in census.signatures] == [
        (1, T0.astimezone(UTC).isoformat())
    ]
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

    cost = census_log(entries, window_start=T0, window_end=END, last_state_change={}).fetch_cost

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

    cost = census_log(entries, window_start=T0, window_end=END, last_state_change={}).fetch_cost

    assert (cost.cycles, cost.cycles_with_repeat_reads, cost.issue_get_lines) == (2, 1, 6)
    assert cost.worst_cycle is not None
    assert (cost.worst_cycle.single_issue_gets, cost.worst_cycle.distinct_issues) == (3, 2)


def test_no_request_lines_reads_as_unmeasured_not_as_clean() -> None:
    cost = census_log(_entries(_iteration(1)), window_start=T0, window_end=END, last_state_change={}).fetch_cost

    assert (cost.cycles, cost.issue_get_lines, cost.worst_cycle) == (1, 0, None)


def test_the_repeated_fall_back_hour_is_read_in_log_order(monkeypatch) -> None:
    """01:30 happens twice in Denver on 2026-11-01: MDT (07:30Z) then MST (08:30Z)."""
    import time

    monkeypatch.setenv("TZ", "America/Denver")
    time.tzset()
    try:
        lines = [
            "2026-11-01 01:30:00 [WARNING] io: first",
            "2026-11-01 01:59:00 [WARNING] io: still first",
            "2026-11-01 01:30:00 [WARNING] io: second",
            "2026-11-01 02:10:00 [WARNING] io: after",
        ]
        previous = None
        instants = []
        for line in lines:
            entry = parse_log_line(line, previous=previous)
            assert entry is not None
            previous = entry.at
            instants.append(entry.at.strftime("%H:%M"))
    finally:
        monkeypatch.undo()
        time.tzset()

    assert instants == ["07:30", "07:59", "08:30", "09:10"]


def test_a_tail_starting_inside_the_repeated_hour_counts_nothing_it_cannot_date(
    tmp_path: Path, monkeypatch
) -> None:
    """With no earlier entry, 01:30 on the fall-back night is 07:30Z or 08:30Z."""
    import time

    monkeypatch.setenv("TZ", "America/Denver")
    time.tzset()
    try:
        log = tmp_path / "orchestrator.log"
        log.write_text(
            "2026-11-01 01:30:00 [WARNING] io: Reconciliation failed for issue #4\n"
            "2026-11-01 01:31:00 [WARNING] io: Reconciliation failed for issue #4\n"
            "2026-11-01 02:10:00 [WARNING] io: Reconciliation failed for issue #4\n",
            encoding="utf-8",
        )
        _excerpt, entries = read_log(log, tail_bytes=1 << 20)
        read = list(entries)
        census = census_log(
            read,
            window_start=datetime(2026, 11, 1, 8, 0, tzinfo=UTC),
            window_end=datetime(2026, 11, 2, tzinfo=UTC),
            last_state_change={},
        )
    finally:
        monkeypatch.undo()
        time.tzset()

    assert [e.certain for e in read] == [False, False, True]
    assert census.uncertain_entries == 2
    assert [s.count for s in census.signatures] == [1]
    # The two it could not date may be inside the window: not a complete read.
    assert census.covers(datetime(2026, 11, 1, 8, 0, tzinfo=UTC)) is False


def test_a_logged_exception_is_part_of_its_signature(tmp_path: Path) -> None:
    formatter = logging.Formatter(ROTATING_LOG_FORMAT, datefmt=ROTATING_LOG_DATEFMT)
    lines = []
    for n, error in enumerate((KeyError("sig-1"), TimeoutError("read 3"), KeyError("sig-2"))):
        try:
            raise error
        except Exception:
            import sys

            record = logging.LogRecord(
                "io", logging.ERROR, __file__, 1, "operation failed for issue #9", None, sys.exc_info()
            )
        record.created = (T0 + timedelta(seconds=n)).timestamp()
        record.msecs = 0
        lines.append(formatter.format(record))
    log = tmp_path / "orchestrator.log"
    log.write_text("\n".join(lines) + "\n", encoding="utf-8")

    _excerpt, entries = read_log(log, tail_bytes=1 << 20)
    read = list(entries)
    census = census_log(read, window_start=T0 - timedelta(hours=1), window_end=END, last_state_change={})

    assert [e.exception for e in read] == [
        "KeyError: 'sig-1'",
        "TimeoutError: read 3",
        "KeyError: 'sig-2'",
    ]
    assert {(s.signature, s.count) for s in census.signatures} == {
        ("operation failed for issue #N | KeyError: 'sig-N'", 2),
        ("operation failed for issue #N | TimeoutError: read N", 1),
    }


def test_entries_written_after_the_audit_instant_are_not_counted() -> None:
    entries = _entries(
        *(_line("boom", at=T0 + timedelta(minutes=m), level=logging.ERROR) for m in range(3)),
        *(_line("boom", at=T0 + timedelta(hours=2, minutes=m), level=logging.ERROR) for m in range(5)),
    )

    census = census_log(
        entries, window_start=T0, window_end=T0 + timedelta(hours=1), last_state_change={}
    )

    assert [s.count for s in census.signatures] == [3]


def test_a_multiline_message_is_one_entry_however_its_lines_look(tmp_path: Path) -> None:
    """Captured agent output carries its own timestamps and request lines."""
    captured = (
        "[issue-7] LAST OUTPUT:\n"
        "2026-09-28 09:00:01 [WARNING] io: Reconciliation failed for issue #7\n"
        'HTTP Request: GET https://api.github.com/repos/o/r/issues/7 "HTTP/1.1 200 OK"\n'
        "2026-09-28 09:00:02 [INFO] httpx: HTTP Request: GET"
        ' https://api.github.com/repos/o/r/issues/7 "HTTP/1.1 200 OK"'
    )
    log = tmp_path / "orchestrator.log"
    log.write_text(
        "\n".join(
            [
                _line("[LOOP] Iteration 1 - active=1", level=logging.INFO),
                _line(
                    captured,
                    formatter=FramedFormatter(ROTATING_LOG_FORMAT, datefmt=ROTATING_LOG_DATEFMT),
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    _excerpt, entries = read_log(log, tail_bytes=1 << 20)
    read = list(entries)
    census = census_log(read, window_start=T0 - timedelta(hours=1), window_end=END, last_state_change={})

    assert [e.message.splitlines()[0] for e in read] == ["[LOOP] Iteration 1 - active=1", "[issue-7] LAST OUTPUT:"]
    assert census.fetch_cost.issue_get_lines == 0
    assert [(s.subject, s.count) for s in census.signatures] == [("#7", 1)]


def test_a_log_shaped_line_inside_an_exception_is_not_an_entry(tmp_path: Path) -> None:
    formatter = FramedFormatter(ROTATING_LOG_FORMAT, datefmt=ROTATING_LOG_DATEFMT)
    try:
        raise RuntimeError("agent said:\n2026-09-28 09:00:01 [WARNING] io: Reconciliation failed for issue #7")
    except RuntimeError:
        import sys

        record = logging.LogRecord("io", logging.ERROR, __file__, 1, "drain failed", None, sys.exc_info())
    record.created = T0.timestamp()
    record.msecs = 0
    log = tmp_path / "orchestrator.log"
    log.write_text(formatter.format(record) + "\n", encoding="utf-8")

    _excerpt, entries = read_log(log, tail_bytes=1 << 20)
    read = list(entries)

    assert [e.message for e in read] == ["drain failed"]
    assert read[0].exception == "2026-09-28 09:00:01 [WARNING] io: Reconciliation failed for issue #7"
    # A plain formatter on another handler still gets the unframed traceback.
    assert "    | " not in logging.Formatter().format(record)


def test_timezone_of_log_times_is_the_local_zone() -> None:
    entry = parse_log_line(_line("x"))

    assert entry is not None
    assert entry.at.astimezone(UTC) == T0.astimezone(UTC)


# -- work refusals -----------------------------------------------------------

#: What porchpin's engine logged for PR #379 on every scan from 2026-10-01
#: 23:00 (local), and at the launch and at startup: INFO, every one.
SCANNER_SKIP = (
    "[SCANNER] Skipping stale review PR: pr=379 issue=364 reason=issue_blocked"
    " issue_labels=v1,needs-human,priority:medium,agent:backend,pr-pending"
    " pr_labels=tech-lead-reviewed,needs-code-review,rework-cycle-5"
)
LAUNCH_DROP = (
    "[launch] Dropping stale pending review: pr=379 issue=364 reason=issue_blocked"
    " issue_labels=v1,needs-human,priority:medium,agent:backend,pr-pending"
    " pr_labels=tech-lead-reviewed,needs-code-review,rework-cycle-5"
)


def test_a_message_names_every_subject_once_first_named_first() -> None:
    assert subjects_of_text(SCANNER_SKIP) == ("PR #379", "#364")
    # "#12" inside "PR #12" is the PR, not a second subject.
    assert subjects_of_text("Merge queue refused PR #12 for issue #4") == ("PR #12", "#4")
    assert subjects_of_text("[LOOP] Tick took 3.0s") == ()


@pytest.mark.parametrize(
    ("message", "refusal"),
    [
        (SCANNER_SKIP, WorkRefusal("PR #379", ("#364",), "issue_blocked")),
        (LAUNCH_DROP, WorkRefusal("PR #379", ("#364",), "issue_blocked")),
        (
            "trace-tech-lead-decision issue=200 flavor=failure_investigation decision=skip"
            " reason=subject_no_longer_eligible (pending=1)",
            WorkRefusal("#200", (), "subject_no_longer_eligible"),
        ),
        # The rework lane refuses on the same blocked-PR vocabulary.
        ("[SCANNER] Skipping blocked rework PR: pr=12 issue=4 reason=pr_blocked blocking=blocked",
         WorkRefusal("PR #12", ("#4",), "pr_blocked")),
        ("[SCANNER] Skipping blocked rework PR: pr=12 issue=4 reason=issue_blocked blocking=needs-human",
         WorkRefusal("PR #12", ("#4",), "issue_blocked")),
    ],
)
def test_a_decision_not_to_do_planned_work_is_a_refusal(message: str, refusal: WorkRefusal) -> None:
    assert refusal_of_text(message) == refusal


@pytest.mark.parametrize(
    "message",
    [
        # Waits: a dependency, the issue's own block, capacity, a pause, work
        # already under way for the issue (every planner queue reason).
        "[issue-262] Skipped: reason=blocked_by_dependency detail=Blocked - waiting on: #179",
        "trace-queue-decision issue=262 decision=skip reason=blocked_label detail=blocking labels: needs-human",
        "[issue-4] Skipped review: pr=#4 reason=No capacity",
        "[issue-4] Skipped review: pr=#4 reason=Orchestrator paused",
        "[issue-4] Skipped rework: cycle=2 reason=Orchestrator paused",
        "[issue-328] Skipped: reason=active_session",
        "[issue-328] Skipped: reason=pending_rework",
        "[issue-328] Skipped: reason=pending_retrospective_review",
        "[issue-328] Skipped: reason=session_history",
        "[issue #9] Skipped: reason=provider_unavailable provider=codex",
        # The opt-in trace's copy of a rework skip is not a second refusal.
        "[TIMELINE] scanner.rework_skip pr=12 issue=4 reason=issue_blocked blocking=needs-human",
        "[TIMELINE] scanner.rework_skip pr=12 issue=4 reason=already_queued",
        "trace-tech-lead-decision issue=410 flavor=health_review decision=skip reason=global_run_awaiting_drain (pending=1)",
        # A reason no owner classes as a refusal.
        "[launch] Dropping queued rework: pr=5 reason=brand_new_reason",
        # No decision, or no reason given.
        "[SCANNER] Found orphaned PR #379 for code review reason=issue_blocked",
        "[issue-364] Launch refused: PR #379 carries published validated work",
        # Nothing it is about.
        "[TECH_LEAD] Skipping the sweep: reason=quiet_hours",
    ],
)
def test_a_wait_or_an_unreasoned_line_is_no_refusal(message: str) -> None:
    assert refusal_of_text(message) is None


def test_the_refusal_reasons_are_their_owners_vocabulary() -> None:
    """Both PR lanes' block refusals and the tech lead's withdrawal, by their
    owners' constants: nothing else is a refusal."""
    assert REFUSAL_REASONS == {r.value for r in BlockedPRSkipReason} | {WITHDRAWN_SUBJECT_NO_LONGER_ELIGIBLE}


def test_an_info_refusal_repeating_with_nothing_moving_is_counted() -> None:
    """The porchpin #379 livelock: the ERROR/WARNING census never saw it."""
    changed = T0 + timedelta(minutes=10)
    entries = _entries(
        *(
            _line(SCANNER_SKIP, at=T0 + timedelta(minutes=2 * m), level=logging.INFO,
                  logger="issue_orchestrator.control.pr_scanner")
            for m in range(6)
        ),
        _line(LAUNCH_DROP, at=T0 + timedelta(minutes=11), level=logging.INFO,
              logger="issue_orchestrator.control.session_review_support"),
        *(
            _line(SCANNER_SKIP, at=T0 + timedelta(minutes=2 * m), level=logging.INFO,
                  logger="issue_orchestrator.control.pr_scanner")
            for m in range(6, 12)
        ),
    )

    census = census_log(
        entries, window_start=T0 - timedelta(hours=1), window_end=END,
        # The PR's needs-code-review went back on at T0+10min.
        last_state_change={"PR #379": changed},
    )

    assert census.signatures == ()
    [refused] = census.refusals
    assert (refused.subject, refused.related, refused.reason) == ("PR #379", ("#364",), "issue_blocked")
    assert refused.loggers == (
        "issue_orchestrator.control.pr_scanner", "issue_orchestrator.control.session_review_support",
    )
    assert (refused.count, refused.since_state_change) == (13, 7)
    assert datetime.fromisoformat(refused.first_seen) == T0
    assert datetime.fromisoformat(refused.last_seen) == T0 + timedelta(minutes=22)


def test_a_change_of_the_subject_a_refusal_names_restarts_its_count() -> None:
    """The issue whose block refuses the review changing state may end it."""
    entries = _entries(
        *(_line(SCANNER_SKIP, at=T0 + timedelta(minutes=m), level=logging.INFO) for m in range(6)),
    )

    census = census_log(
        entries, window_start=T0 - timedelta(hours=1), window_end=END,
        last_state_change={"#364": T0 + timedelta(minutes=3, seconds=30)},
    )

    assert census.refusals[0].since_state_change == 2


def test_without_the_timeline_no_refusal_count_since_a_change_is_claimed() -> None:
    entries = _entries(_line(SCANNER_SKIP, level=logging.INFO))

    census = census_log(entries, window_start=T0 - timedelta(hours=1), window_end=END, last_state_change=None)

    assert census.refusals[0].since_state_change is None
