"""Construct the durable Git-backed boundary for budgeted validation."""

from dataclasses import dataclass
from pathlib import Path

from ..adapters.budgeted_validation_git import BudgetedValidationGit
from ..adapters.budgeted_validation_store import FileBudgetedValidationStore
from ..ports.budgeted_validation import (
    BudgetedValidationRepository,
    BudgetedValidationStore,
)
from ..ports.budgeted_validation_checkout import BudgetedValidationCheckouts
from ..ports.command_runner import CommandRunner


@dataclass(frozen=True, slots=True)
class BudgetedValidationStorage:
    repository: BudgetedValidationRepository
    checkouts: BudgetedValidationCheckouts
    store: BudgetedValidationStore
    directory: Path


def open_budgeted_validation_storage(
    root: Path, command_runner: CommandRunner
) -> BudgetedValidationStorage:
    repository = BudgetedValidationGit(root, command_runner)
    directory = repository.storage_directory()
    return BudgetedValidationStorage(
        repository=repository,
        checkouts=repository,
        store=FileBudgetedValidationStore(directory),
        directory=directory,
    )
