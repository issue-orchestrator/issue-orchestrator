"""Durable process-family ownership beneath budgeted live validation."""

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .command_runner import CommandResult


class ContainedValidationPending(RuntimeError):
    """The scheduler may still own this operation; retry observation later."""


@dataclass(frozen=True, slots=True)
class ContainedValidationCommand:
    operation_id: str
    arguments: tuple[str, ...]
    working_directory: Path
    evidence_directory: Path
    environment: dict[str, str]
    timeout_seconds: int


class ContainedValidationRunner(Protocol):
    def reserved(self, evidence_directory: Path) -> bool:
        """Whether submission may already have happened for this evidence directory."""
        ...

    def run(self, command: ContainedValidationCommand) -> CommandResult:
        """Return only after the scheduler proves the whole job family is gone."""
        ...
