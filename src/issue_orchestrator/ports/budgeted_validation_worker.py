"""Nonblocking launch boundary for a budgeted validation worker."""

from typing import Protocol


class BudgetedValidationWorker(Protocol):
    def running(self) -> bool:
        ...

    def start(self) -> None:
        """Start a bounded worker; it owns the repository lease until completion."""
        ...
