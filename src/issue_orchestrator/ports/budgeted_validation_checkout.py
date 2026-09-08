"""Owned checkout boundary used by budgeted test execution."""

from pathlib import Path
from typing import Protocol


class BudgetedValidationCheckouts(Protocol):
    def create_checkout(self, commit: str, path: Path) -> None:
        """Create a new isolated checkout pinned to commit; never reuse a path."""
        ...

    def remove_checkout(self, path: Path) -> None:
        """Remove the checkout this caller allocated after its processes stopped."""
        ...
