"""The improver's trigger: due on engine activity, never on io merges (#7567)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from issue_orchestrator.adapters.budgeted_validation_store import FileBudgetedValidationStore
from issue_orchestrator.control.budgeted_validation import (
    NOT_BISECTED_DIAGNOSIS,
    BudgetedValidationCycle,
)
from issue_orchestrator.domain.budgeted_validation import (
    BudgetedValidationOutcome,
    ValidationCadence,
)
from issue_orchestrator.domain.engine_activity import (
    EngineActivity,
    EngineActivityCadence,
    EngineActivityObservation,
)
from issue_orchestrator.infra.budgeted_validation_config import parse_budgeted_validation
from tests.unit.test_budgeted_validation import IntegrationHistory, MemoryBudgetedStore, RecordedProbe

NOW = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)
PORCHPIN = "repo-porchpin"


def _suite():
    return parse_budgeted_validation({
        "tech-lead-improver": {
            "command": ["make", "tech-lead-improver"],
            "cadence": {"kind": "engine_activity", "max_delay_hours": 24},
        },
    })["tech-lead-improver"]


class Engines:
    """The engines' activity as the probe would read it, and every look taken."""

    def __init__(self) -> None:
        self.decisions = 5
        self.completions = 2
        self.anomalies: tuple[str, ...] = ("parked_action|#320|publish:fp1",)
        self.engines_running = True
        self.looks: list[datetime] = []
        self.since: list[datetime] = []

    def observe(self, *, now: datetime, since: datetime) -> EngineActivityObservation:
        self.looks.append(now)
        self.since.append(since)
        engines = (
            (EngineActivity(PORCHPIN, "porchpin/porchpin", self.decisions, self.completions, self.anomalies),)
            if self.engines_running else ()
        )
        return EngineActivityObservation(observed_at=now, engines=engines)


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


def _cycle(store, repo, executor, engines, clock) -> BudgetedValidationCycle:
    return BudgetedValidationCycle(store=store, repository=repo, executor=executor, clock=clock, activity=engines)


@pytest.fixture
def world():
    store, repo, executor, engines, clock = (
        MemoryBudgetedStore(), IntegrationHistory(), RecordedProbe(), Engines(), Clock(),
    )
    cycle = _cycle(store, repo, executor, engines, clock)
    suite = _suite()
    cycle.run((suite,))  # the first run: the engine has decisions, completions and an anomaly
    assert executor.calls == ["0"]
    return store, repo, executor, engines, clock, cycle, suite


def test_the_improver_suite_parses_as_an_engine_activity_cadence() -> None:
    suite = _suite()

    assert suite.cadence == EngineActivityCadence(max_delay_hours=24)
    # Every other suite keeps the code-change cadence.
    other = parse_budgeted_validation({"exam": {"command": ["make", "exam"]}})["exam"]
    assert other.cadence == ValidationCadence()


def test_the_worker_request_round_trips_the_engine_activity_cadence() -> None:
    from issue_orchestrator.infra.budgeted_validation_config import serialize_budgeted_validation

    suite = _suite()

    assert parse_budgeted_validation(serialize_budgeted_validation({suite.name: suite}))[suite.name] == suite


def test_not_due_without_engine_activity_even_after_many_io_merges(world) -> None:
    store, repo, executor, engines, clock, cycle, suite = world
    repo.current = 500
    for hours in (25, 26, 48, 100):
        clock.now = NOW + timedelta(hours=hours)
        cycle.run((suite,))

    assert executor.calls == ["0"]
    assert store.history.last_activity_probe is not None


def test_no_engine_running_is_never_due() -> None:
    store, repo, executor, engines, clock = (
        MemoryBudgetedStore(), IntegrationHistory(), RecordedProbe(), Engines(), Clock(),
    )
    engines.engines_running = False
    repo.current = 50

    _cycle(store, repo, executor, engines, clock).run((_suite(),))

    assert executor.calls == []


@pytest.mark.parametrize("activity", ["decision", "completion", "anomaly"])
def test_due_after_engine_activity_with_zero_io_merges(world, activity: str) -> None:
    store, repo, executor, engines, clock, cycle, suite = world
    if activity == "decision":
        engines.decisions += 1
    elif activity == "completion":
        engines.completions += 1
    else:
        engines.anomalies = tuple(sorted((*engines.anomalies, "no_progress_log|#500|sig")))
    clock.now = NOW + timedelta(hours=24)

    cycle.run((suite,))

    assert repo.current == 0
    assert executor.calls == ["0", "0"]
    started = store.history.last_scheduled
    assert started.activity is not None and started.activity.engine(PORCHPIN).decisions == engines.decisions


def test_at_most_once_per_window_however_active(world) -> None:
    store, repo, executor, engines, clock, cycle, suite = world
    looks = len(engines.looks)
    for hours in (1, 6, 23):
        engines.decisions += 10
        clock.now = NOW + timedelta(hours=hours)
        cycle.run((suite,))

    assert executor.calls == ["0"]
    # Inside the window the engines are not even looked at.
    assert len(engines.looks) == looks
    clock.now = NOW + timedelta(hours=24)
    cycle.run((suite,))
    assert executor.calls == ["0", "0"]


def test_an_idle_open_window_reprobes_only_per_probe_interval(world) -> None:
    store, repo, executor, engines, clock, cycle, suite = world
    for minutes in (0, 1, 30, 59):
        clock.now = NOW + timedelta(hours=24, minutes=minutes)
        cycle.run((suite,))
    assert len(engines.looks) == 2  # the first run's, then one at +24h

    clock.now = NOW + timedelta(hours=25)
    cycle.run((suite,))
    assert len(engines.looks) == 3
    assert executor.calls == ["0"]


def test_activity_is_measured_from_the_last_successful_run(world) -> None:
    """A failed (or unavailable) run does not consume the activity it started for."""
    store, repo, executor, engines, clock, cycle, suite = world
    engines.decisions += 1
    executor.overrides["0"] = BudgetedValidationOutcome.UNAVAILABLE
    clock.now = NOW + timedelta(hours=24)
    cycle.run((suite,))
    assert executor.calls == ["0", "0"]

    del executor.overrides["0"]
    clock.now = NOW + timedelta(hours=48)
    cycle.run((suite,))

    assert executor.calls == ["0", "0", "0"]


def test_a_failed_improver_run_is_never_bisected_over_io_commits(world) -> None:
    store, repo, executor, engines, clock, cycle, suite = world
    repo.current = 30
    engines.decisions += 1
    executor.overrides["30"] = BudgetedValidationOutcome.FAILED
    clock.now = NOW + timedelta(hours=24)

    cycle.run((suite,))

    assert executor.calls == ["0", "30"]
    assert store.history.diagnosis == NOT_BISECTED_DIAGNOSIS
    assert store.history.first_bad_commit is None
    assert not store.history.recovery_pending


def test_the_per_engine_watermark_survives_the_store(tmp_path: Path) -> None:
    store, repo, executor, engines, clock = (
        FileBudgetedValidationStore(tmp_path), IntegrationHistory(), RecordedProbe(), Engines(), Clock(),
    )
    suite = _suite()
    _cycle(store, repo, executor, engines, clock).run((suite,))

    history = FileBudgetedValidationStore(tmp_path).read(suite)

    assert history.latest is not None and history.latest.suite.cadence == EngineActivityCadence(24)
    watermark = history.activity_baseline
    assert watermark is not None
    assert watermark.engine(PORCHPIN) == EngineActivity(
        PORCHPIN, "porchpin/porchpin", 5, 2, ("parked_action|#320|publish:fp1",)
    )
    assert history.last_activity_probe == watermark
    # A restarted cycle reads it back and is not due without new activity.
    clock.now = NOW + timedelta(hours=30)
    _cycle(FileBudgetedValidationStore(tmp_path), repo, executor, engines, clock).run((suite,))
    assert executor.calls == ["0"]


def test_an_engine_that_acted_after_the_last_success_then_stopped_is_still_observed(world) -> None:
    """r2 F1: success at T0, a decision at T0+1m, then the engine stops; the
    next look at T0+24h+2m must reach back to T0, not just 24 hours."""
    store, repo, executor, engines, clock, cycle, suite = world
    clock.now = NOW + timedelta(hours=24, minutes=2)

    cycle.run((suite,))

    assert engines.since[-1] == store.history.last_success.activity.observed_at == NOW


def test_with_no_successful_run_engines_are_observed_over_the_window() -> None:
    cadence = EngineActivityCadence(24)

    assert cadence.observe_since(now=NOW, baseline=None) == NOW - timedelta(hours=24)


def test_a_ledger_that_shrank_or_went_unread_is_not_activity() -> None:
    before = EngineActivity(PORCHPIN, "p/p", 5, 2, ("a|b|c",))

    assert EngineActivity(PORCHPIN, "p/p", 4, None, ("a|b|c",)).new_since(before) == ()
    assert EngineActivity(PORCHPIN, "p/p", 5, 2, ()).new_since(before) == ()
    assert EngineActivity(PORCHPIN, "p/p", 6, 2, ("a|b|c",)).new_since(before) == ("1 new decisions",)
    # A new engine counts from zero.
    assert EngineActivity("repo-new", "n/n", 0, 0, ()).new_since(None) == ()
    assert EngineActivity("repo-new", "n/n", 1, 0, ()).new_since(None) == ("1 new decisions",)
