"""Execute a budgeted suite through one durably contained scheduler job."""

import hashlib
import json
import os
from pathlib import Path

from ..domain.budgeted_validation import (
    BudgetedValidationOutcome, BudgetedValidationProbe, BudgetedValidationSuite,
    command_verdict, reconcile_report_verdict,
)
from ..ports.budgeted_validation_checkout import BudgetedValidationCheckouts
from ..ports.contained_validation import (
    ContainedValidationCommand, ContainedValidationPending, ContainedValidationRunner,
)


class BudgetedValidationCommandExecutor:
    """Own checkout, evidence and one contained setup-plus-test operation."""

    def __init__(self, *, checkouts: BudgetedValidationCheckouts,
                 runner: ContainedValidationRunner, directory: Path,
                 environment: dict[str, str]) -> None:
        self._checkouts = checkouts
        self._runner = runner
        self._directory = directory
        self._environment = environment

    def probe(self, suite: BudgetedValidationSuite, commit: str,
              run_id: str) -> BudgetedValidationProbe:
        return self._execute_owned(suite, commit, run_id)

    def resume(self, suite: BudgetedValidationSuite, commit: str,
               run_id: str) -> BudgetedValidationProbe:
        return self._execute_owned(suite, commit, run_id)

    def _execute_owned(self, suite: BudgetedValidationSuite, commit: str,
                       run_id: str) -> BudgetedValidationProbe:
        evidence = self._directory / "runs" / suite.name / run_id
        evidence.mkdir(parents=True, exist_ok=True)
        workspace = evidence / "worktree"
        reserved = self._runner.reserved(evidence)
        checkout_owned = workspace.exists()
        if workspace.exists() and not reserved:
            # A prior worker died before recording submission intent. No job
            # can own this checkout, so it is safe to rebuild from the exact
            # requested commit. A reservation is the authority to keep it.
            self._checkouts.remove_checkout(workspace)
        if not workspace.exists() and not reserved:
            self._checkouts.create_checkout(commit, workspace)
            checkout_owned = True
        script = evidence / "validation.py"
        if not reserved:
            script.write_text(_validation_script(suite, evidence), encoding="utf-8")
            script.chmod(0o755)
        environment = dict(self._environment)
        environment["PATH"] = f"{workspace / '.venv/bin'}{os.pathsep}{environment.get('PATH', '')}"
        result_path = evidence / "result.json"
        environment["IO_BUDGETED_VALIDATION_RESULT"] = str(result_path)
        command = ContainedValidationCommand(
            operation_id=run_id,
            # Keep reservation identity stable across engine/venv upgrades.
            # PATH selects the checkout's interpreter when the job first runs;
            # an existing scheduler reservation is only observed, never rebound.
            arguments=("/usr/bin/env", "python3", str(script)),
            working_directory=workspace,
            evidence_directory=evidence,
            environment=environment,
            timeout_seconds=suite.setup_timeout_seconds + suite.timeout_seconds + 30,
        )
        try:
            result = self._runner.run(command)
        except ContainedValidationPending:
            # The reservation and checkout stay together until the scheduler
            # proves the process family is empty. The next worker resumes it.
            raise
        if checkout_owned:
            self._checkouts.remove_checkout(workspace)
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


def _validation_script(suite: BudgetedValidationSuite, evidence: Path) -> str:
    """Compile setup and tests into one scheduler-owned process family."""
    return (
        "import pathlib, subprocess, sys\n"
        "def run(command, log, timeout):\n"
        "    with pathlib.Path(log).open('wb') as output:\n"
        "        try:\n"
        "            return subprocess.run(command, stdout=output, stderr=subprocess.STDOUT, "
        "timeout=timeout, check=False).returncode\n"
        "        except subprocess.TimeoutExpired:\n"
        "            return 75\n"
        f"setup = {suite.setup_command!r}\n"
        f"status = run(setup, {str(evidence / 'setup.log')!r}, {suite.setup_timeout_seconds}) if setup else 0\n"
        "if status != 0:\n"
        "    raise SystemExit(75)\n"
        f"status = run({suite.command!r}, {str(evidence / 'tests.log')!r}, {suite.timeout_seconds})\n"
        "raise SystemExit(75 if status in (124, 137) else status)\n"
    )
