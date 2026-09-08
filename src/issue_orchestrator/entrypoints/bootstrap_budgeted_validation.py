"""Composition of budgeted validation's runtime and isolated execution worker."""

from datetime import datetime, timezone
import os
from pathlib import Path
import time

from ..control.budgeted_validation import BudgetedValidationCycle
from ..control.budgeted_validation_scheduler import BudgetedValidationScheduler
from ..domain.budgeted_validation import BudgetedValidationSuite
from ..execution.budgeted_validation_executor import BudgetedValidationCommandExecutor
from ..execution.budgeted_validation_worker import BudgetedValidationWorkerProcess
from ..execution.lane_backends import resolve_contained_validation_runner
from ..ports.budgeted_validation import BudgetedValidationRuntime, DisabledBudgetedValidation
from ..ports.budgeted_validation import BudgetedValidationRepository, BudgetedValidationStore
from ..ports.budgeted_validation_checkout import BudgetedValidationCheckouts


def build_budgeted_validation_runtime(root: Path, suites: tuple[BudgetedValidationSuite, ...], directory: Path) -> BudgetedValidationRuntime:
    if not any(suite.enabled for suite in suites):
        return DisabledBudgetedValidation()
    worker = BudgetedValidationWorkerProcess(repo_root=root, directory=directory, suites=suites)
    # Observation throttling is cheap infrastructure work, not the live-test
    # cadence. Every run decision is still made from each suite's YAML values.
    return BudgetedValidationScheduler(worker, clock=time.monotonic, check_interval_seconds=60)


def assemble_budgeted_validation_cycle(repository: BudgetedValidationRepository, checkouts: BudgetedValidationCheckouts, store: BudgetedValidationStore, directory: Path) -> BudgetedValidationCycle:
    environment = {key: value for key, value in os.environ.items() if key not in {
        "MAKEFLAGS", "MFLAGS", "PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV",
        "ISSUE_ORCHESTRATOR_PYTHON", "PYTEST_ADDOPTS", "CLAUDECODE",
    } and not key.startswith("GIT_")}
    runner = resolve_contained_validation_runner()
    executor = BudgetedValidationCommandExecutor(checkouts=checkouts, runner=runner,
                                                directory=directory, environment=environment)
    return BudgetedValidationCycle(store=store, repository=repository, executor=executor,
                                   clock=lambda: datetime.now(timezone.utc))
