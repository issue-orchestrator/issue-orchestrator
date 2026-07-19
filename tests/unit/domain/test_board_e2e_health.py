"""Pure-projection tests for BoardE2EHealth.

``BoardE2EHealth.project`` classifies raw e2e-health facts (from the read-only
reader) against an injected ``now`` — no db, no io, no ``datetime.now()``. These
tests pin the neglect signals a health review keys on: off-cadence (``stale``),
red streaks (``nonpassing_streak``), chronic failures with/without tracking, and
the defensive edges (no runs, unparseable timestamp, truncation).
"""

import logging
from datetime import datetime, timezone

from issue_orchestrator.domain.board_snapshot import (
    MAX_CHRONIC_E2E_FAILURES,
    RECENT_E2E_RUN_WINDOW,
    BoardE2EHealth,
    E2EChronicFailureFact,
    E2ERunHealthFact,
)

NOW = datetime(2026, 7, 18, 12, 0, 0, tzinfo=timezone.utc)


def _run(
    run_id: int,
    status: str,
    *,
    minutes_ago: int = 0,
    duration: float | None = 10.0,
    passed: int = 1,
    failed: int = 0,
    started_at: str | None = None,
) -> E2ERunHealthFact:
    if started_at is None:
        ts = datetime.fromtimestamp(NOW.timestamp() - minutes_ago * 60, tz=timezone.utc)
        started_at = ts.isoformat()
    return E2ERunHealthFact(
        id=run_id,
        status=status,
        started_at=started_at,
        duration_seconds=duration,
        passed_count=passed,
        failed_count=failed,
    )


def _project(
    *,
    enabled: bool = True,
    interval: int = 240,
    runs: tuple[E2ERunHealthFact, ...] = (),
    chronic: tuple[E2EChronicFailureFact, ...] = (),
    quarantine_count: int = 0,
    now: datetime = NOW,
) -> BoardE2EHealth:
    return BoardE2EHealth.project(
        now=now,
        enabled=enabled,
        expected_interval_minutes=interval,
        runs=runs,
        chronic_failures=chronic,
        quarantine_count=quarantine_count,
    )


class TestLastRunAndRecency:
    def test_last_run_is_newest_and_recent_runs_preserve_order(self) -> None:
        runs = (
            _run(115, "warning", minutes_ago=60),
            _run(114, "failed", minutes_ago=120, duration=None, passed=9, failed=1),
        )

        health = _project(runs=runs)

        assert health.last_run is not None
        assert health.last_run.id == 115
        assert health.last_run.age_minutes == 60
        assert [r.id for r in health.recent_runs] == [115, 114]
        # None duration is preserved verbatim (not coerced).
        assert health.recent_runs[1].duration_seconds is None
        assert (health.recent_runs[1].passed_count, health.recent_runs[1].failed_count) == (9, 1)

    def test_recent_runs_bounded_by_window(self) -> None:
        runs = tuple(_run(200 + i, "failed", minutes_ago=i) for i in range(RECENT_E2E_RUN_WINDOW + 5))

        health = _project(runs=runs)

        assert len(health.recent_runs) == RECENT_E2E_RUN_WINDOW

    def test_no_runs_yields_no_last_run(self) -> None:
        health = _project(runs=())

        assert health.last_run is None
        assert health.recent_runs == ()
        assert health.nonpassing_streak == 0


class TestAgeIsTimezoneRobust:
    def test_aware_started_at_with_naive_now_does_not_raise(self) -> None:
        """Prod clock is naive-local; db timestamps are UTC-aware. The delta
        must not raise a naive/aware TypeError."""
        runs = (_run(1, "failed", started_at="2026-07-18T00:00:00+00:00"),)

        health = _project(runs=runs, now=datetime(2026, 7, 18, 6, 0, 0))

        assert isinstance(health.last_run.age_minutes, int)

    def test_unparseable_started_at_yields_unknown_age(self) -> None:
        runs = (_run(1, "failed", started_at="not-a-timestamp"),)

        health = _project(runs=runs)

        assert health.last_run.age_minutes == -1


class TestStale:
    def test_stale_when_last_run_older_than_multiplier(self) -> None:
        # interval 240, multiplier 3 -> threshold 720 min. 800 > 720.
        health = _project(interval=240, runs=(_run(1, "failed", minutes_ago=800),))

        assert health.stale is True

    def test_not_stale_within_multiplier_window(self) -> None:
        health = _project(interval=240, runs=(_run(1, "passed", minutes_ago=700),))

        assert health.stale is False

    def test_stale_when_enabled_and_never_run(self) -> None:
        health = _project(enabled=True, runs=())

        assert health.stale is True

    def test_not_stale_when_disabled(self) -> None:
        health = _project(enabled=False, runs=(_run(1, "failed", minutes_ago=99999),))

        assert health.stale is False
        assert health.enabled is False

    def test_not_stale_when_interval_disabled(self) -> None:
        """interval <= 0 means auto-run is off: no cadence to miss."""
        health = _project(interval=0, runs=(_run(1, "failed", minutes_ago=99999),))

        assert health.stale is False

    def test_stale_when_last_run_timestamp_unparseable(self) -> None:
        health = _project(runs=(_run(1, "failed", started_at="garbage"),))

        assert health.stale is True


class TestNonpassingStreak:
    def test_counts_consecutive_nonpassed_until_a_pass(self) -> None:
        runs = (
            _run(5, "warning", minutes_ago=1),
            _run(4, "failed", minutes_ago=2),
            _run(3, "error", minutes_ago=3),
            _run(2, "passed", minutes_ago=4),
            _run(1, "failed", minutes_ago=5),
        )

        assert _project(runs=runs).nonpassing_streak == 3

    def test_inflight_runs_are_skipped_not_counted_or_breaking(self) -> None:
        runs = (
            _run(4, "running", minutes_ago=1),
            _run(3, "canceled", minutes_ago=2),
            _run(2, "failed", minutes_ago=3),
            _run(1, "passed", minutes_ago=4),
        )

        # running + canceled skipped; one failed before the pass.
        assert _project(runs=runs).nonpassing_streak == 1

    def test_all_nonpassing_streak_equals_window(self) -> None:
        runs = tuple(_run(i, "failed", minutes_ago=i) for i in range(RECENT_E2E_RUN_WINDOW))

        assert _project(runs=runs).nonpassing_streak == RECENT_E2E_RUN_WINDOW

    def test_leading_pass_gives_zero_streak(self) -> None:
        runs = (_run(2, "passed", minutes_ago=1), _run(1, "failed", minutes_ago=2))

        assert _project(runs=runs).nonpassing_streak == 0


class TestChronicFailures:
    def test_tracked_unresolved_and_untracked_both_surface_top_by_fail_count(self) -> None:
        chronic = (
            E2EChronicFailureFact("t::untracked", fail_count=3, tracking_issue=None, tracking_resolved=False),
            E2EChronicFailureFact("t::tracked", fail_count=18, tracking_issue=6822, tracking_resolved=False),
        )

        health = _project(chronic=chronic)

        # Re-sorted top-by-fail_count: the tracked 18-failure test leads.
        assert [c.nodeid for c in health.chronic_failures] == ["t::tracked", "t::untracked"]
        assert (health.chronic_failures[0].tracking_issue, health.chronic_failures[0].tracking_resolved) == (6822, False)
        assert health.chronic_failures[1].tracking_issue is None

    def test_truncation_is_logged_not_silent(self, caplog) -> None:
        chronic = tuple(
            E2EChronicFailureFact(f"t::n{i}", fail_count=100 - i, tracking_issue=None, tracking_resolved=False)
            for i in range(MAX_CHRONIC_E2E_FAILURES + 4)
        )

        with caplog.at_level(logging.WARNING):
            health = _project(chronic=chronic)

        assert len(health.chronic_failures) == MAX_CHRONIC_E2E_FAILURES
        assert any("chronic-failure list truncated" in rec.message for rec in caplog.records)
        # Highest fail_count kept (top-by-fail_count ordering).
        assert health.chronic_failures[0].fail_count == 100


class TestPassthroughFields:
    def test_enabled_interval_and_quarantine_are_carried(self) -> None:
        health = _project(enabled=True, interval=240, quarantine_count=5)

        assert (health.enabled, health.expected_interval_minutes, health.quarantine_count) == (True, 240, 5)
