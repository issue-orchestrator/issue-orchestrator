"""Behavior boundaries for IO-owned cost-bounded test execution."""

from collections.abc import Callable
from typing import Protocol

from ..domain.budgeted_validation import (
    BudgetedValidationHistory, BudgetedValidationProbe, BudgetedValidationSuite,
    BudgetedValidationNotice, BudgetedValidationReportReceipt,
    PendingBudgetedValidation, StoredBudgetedValidation,
)


class BudgetedValidationJournal(Protocol):
    def inventory(self) -> tuple[StoredBudgetedValidation, ...]:
        """Return exact durable suite histories in this repository namespace."""
        ...

    def pending(self) -> tuple[PendingBudgetedValidation, ...]:
        """Return every durable unfinished run with its original suite definition."""
        ...

    def read_report(self, case_id: str) -> BudgetedValidationReportReceipt:
        ...

    def write_report(self, case_id: str, receipt: BudgetedValidationReportReceipt) -> None:
        ...

    def read(self, suite: BudgetedValidationSuite) -> BudgetedValidationHistory:
        """Read suite history; reject reuse under a changed test definition."""
        ...

    def write(self, suite: BudgetedValidationSuite, history: BudgetedValidationHistory) -> None:
        """Durably replace this suite's history under the caller's exclusive lease."""
        ...


class BudgetedValidationStore(Protocol):
    def pending(self) -> tuple[PendingBudgetedValidation, ...]:
        """Return unfinished durable runs that must survive reconfiguration."""
        ...

    def inventory(self) -> tuple[StoredBudgetedValidation, ...]:
        """Return exact durable suite histories, including removed configuration."""
        ...

    def read_report(self, case_id: str) -> BudgetedValidationReportReceipt:
        ...

    def run_exclusive(self, operation: Callable[[BudgetedValidationJournal], None]) -> bool:
        """Run while exclusively owning this repository's budget; false if busy."""
        ...

    def read(self, suite: BudgetedValidationSuite) -> BudgetedValidationHistory:
        ...


class BudgetedValidationRepository(Protocol):
    def head(self, branch: str) -> str:
        """Refresh and return the remote branch SHA, never a PR worktree HEAD."""
        ...

    def changes(self, good: str, bad: str) -> tuple[str, ...]:
        """Ordered first-parent integration commits after good through bad; fail on rewrite."""
        ...

    def merged_count(self, commits: tuple[str, ...]) -> int:
        """Count merged PRs, treating untagged integrations conservatively."""
        ...


class BudgetedValidationExecutor(Protocol):
    def probe(self, suite: BudgetedValidationSuite, commit: str, run_id: str) -> BudgetedValidationProbe:
        """Test exactly commit in an owned isolated checkout with bounded processes."""
        ...

    def resume(self, suite: BudgetedValidationSuite, commit: str, run_id: str) -> BudgetedValidationProbe:
        """Resume an interrupted durable probe without submitting it twice."""
        ...


class BudgetedValidationRuntime(Protocol):
    def tick(self) -> None:
        """Schedule due observation/execution without blocking the engine tick."""
        ...


class DisabledBudgetedValidation:
    """Explicit composition when no budgeted suites are configured."""

    def tick(self) -> None:
        return


class BudgetedValidationReports(Protocol):
    def pending(self) -> tuple["BudgetedValidationNotice", ...]:
        ...

    def publish(self, notice: "BudgetedValidationNotice") -> int:
        """Apply a planned report, returning its existing/new regression issue."""
        ...


class DisabledBudgetedValidationReports:
    def pending(self) -> tuple["BudgetedValidationNotice", ...]:
        return ()

    def publish(self, notice: "BudgetedValidationNotice") -> int:
        raise RuntimeError("Budgeted validation reporting is not configured")
