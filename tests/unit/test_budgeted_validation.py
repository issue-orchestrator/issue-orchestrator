"""Configured cadence and regression-narrowing behavior, without model calls."""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from issue_orchestrator.domain.budgeted_validation import (
    BisectRange, BudgetedValidationOutcome, ValidationCadence,
)
from issue_orchestrator.infra.config import Config
from issue_orchestrator.infra.budgeted_validation_config import parse_budgeted_validation

NOW = datetime(2026, 9, 7, 12, tzinfo=timezone.utc)


def test_either_configured_threshold_makes_changed_code_due():
    cadence = ValidationCadence(max_merges_since_success=3, max_delay_hours=6)
    assert not cadence.due(now=NOW, last_success_at=NOW - timedelta(hours=5), merges_since_success=2, changed=True)
    assert cadence.due(now=NOW, last_success_at=NOW - timedelta(hours=5), merges_since_success=3, changed=True)
    assert cadence.due(now=NOW, last_success_at=NOW - timedelta(hours=6), merges_since_success=2, changed=True)
    assert not cadence.due(now=NOW, last_success_at=NOW - timedelta(days=2), merges_since_success=0, changed=False)
    assert cadence.due(now=NOW, last_success_at=None, merges_since_success=0, changed=False)


@pytest.mark.parametrize("suspects", [1, 2, 3, 4, 10, 16, 17, 100])
def test_bisection_finds_every_possible_first_failure_with_derived_bound(suspects):
    commits = tuple(str(index) for index in range(suspects + 1))
    for culprit in range(1, suspects + 1):
        state = BisectRange(commits)
        budget = state.remaining_runs
        runs = 0
        while state.midpoint is not None:
            midpoint = state.midpoint
            outcome = BudgetedValidationOutcome.FAILED if int(midpoint) >= culprit else BudgetedValidationOutcome.PASSED
            state = state.observe(midpoint, outcome)
            runs += 1
        assert state.first_bad == str(culprit)
        assert runs <= budget
        assert state.remaining_runs == 0


@pytest.mark.parametrize("outcome", [BudgetedValidationOutcome.UNAVAILABLE, BudgetedValidationOutcome.INCONCLUSIVE])
def test_unavailable_or_inconsistent_results_do_not_accuse_a_commit(outcome):
    state = BisectRange(("green", "middle", "bad"))
    assert state.observe("middle", outcome) == state
    assert state.first_bad is None


def test_bisection_rejects_results_from_the_wrong_checkout():
    with pytest.raises(ValueError, match="requested midpoint"):
        BisectRange(("green", "middle", "bad")).observe("bad", BudgetedValidationOutcome.FAILED)


def test_yaml_cadence_and_suite_commands_survive_config_round_trip(tmp_path: Path):
    path = tmp_path / ".issue-orchestrator/config/modes/default/default.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(yaml.safe_dump({
        "repo": {"name": "owner/repo"},
        "validation": {"budgeted": {
            "live-agents": {
                "command": [".venv/bin/python", "-m", "pytest", "-m", "live_agent"],
                "setup_command": ["make", "venv-fast"],
                "cadence": {"max_merges_since_success": 7, "max_delay_hours": 12},
            },
            "long-simulation": {"command": ["make", "long-simulation"], "cadence": {"max_merges_since_success": 2}},
        }},
    }))
    config = Config.load(path)
    first = config.validation.budgeted["live-agents"]
    assert first.cadence == ValidationCadence(7, 12)
    assert config.validation.budgeted["long-simulation"].cadence == ValidationCadence(2, 24)
    assert config.to_event_dict()["validation"]["budgeted"]["live-agents"]["cadence"]["max_delay_hours"] == 12
    config.save(path)
    assert Config.load(path).validation.budgeted == config.validation.budgeted
    assert "max_bisect_runs" not in path.read_text()


@pytest.mark.parametrize("cadence", [
    {"max_merges_since_success": 0}, {"max_delay_hours": -1},
    {"max_delay_hours": True}, {"max_merges_since_success": "10"},
    {"max_delay_hours": 1.5}, {"max_bisect_runs": 4},
])
def test_bad_cadence_configuration_fails_instead_of_silently_using_defaults(cadence):
    with pytest.raises(ValueError):
        parse_budgeted_validation({"agents": {"command": ["test"], "cadence": cadence}})


@pytest.mark.parametrize("data", [None, [], {"../escape": {"command": ["test"]}}, {"agents": {"command": []}}, {"agents": {"command": "make test"}}, {"agents": {"command": ["test"], "unexpected": 1}}])
def test_invalid_suite_configuration_is_rejected(data):
    with pytest.raises(ValueError):
        parse_budgeted_validation(data)


class MemoryBudgetedStore:
    def __init__(self):
        from issue_orchestrator.domain.budgeted_validation import BudgetedValidationHistory
        self.history = BudgetedValidationHistory("suite-v1")
        self.busy = False

    def read(self, suite):
        return self.history

    def pending(self):
        from issue_orchestrator.domain.budgeted_validation import PendingBudgetedValidation
        latest = self.history.latest
        if latest is None or latest.finished_at is not None:
            return ()
        return (PendingBudgetedValidation(latest.suite, self.history),)

    def write(self, suite, history):
        self.history = history

    def run_exclusive(self, operation):
        if self.busy:
            return False
        self.busy = True
        try:
            operation(self)
        finally:
            self.busy = False
        return True


class IntegrationHistory:
    def __init__(self):
        self.current = 0

    def head(self, branch):
        return str(self.current)

    def changes(self, good, bad):
        return tuple(str(index) for index in range(int(good) + 1, int(bad) + 1))

    def merged_count(self, commits):
        return len(commits)


class RecordedProbe:
    def __init__(self):
        self.calls = []
        self.overrides = {}

    def probe(self, suite, commit, run_id):
        from issue_orchestrator.domain.budgeted_validation import BudgetedValidationProbe
        self.calls.append(commit)
        outcome = self.overrides.get(commit, BudgetedValidationOutcome.FAILED if int(commit) >= 6 else BudgetedValidationOutcome.PASSED)
        return BudgetedValidationProbe(commit, outcome, f"evidence/{run_id}", "test-regression" if outcome is BudgetedValidationOutcome.FAILED else "")

    def resume(self, suite, commit, run_id):
        return self.probe(suite, commit, run_id)


def test_cycle_uses_one_green_baseline_and_bisects_once_then_bounds_red_retries():
    from issue_orchestrator.control.budgeted_validation import BudgetedValidationCycle
    suite = parse_budgeted_validation({"agents": {"command": ["test"]}})["agents"]
    store, repo, executor = MemoryBudgetedStore(), IntegrationHistory(), RecordedProbe()
    cycle = BudgetedValidationCycle(store=store, repository=repo, executor=executor, clock=lambda: NOW)
    assert cycle.run((suite,))
    assert executor.calls == ["0"]
    repo.current = 9
    cycle.run((suite,))
    assert executor.calls == ["0"]
    repo.current = 10
    cycle.run((suite,))
    assert store.history.first_bad_commit == "6"
    assert store.history.last_success.probe.commit == "0"
    assert store.history.last_scheduled.probe.commit == "10"
    # One scheduled test, two reproducibility/environment checks, <=4 midpoints.
    assert len(executor.calls) <= 1 + 1 + 2 + 4
    calls = list(executor.calls)
    cycle.run((suite,))
    repo.current = 11
    cycle.run((suite,))
    assert executor.calls == calls


def test_cycle_does_not_bisect_a_quota_failure_or_reset_success():
    from issue_orchestrator.control.budgeted_validation import BudgetedValidationCycle
    suite = parse_budgeted_validation({"agents": {"command": ["test"]}})["agents"]
    store, repo, executor = MemoryBudgetedStore(), IntegrationHistory(), RecordedProbe()
    cycle = BudgetedValidationCycle(store=store, repository=repo, executor=executor, clock=lambda: NOW)
    cycle.run((suite,))
    repo.current = 10
    executor.overrides["10"] = BudgetedValidationOutcome.UNAVAILABLE
    cycle.run((suite,))
    cycle.run((suite,))
    assert executor.calls == ["0", "10"]
    assert store.history.last_success.probe.commit == "0"
    assert store.history.first_bad_commit is None


def test_interrupted_contained_probe_resumes_without_duplicate_submission():
    from issue_orchestrator.control.budgeted_validation import BudgetedValidationCycle
    from issue_orchestrator.domain.budgeted_validation import BudgetedValidationProbe
    from issue_orchestrator.ports.contained_validation import ContainedValidationPending

    class RestartableProbe:
        def __init__(self):
            self.submissions = 0
            self.resumptions = 0

        def probe(self, suite, commit, run_id):
            self.submissions += 1
            raise ContainedValidationPending("scheduler owns it")

        def resume(self, suite, commit, run_id):
            self.resumptions += 1
            return BudgetedValidationProbe(
                commit, BudgetedValidationOutcome.PASSED, f"evidence/{run_id}",
            )

    suite = parse_budgeted_validation({"agents": {"command": ["test"]}})["agents"]
    store, repo, executor = MemoryBudgetedStore(), IntegrationHistory(), RestartableProbe()
    cycle = BudgetedValidationCycle(
        store=store, repository=repo, executor=executor, clock=lambda: NOW,
    )
    cycle.run((suite,))
    assert store.history.latest.finished_at is None
    cycle.run((suite,))
    assert executor.submissions == 1
    assert executor.resumptions == 1
    assert store.history.last_success.probe.commit == "0"


@pytest.mark.parametrize("configured_after_restart", [False, True])
def test_pending_run_owns_its_original_definition_across_config_changes(
    tmp_path, configured_after_restart,
):
    from dataclasses import replace
    from issue_orchestrator.adapters.budgeted_validation_store import FileBudgetedValidationStore
    from issue_orchestrator.control.budgeted_validation import BudgetedValidationCycle
    from issue_orchestrator.domain.budgeted_validation import BudgetedValidationProbe
    from issue_orchestrator.ports.contained_validation import ContainedValidationPending

    class Restartable:
        def __init__(self):
            self.probes = []
            self.resumes = []

        def probe(self, suite, commit, run_id):
            self.probes.append(suite)
            if len(self.probes) == 1:
                raise ContainedValidationPending("scheduler owns original definition")
            return BudgetedValidationProbe(commit, BudgetedValidationOutcome.PASSED, run_id)

        def resume(self, suite, commit, run_id):
            self.resumes.append(suite)
            return BudgetedValidationProbe(commit, BudgetedValidationOutcome.PASSED, run_id)

    original = parse_budgeted_validation({"agents": {
        "command": ["old-test"], "timeout_seconds": 60, "branch": "main",
    }})["agents"]
    changed = replace(
        original, command=("new-test",), timeout_seconds=61, branch="release",
    )
    store, repo, executor = FileBudgetedValidationStore(tmp_path), IntegrationHistory(), Restartable()
    cycle = BudgetedValidationCycle(store=store, repository=repo, executor=executor, clock=lambda: NOW)
    cycle.run((original,))

    configured = (changed,) if configured_after_restart else ()
    cycle.run(configured)
    assert executor.resumes == [original]
    assert executor.probes == [original]

    if configured_after_restart:
        cycle.run(configured)
        assert executor.probes == [original, changed]


@pytest.mark.parametrize("interrupted_call", [3, 4, 5])
def test_restart_continues_each_diagnostic_stage_without_replaying_completed_steps(
    interrupted_call,
):
    from issue_orchestrator.control.budgeted_validation import BudgetedValidationCycle
    from issue_orchestrator.domain.budgeted_validation import BudgetedValidationProbe
    from issue_orchestrator.ports.contained_validation import ContainedValidationPending

    class InterruptingDiagnosis:
        def __init__(self):
            self.calls = []
            self.resumes = []

        @staticmethod
        def result(commit, run_id):
            outcome = (
                BudgetedValidationOutcome.FAILED
                if int(commit) >= 6 else BudgetedValidationOutcome.PASSED
            )
            return BudgetedValidationProbe(
                commit, outcome, f"evidence/{run_id}",
                "test-regression" if outcome is BudgetedValidationOutcome.FAILED else "",
            )

        def probe(self, suite, commit, run_id):
            self.calls.append((commit, run_id))
            if len(self.calls) == interrupted_call:
                raise ContainedValidationPending("diagnostic job still owned")
            return self.result(commit, run_id)

        def resume(self, suite, commit, run_id):
            self.resumes.append((commit, run_id))
            return self.result(commit, run_id)

    suite = parse_budgeted_validation({"agents": {"command": ["test"]}})["agents"]
    store, repo, executor = MemoryBudgetedStore(), IntegrationHistory(), InterruptingDiagnosis()
    cycle = BudgetedValidationCycle(store=store, repository=repo, executor=executor, clock=lambda: NOW)
    cycle.run((suite,))
    repo.current = 10
    cycle.run((suite,))
    assert store.history.latest.finished_at is None
    pending_id = store.history.latest.id
    calls_before_restart = tuple(executor.calls)

    cycle.run((suite,))

    assert executor.resumes == [(calls_before_restart[-1][0], pending_id)]
    assert executor.calls[:len(calls_before_restart)] == list(calls_before_restart)
    assert sum(run.id == pending_id for run in store.history.runs) == 1
    assert store.history.first_bad_commit == "6"


def test_busy_repository_coalesces_without_even_fetching_or_spending():
    from issue_orchestrator.control.budgeted_validation import BudgetedValidationCycle
    suite = parse_budgeted_validation({"agents": {"command": ["test"]}})["agents"]
    store, repo, executor = MemoryBudgetedStore(), IntegrationHistory(), RecordedProbe()
    store.busy = True
    cycle = BudgetedValidationCycle(store=store, repository=repo, executor=executor, clock=lambda: NOW)
    assert not cycle.run((suite,))
    assert not executor.calls


def test_a_new_scheduled_run_clears_old_diagnosis_and_restores_coverage_only_on_success():
    from issue_orchestrator.control.budgeted_validation import BudgetedValidationCycle
    suite = parse_budgeted_validation({"agents": {"command": ["test"]}})["agents"]
    store, repo, executor = MemoryBudgetedStore(), IntegrationHistory(), RecordedProbe()
    cycle = BudgetedValidationCycle(store=store, repository=repo, executor=executor, clock=lambda: NOW)
    cycle.run((suite,))
    repo.current = 10
    cycle.run((suite,))
    assert store.history.first_bad_commit == "6"
    assert store.history.coverage_outcome is BudgetedValidationOutcome.FAILED
    executor.overrides["10"] = BudgetedValidationOutcome.PASSED
    cycle.run((suite,), force=True)
    assert store.history.first_bad_commit is None
    assert store.history.diagnosis == ""
    assert store.history.coverage_outcome is BudgetedValidationOutcome.PASSED
    assert store.history.last_success.probe.commit == "10"


def test_runtime_observes_periodically_and_never_starts_a_second_local_worker():
    from unittest.mock import MagicMock
    from issue_orchestrator.control.budgeted_validation_scheduler import BudgetedValidationScheduler
    from issue_orchestrator.ports.budgeted_validation_worker import BudgetedValidationWorker
    worker = MagicMock(spec=BudgetedValidationWorker)
    worker.running.return_value = False
    clock = [0.0]
    scheduler = BudgetedValidationScheduler(worker, clock=lambda: clock[0], check_interval_seconds=60)
    scheduler.tick()
    scheduler.tick()
    clock[0] = 60
    worker.running.return_value = True
    scheduler.tick()
    worker.start.assert_called_once()
    worker.running.return_value = False
    scheduler.tick()
    assert worker.start.call_count == 2


def test_lightweight_validation_config_loader_preserves_named_suite_parameters():
    from issue_orchestrator.infra.validation_config_loader import extract_validation_config
    budgeted = {"agents": {"command": ["test"], "cadence": {"max_merges_since_success": 4, "max_delay_hours": 8}}}
    extracted = extract_validation_config({"validation": {"budgeted": budgeted}})
    assert parse_budgeted_validation(extracted["budgeted"])["agents"].cadence == ValidationCadence(4, 8)


def test_unrelated_settings_save_preserves_yaml_managed_suites(tmp_path):
    from issue_orchestrator.infra.config_document_patch import save_config_document_patch
    from issue_orchestrator.infra.settings_schema import apply_to, build_save_plan, from_config
    path = tmp_path / "main.yaml"
    budgeted = {"agents": {"command": ["test"], "cadence": {"max_merges_since_success": 4, "max_delay_hours": 8}}}
    path.write_text(yaml.safe_dump({"repo": {"name": "owner/repo"}, "validation": {"budgeted": budgeted}}))
    config = Config.load(path)
    before = from_config(config)
    after = from_config(config)
    after["concurrency"].max_concurrent_sessions = 7
    apply_to(after, config)
    save_config_document_patch(config, build_save_plan(before, after).entries)
    assert yaml.safe_load(path.read_text())["validation"]["budgeted"] == budgeted
    assert Config.load(path).validation.budgeted == config.validation.budgeted


@pytest.mark.parametrize(("paused", "shutdown", "starts"), [(False, False, 1), (True, False, 0), (False, True, 0)])
def test_engine_tick_respects_pause_and_shutdown_at_the_runtime_port(sample_orchestrator, paused, shutdown, starts):
    from dataclasses import replace
    from unittest.mock import MagicMock
    from issue_orchestrator.ports.budgeted_validation import BudgetedValidationRuntime
    from tests.conftest import operator_paused_state
    runtime = MagicMock(spec=BudgetedValidationRuntime)
    engine = sample_orchestrator
    engine.deps = replace(engine.deps, services=replace(engine.deps.services, budgeted_validation=runtime))
    if paused:
        engine.state.pause_state = operator_paused_state()
    engine.shutdown_requested = shutdown
    engine.tick()
    assert runtime.tick.call_count == starts


def test_forced_failure_on_the_green_commit_does_not_invent_a_bisect_range():
    from issue_orchestrator.control.budgeted_validation import BudgetedValidationCycle
    suite = parse_budgeted_validation({"agents": {"command": ["test"]}})["agents"]
    store, repo, executor = MemoryBudgetedStore(), IntegrationHistory(), RecordedProbe()
    cycle = BudgetedValidationCycle(store=store, repository=repo, executor=executor, clock=lambda: NOW)
    cycle.run((suite,))
    executor.overrides["0"] = BudgetedValidationOutcome.FAILED
    cycle.run((suite,), force=True)
    assert executor.calls == ["0", "0"]
    assert store.history.first_bad_commit is None
    assert "no new integration" in store.history.diagnosis


@pytest.mark.parametrize("outcome", [BudgetedValidationOutcome.UNAVAILABLE, BudgetedValidationOutcome.INCONCLUSIVE])
def test_unavailable_attempt_retries_at_either_configured_bound(outcome):
    from issue_orchestrator.control.budgeted_validation import BudgetedValidationCycle
    suite = parse_budgeted_validation({"agents": {"command": ["test"]}})["agents"]
    store, repo, executor = MemoryBudgetedStore(), IntegrationHistory(), RecordedProbe()
    cycle = BudgetedValidationCycle(store=store, repository=repo, executor=executor, clock=lambda: NOW)
    cycle.run((suite,))
    repo.current = 10
    executor.overrides["10"] = outcome
    cycle.run((suite,))
    repo.current = 19
    cycle.run((suite,))
    assert executor.calls == ["0", "10"]
    repo.current = 20
    executor.overrides["20"] = BudgetedValidationOutcome.UNAVAILABLE
    cycle.run((suite,))
    assert executor.calls == ["0", "10", "20"]


def test_diagnostic_probe_does_not_shift_scheduled_retry_watermark():
    from datetime import timedelta
    from issue_orchestrator.domain.budgeted_validation import BudgetedValidationHistory, BudgetedValidationRun, BudgetedValidationProbe
    suite = parse_budgeted_validation({"agents": {"command": ["test"]}})["agents"]
    history = BudgetedValidationHistory("suite")
    good = BudgetedValidationRun("good", NOW, NOW,
        BudgetedValidationProbe("0", BudgetedValidationOutcome.PASSED, "evidence/good"), "scheduled", suite)
    bad = BudgetedValidationRun("bad", NOW, NOW,
        BudgetedValidationProbe("10", BudgetedValidationOutcome.FAILED, "evidence/bad", "failure"), "scheduled", suite)
    pending = BudgetedValidationRun("diagnosis", NOW + timedelta(hours=1), None,
        BudgetedValidationProbe("10", BudgetedValidationOutcome.UNAVAILABLE, ""), "reproduce", suite)
    history = history.append(good).append(bad).append(pending)
    assert history.scheduled_due(now=NOW + timedelta(hours=24), cadence=ValidationCadence(),
        head="10", integrations_since_attempt=0)
    assert not history.scheduled_due(now=NOW + timedelta(hours=23), cadence=ValidationCadence(),
        head="10", integrations_since_attempt=9)
    assert history.scheduled_due(now=NOW + timedelta(hours=23), cadence=ValidationCadence(),
        head="20", integrations_since_attempt=10)
