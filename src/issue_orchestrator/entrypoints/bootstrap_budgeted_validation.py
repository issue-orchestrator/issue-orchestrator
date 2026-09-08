"""Composition of budgeted validation's runtime and isolated execution worker."""

from datetime import datetime, timezone
from collections.abc import Callable
import os
from pathlib import Path
import time

from ..control.budgeted_validation import BudgetedValidationCycle
from ..control.budgeted_validation_reporting import BudgetedValidationReportOwner
from ..control.budgeted_validation_scheduler import BudgetedValidationScheduler
from ..domain.budgeted_validation import BudgetedValidationSuite
from ..execution.budgeted_validation_storage import open_budgeted_validation_storage
from ..execution.command_runner import LocalCommandRunner
from ..execution.budgeted_validation_executor import BudgetedValidationCommandExecutor
from ..execution.budgeted_validation_worker import BudgetedValidationWorkerProcess
from ..execution.lane_backends import resolve_contained_validation_runner
from ..infra.config import Config
from ..ports.budgeted_validation import (
    BudgetedValidationReports,
    BudgetedValidationRepository,
    BudgetedValidationRuntime,
    BudgetedValidationStore,
    DisabledBudgetedValidation,
    DisabledBudgetedValidationReports,
)
from ..ports.budgeted_validation_checkout import BudgetedValidationCheckouts
from ..ports.command_runner import CommandRunner
from ..ports.repository_host import RepositoryHost


def build_budgeted_validation_runtime(
    root: Path, suites: tuple[BudgetedValidationSuite, ...], directory: Path,
    *, has_recoverable_work: Callable[[], bool],
) -> BudgetedValidationRuntime:
    worker = BudgetedValidationWorkerProcess(repo_root=root, directory=directory, suites=suites)
    # Observation throttling is cheap infrastructure work, not the live-test
    # cadence. Every run decision is still made from each suite's YAML values.
    return BudgetedValidationScheduler(
        worker, clock=time.monotonic, check_interval_seconds=60,
        should_start=lambda: any(suite.enabled for suite in suites) or has_recoverable_work(),
    )


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


def build_budgeted_validation_cycle(
    root: Path,
) -> tuple[BudgetedValidationCycle, BudgetedValidationStore]:
    """Compose the standalone worker and CLI around one repository owner."""
    storage = open_budgeted_validation_storage(root, LocalCommandRunner())
    return (
        assemble_budgeted_validation_cycle(
            storage.repository,
            storage.checkouts,
            storage.store,
            storage.directory,
        ),
        storage.store,
    )


def build_budgeted_validation_services(
    config: Config,
    command_runner: CommandRunner,
    repository: RepositoryHost,
) -> tuple[BudgetedValidationRuntime, BudgetedValidationReports]:
    """Share one durable reporting owner with observation and application."""
    suites = tuple(config.validation.budgeted.values())
    # A disabled feature must not add a Git requirement to embedding/test
    # compositions. A real checkout is still inspected when configuration is
    # empty because its common directory may retain a removed suite's work.
    if not suites and not (config.repo_root / ".git").exists():
        return DisabledBudgetedValidation(), DisabledBudgetedValidationReports()
    storage = open_budgeted_validation_storage(config.repo_root, command_runner)
    return (
        build_budgeted_validation_runtime(
            config.repo_root,
            suites,
            storage.directory,
            has_recoverable_work=lambda: bool(storage.store.pending()),
        ),
        BudgetedValidationReportOwner(
            suites=suites,
            store=storage.store,
            repository=repository,
            clock=lambda: datetime.now(timezone.utc),
        ),
    )
