"""Provider unavailability and contradictory reports cannot establish coverage."""

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from issue_orchestrator.domain.budgeted_validation import BudgetedValidationOutcome
from issue_orchestrator.execution.budgeted_validation_executor import BudgetedValidationCommandExecutor
from issue_orchestrator.infra.budgeted_validation_config import parse_budgeted_validation
from issue_orchestrator.ports.budgeted_validation_checkout import BudgetedValidationCheckouts
from issue_orchestrator.ports.command_runner import CommandResult, CommandRunner


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
    runner = MagicMock(spec=CommandRunner)
    checkouts = MagicMock(spec=BudgetedValidationCheckouts)
    def command(*args, **kwargs):
        Path(kwargs["env"]["IO_BUDGETED_VALIDATION_RESULT"]).write_text(json.dumps(report))
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
