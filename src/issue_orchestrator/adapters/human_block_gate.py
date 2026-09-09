"""Serialize the shared human-block owner across handles, threads and processes."""

from contextlib import AbstractContextManager
from pathlib import Path

from ..domain.issue_disposition_gate import IssueDispositionGateStatus
from .issue_disposition_gate import hold_mutation_file


class FileHumanBlockMutationGate:
    def __init__(self, cause_database: Path) -> None:
        database = cause_database.resolve()
        self._directory = database.parent / f"{database.name}.human-block-gates"

    def try_acquire(
        self, issue_number: int
    ) -> AbstractContextManager[IssueDispositionGateStatus]:
        if type(issue_number) is not int or issue_number <= 0:
            raise ValueError("human-block target must be positive")
        return hold_mutation_file(self._directory / f"{issue_number}.lock")
