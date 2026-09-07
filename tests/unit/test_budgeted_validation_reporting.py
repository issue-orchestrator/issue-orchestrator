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
        BudgetedValidationProbe("bad", BudgetedValidationOutcome.FAILED, "/evidence/run-1", "regression"), "scheduled")
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


def test_a_green_run_between_plan_and_apply_cancels_stale_reporting(tmp_path):
    suite, store, host, owner = reporting(tmp_path)
    notice = owner.pending()[0]
    good = BudgetedValidationRun("run-2", NOW, NOW,
        BudgetedValidationProbe("fixed", BudgetedValidationOutcome.PASSED, "/evidence/run-2"), "scheduled")
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
