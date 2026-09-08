"""Provider unavailability and contradictory reports cannot establish coverage."""

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from issue_orchestrator.domain.budgeted_validation import BudgetedValidationOutcome
from issue_orchestrator.execution.budgeted_validation_executor import BudgetedValidationCommandExecutor
from issue_orchestrator.infra.budgeted_validation_config import parse_budgeted_validation
from issue_orchestrator.ports.budgeted_validation_checkout import BudgetedValidationCheckouts
from issue_orchestrator.ports.command_runner import CommandResult
from issue_orchestrator.ports.contained_validation import (
    ContainedValidationPending, ContainedValidationRunner,
)


@pytest.mark.parametrize(("code", "timed_out", "report", "expected"), [
    (0, False, {"status": "passed", "failed": []}, BudgetedValidationOutcome.PASSED),
    (1, False, {"status": "failed", "failed": ["test_a"]}, BudgetedValidationOutcome.FAILED),
    (75, False, {"status": "unavailable", "failed": []}, BudgetedValidationOutcome.UNAVAILABLE),
    (-9, True, {"status": "passed", "failed": []}, BudgetedValidationOutcome.UNAVAILABLE),
    (-9, True, {"status": "failed", "failed": ["test_a"]}, BudgetedValidationOutcome.UNAVAILABLE),
    (0, False, {"status": "passed", "failed": ["test_a"]}, None),
    (1, False, {"status": "passed", "failed": []}, None),
    (75, False, {"status": "failed", "failed": ["test_a"]}, None),
])
def test_executor_requires_consistent_exit_and_report_and_always_cleans_owned_checkout(tmp_path, code, timed_out, report, expected):
    suite = parse_budgeted_validation({"agents": {"command": ["test"]}})["agents"]
    runner = MagicMock(spec=ContainedValidationRunner)
    runner.reserved.return_value = False
    checkouts = MagicMock(spec=BudgetedValidationCheckouts)
    def command(request):
        Path(request.environment["IO_BUDGETED_VALIDATION_RESULT"]).write_text(json.dumps(report))
        (request.evidence_directory / "tests.log").write_text("diagnostic output")
        return CommandResult(code, "diagnostic output", "", timed_out)
    runner.run.side_effect = command
    executor = BudgetedValidationCommandExecutor(checkouts=checkouts, runner=runner, directory=tmp_path, environment={})
    if expected is None:
        with pytest.raises(ValueError, match="verdict"):
            executor.probe(suite, "commit", "run")
    else:
        probe = executor.probe(suite, "commit", "run")
        assert probe.outcome is expected
        assert bool(probe.failure_signature) == (expected is BudgetedValidationOutcome.FAILED)
    checkouts.remove_checkout.assert_called_once_with(tmp_path / "runs/agents/run/worktree")
    assert (tmp_path / "runs/agents/run/tests.log").read_text() == "diagnostic output"


def test_executor_keeps_checkout_when_scheduler_ownership_is_pending(tmp_path):
    suite = parse_budgeted_validation({"agents": {"command": ["test"]}})["agents"]
    runner = MagicMock(spec=ContainedValidationRunner)
    runner.reserved.return_value = False
    runner.run.side_effect = ContainedValidationPending("still owned")
    checkouts = MagicMock(spec=BudgetedValidationCheckouts)
    executor = BudgetedValidationCommandExecutor(
        checkouts=checkouts, runner=runner, directory=tmp_path, environment={},
    )
    with pytest.raises(ContainedValidationPending):
        executor.probe(suite, "commit", "run")
    checkouts.remove_checkout.assert_not_called()


def test_executor_does_not_recreate_a_missing_checkout_under_a_reservation(tmp_path):
    suite = parse_budgeted_validation({"agents": {"command": ["test"]}})["agents"]
    runner = MagicMock(spec=ContainedValidationRunner)
    runner.reserved.return_value = True
    runner.run.side_effect = ContainedValidationPending("still owned")
    checkouts = MagicMock(spec=BudgetedValidationCheckouts)
    executor = BudgetedValidationCommandExecutor(
        checkouts=checkouts, runner=runner, directory=tmp_path, environment={},
    )
    with pytest.raises(ContainedValidationPending):
        executor.resume(suite, "commit", "run")
    checkouts.create_checkout.assert_not_called()
    checkouts.remove_checkout.assert_not_called()


def test_reservation_command_does_not_bind_the_engine_interpreter_path(tmp_path):
    suite = parse_budgeted_validation({"agents": {"command": ["test"]}})["agents"]
    runner = MagicMock(spec=ContainedValidationRunner)
    runner.reserved.return_value = False
    runner.run.side_effect = ContainedValidationPending("retained")
    executor = BudgetedValidationCommandExecutor(
        checkouts=MagicMock(spec=BudgetedValidationCheckouts), runner=runner,
        directory=tmp_path, environment={},
    )
    with pytest.raises(ContainedValidationPending):
        executor.probe(suite, "commit", "run")
    command = runner.run.call_args.args[0]
    assert command.arguments[:2] == ("/usr/bin/env", "python3")
    assert command.arguments[2].endswith("/runs/agents/run/validation.py")
