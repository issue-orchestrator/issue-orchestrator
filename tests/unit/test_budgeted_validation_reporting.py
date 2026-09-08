"""Regression issue ownership and the observation/planning/application boundary."""

from dataclasses import replace
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from issue_orchestrator.adapters.budgeted_validation_store import FileBudgetedValidationStore
from issue_orchestrator.control.action_applier import ActionApplier
from issue_orchestrator.control.budgeted_validation_reporting import BudgetedValidationReportOwner, ReportBudgetedValidationAction
from issue_orchestrator.control.fact_gatherer import FactGatherer
from issue_orchestrator.control.planner import Planner
from issue_orchestrator.control.scheduler import Scheduler
from issue_orchestrator.domain.budgeted_validation import BudgetedValidationOutcome, BudgetedValidationProbe, BudgetedValidationRun
from issue_orchestrator.domain.models import OrchestratorState
from issue_orchestrator.infra.budgeted_validation_config import parse_budgeted_validation
from issue_orchestrator.infra.config import Config
from issue_orchestrator.ports.repository_host import RepositoryHost

NOW = datetime(2026, 9, 7, 12, tzinfo=timezone.utc)


def reporting(tmp_path):
    suite = parse_budgeted_validation({"agents": {"command": ["test"]}})["agents"]
    store = FileBudgetedValidationStore(tmp_path)
    bad = BudgetedValidationRun("run-1", NOW, NOW,
        BudgetedValidationProbe("bad", BudgetedValidationOutcome.FAILED, "/evidence/run-1", "regression"), "scheduled", suite)
    store.run_exclusive(lambda journal: journal.write(suite, replace(journal.read(suite).append(bad), diagnosis="Missing baseline")))
    host = MagicMock(spec=RepositoryHost)
    host.find_issue_by_marker.return_value = None
    host.create_issue.return_value = {"number": 42}
    owner = BudgetedValidationReportOwner(suites=(suite,), store=store, repository=host, clock=lambda: NOW)
    return suite, store, host, owner


def test_failure_is_observed_planned_and_applied_once(tmp_path):
    suite, store, host, owner = reporting(tmp_path)
    config = Config()
    gatherer = FactGatherer(config, host, budgeted_validation_reports=owner)
    snapshot = gatherer.create_snapshot(OrchestratorState(), [])
    assert len(snapshot.budgeted_validation_notices) == 1
    plan = Planner(config, Scheduler(config)).plan(snapshot)
    reports = [action for action in plan.actions if isinstance(action, ReportBudgetedValidationAction)]
    assert len(reports) == 1
    host.create_issue.assert_not_called()
    applier = ActionApplier(labels=host, sessions=MagicMock(), events=MagicMock(),
                            repository_host=host, budgeted_validation_reports=owner)
    applied = applier.apply(reports[0])
    assert applied.success, applied.error
    assert applied.details["issue_number"] == 42
    assert owner.pending() == ()
    assert owner.publish(reports[0].notice) == 42
    host.create_issue.assert_called_once()
    request = host.create_issue.call_args.kwargs
    assert request["labels"] == [suite.issue_agent_label]
    assert "tech lead owns" in request["body"]
    assert store.read_report(reports[0].notice.case_id).issue_number == 42


def test_removed_suite_keeps_its_confirmed_regression_reportable(tmp_path):
    suite, store, host, _owner = reporting(tmp_path)
    owner = BudgetedValidationReportOwner(
        suites=(), store=store, repository=host, clock=lambda: NOW,
    )
    notice, = owner.pending()
    assert notice.suite_name == suite.name
    assert owner.publish(notice) == 42
    host.create_issue.assert_called_once()
    assert store.read_report(notice.case_id).issue_number == 42


def test_ambiguous_create_never_reposts_and_recovers_a_late_marker(tmp_path):
    _, store, host, owner = reporting(tmp_path)
    notice = owner.pending()[0]
    host.create_issue.side_effect = TimeoutError("response lost")
    with pytest.raises(TimeoutError):
        owner.publish(notice)
    assert store.read_report(notice.case_id).attempted_at == NOW
    with pytest.raises(RuntimeError, match="duplicate POST"):
        owner.publish(notice)
    host.create_issue.assert_called_once()
    host.find_issue_by_marker.return_value = 42
    assert owner.publish(notice) == 42
    host.create_issue.assert_called_once()
    assert owner.pending() == ()


def test_pre_namespace_report_receipt_prevents_duplicate_issue_creation(tmp_path):
    _, store, host, owner = reporting(tmp_path)
    notice = owner.pending()[0]
    assert owner.publish(notice) == 42
    receipt, = (tmp_path / "reports").glob("*.json")
    receipt.replace(tmp_path / f"report-{receipt.name}")

    reopened = FileBudgetedValidationStore(tmp_path)
    restarted = BudgetedValidationReportOwner(
        suites=(), store=reopened, repository=host, clock=lambda: NOW,
    )
    assert restarted.pending() == ()
    assert restarted.publish(notice) == 42
    host.create_issue.assert_called_once()


def test_a_green_run_between_plan_and_apply_cancels_stale_reporting(tmp_path):
    suite, store, host, owner = reporting(tmp_path)
    notice = owner.pending()[0]
    good = BudgetedValidationRun("run-2", NOW, NOW,
        BudgetedValidationProbe("fixed", BudgetedValidationOutcome.PASSED, "/evidence/run-2"), "scheduled", suite)
    store.run_exclusive(lambda journal: journal.write(suite, journal.read(suite).append(good)))
    with pytest.raises(ValueError, match="stale"):
        owner.publish(notice)
    host.create_issue.assert_not_called()
    assert not owner.pending()


@pytest.mark.parametrize("change", [{"case_id": "forged"}, {"failed_commit": "other"}, {"evidence": "unverified"}])
def test_reporting_owner_revalidates_the_entire_notice(tmp_path, change):
    _, _, host, owner = reporting(tmp_path)
    with pytest.raises(ValueError, match="stale"):
        owner.publish(replace(owner.pending()[0], **change))
    host.create_issue.assert_not_called()


def test_confirmed_failure_survives_interrupted_diagnosis_and_unavailable_retries(tmp_path):
    from datetime import timedelta
    suite, store, host, owner = reporting(tmp_path)
    original = owner.pending()[0]
    pending = BudgetedValidationRun("interrupted", NOW, None,
        BudgetedValidationProbe("bad", BudgetedValidationOutcome.UNAVAILABLE, ""), "reproduce", suite)
    store.run_exclusive(lambda journal: journal.write(suite, journal.read(suite).append(pending)))
    assert owner.pending() == (original,)
    # More than the bounded diagnostic history: the failure has its own durable
    # lifecycle and cannot disappear when old run details roll out of that list.
    for number in range(110):
        at = NOW + timedelta(hours=25 * (number + 1))
        unavailable = BudgetedValidationRun(f"retry-{number}", at, at,
            BudgetedValidationProbe("bad", BudgetedValidationOutcome.UNAVAILABLE, f"evidence/{number}"), "scheduled", suite)
        store.run_exclusive(lambda journal: journal.write(suite, journal.read(suite).append(unavailable)))
    reopened = FileBudgetedValidationStore(tmp_path)
    owner = BudgetedValidationReportOwner(suites=(suite,), store=reopened, repository=host, clock=lambda: NOW)
    assert owner.pending() == (original,)
    assert owner.publish(original) == 42
    host.create_issue.assert_called_once()


def test_diagnosis_in_progress_cannot_publish_without_owning_the_repository_lease(tmp_path):
    suite, store, host, owner = reporting(tmp_path)
    notice = owner.pending()[0]
    def diagnose(journal):
        with pytest.raises(RuntimeError, match="still running"):
            owner.publish(notice)
        host.create_issue.assert_not_called()
    store.run_exclusive(diagnose)
    assert owner.publish(notice) == 42


def test_v1_history_migration_recovers_failure_behind_interrupted_diagnosis(tmp_path):
    import json
    suite, store, host, _owner = reporting(tmp_path)
    pending = BudgetedValidationRun("interrupted", NOW, None,
        BudgetedValidationProbe("bad", BudgetedValidationOutcome.UNAVAILABLE, ""), "reproduce", suite)
    store.run_exclusive(lambda journal: journal.write(suite, journal.read(suite).append(pending)))
    path, = (tmp_path / "histories").glob("agents-*.json")
    old = json.loads(path.read_text())
    old["version"] = 1
    old.pop("regression")
    path.write_text(json.dumps(old))
    reopened = FileBudgetedValidationStore(tmp_path)
    owner = BudgetedValidationReportOwner(suites=(suite,), store=reopened, repository=host, clock=lambda: NOW)
    notice, = owner.pending()
    assert notice.failed_commit == "bad"
    assert notice.run_id == "run-1"
    assert owner.publish(notice) == 42


def test_cycle_restart_reports_original_failure_after_diagnosis_launch_error(tmp_path):
    from datetime import timedelta
    from issue_orchestrator.control.budgeted_validation import BudgetedValidationCycle
    from tests.unit.test_budgeted_validation import IntegrationHistory, RecordedProbe

    class InterruptedProbe(RecordedProbe):
        def probe(self, suite, commit, run_id):
            if commit == "10" and self.calls.count("10") == 1:
                raise OSError("diagnosis launch interrupted")
            return super().probe(suite, commit, run_id)

    suite = parse_budgeted_validation({"agents": {"command": ["test"]}})["agents"]
    store = FileBudgetedValidationStore(tmp_path)
    repository, executor = IntegrationHistory(), InterruptedProbe()
    cycle = BudgetedValidationCycle(store=store, repository=repository,
                                    executor=executor, clock=lambda: NOW)
    cycle.run((suite,))
    repository.current = 10
    with pytest.raises(OSError, match="diagnosis launch interrupted"):
        cycle.run((suite,))
    interrupted = store.read(suite)
    assert interrupted.latest.finished_at is None
    original = interrupted.regression.failed
    host = MagicMock(spec=RepositoryHost)
    host.find_issue_by_marker.return_value = None
    host.create_issue.return_value = {"number": 42}
    restarted = FileBudgetedValidationStore(tmp_path)
    retry = RecordedProbe()
    retry.overrides["10"] = BudgetedValidationOutcome.UNAVAILABLE
    BudgetedValidationCycle(store=restarted, repository=repository, executor=retry,
                            clock=lambda: NOW + timedelta(hours=25)).run((suite,))
    history = restarted.read(suite)
    assert retry.calls == ["10"]
    assert history.latest.probe.outcome is BudgetedValidationOutcome.INCONCLUSIVE
    assert history.regression.failed == original
    reconciled = [run for run in history.runs if run.id == interrupted.latest.id
                  and run.finished_at is not None]
    assert len(reconciled) == 1
    assert reconciled[0].probe.outcome is BudgetedValidationOutcome.UNAVAILABLE
    owner = BudgetedValidationReportOwner(suites=(suite,), store=restarted,
                                          repository=host, clock=lambda: NOW)
    config = Config()
    snapshot = FactGatherer(config, host, budgeted_validation_reports=owner).create_snapshot(OrchestratorState(), [])
    reports = [action for action in Planner(config, Scheduler(config)).plan(snapshot).actions
               if isinstance(action, ReportBudgetedValidationAction)]
    assert len(reports) == 1
    assert reports[0].notice.failed_commit == original.probe.commit
    assert reports[0].notice.evidence == original.probe.evidence
    applier = ActionApplier(labels=host, sessions=MagicMock(), events=MagicMock(),
                            repository_host=host, budgeted_validation_reports=owner)
    assert applier.apply(reports[0]).success
    host.create_issue.assert_called_once()
    assert owner.pending() == ()
