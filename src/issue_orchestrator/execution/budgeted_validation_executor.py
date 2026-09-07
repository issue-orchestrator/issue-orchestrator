"""Execute a budgeted suite in a fresh exact-commit checkout."""

import hashlib
import json
import os
from pathlib import Path

from ..domain.budgeted_validation import (
    BudgetedValidationOutcome, BudgetedValidationProbe, BudgetedValidationSuite,
    command_verdict, reconcile_report_verdict,
)
from ..ports.command_runner import CommandRunner
from ..ports.budgeted_validation_checkout import BudgetedValidationCheckouts


class BudgetedValidationCommandExecutor:
    def __init__(self, *, checkouts: BudgetedValidationCheckouts, runner: CommandRunner,
                 directory: Path, environment: dict[str, str]) -> None:
        self._checkouts = checkouts
        self._runner = runner
        self._directory = directory
        self._environment = environment

    def probe(self, suite: BudgetedValidationSuite, commit: str, run_id: str) -> BudgetedValidationProbe:
        evidence = self._directory / "runs" / suite.name / run_id
        evidence.mkdir(parents=True)
        workspace = evidence / "worktree"
        self._checkouts.create_checkout(commit, workspace)
        try:
            return self._execute(suite, commit, workspace, evidence)
        finally:
            # Only this call's fresh checkout is removable; never an existing
            # worktree. Logs and machine-readable results remain in evidence.
            self._checkouts.remove_checkout(workspace)

    def _execute(self, suite: BudgetedValidationSuite, commit: str, workspace: Path,
                 evidence: Path) -> BudgetedValidationProbe:
        environment = dict(self._environment)
        environment["PATH"] = f"{workspace / '.venv/bin'}{os.pathsep}{environment.get('PATH', '')}"
        result_path = evidence / "result.json"
        environment["IO_BUDGETED_VALIDATION_RESULT"] = str(result_path)
        if suite.setup_command:
            setup = self._runner.run(list(suite.setup_command), cwd=workspace, env=environment,
                                     timeout_seconds=suite.setup_timeout_seconds)
            (evidence / "setup.log").write_text(setup.stdout + setup.stderr)
            if setup.returncode or setup.timed_out:
                return BudgetedValidationProbe(commit, BudgetedValidationOutcome.UNAVAILABLE, str(evidence))
        result = self._runner.run(list(suite.command), cwd=workspace, env=environment,
                                  timeout_seconds=suite.timeout_seconds)
        (evidence / "tests.log").write_text(result.stdout + result.stderr)
        outcome = command_verdict(result.returncode, result.timed_out)
        signature = ""
        if result_path.exists() and not result.timed_out:
            report = json.loads(result_path.read_text())
            declared = BudgetedValidationOutcome(report["status"])
            failed = report["failed"]
            if not isinstance(failed, list) or not all(isinstance(item, str) for item in failed):
                raise ValueError("Live verdict failure identities must be strings")
            outcome = reconcile_report_verdict(result.returncode, declared, tuple(failed))
            if failed and outcome.is_failure:
                signature = hashlib.sha256(json.dumps(sorted(failed)).encode()).hexdigest()
        return BudgetedValidationProbe(commit, outcome, str(evidence), signature)
