"""Exact configured and retained coverage projection for command consumers."""

from dataclasses import dataclass

from ..domain.budgeted_validation import (
    BudgetedValidationHistory,
    BudgetedValidationOutcome,
    BudgetedValidationSuite,
)
from ..ports.budgeted_validation import BudgetedValidationStore


@dataclass(frozen=True, slots=True)
class BudgetedValidationCoverageEntry:
    suite: BudgetedValidationSuite
    history: BudgetedValidationHistory
    counts_for_verdict: bool


@dataclass(frozen=True, slots=True)
class BudgetedValidationCoverageSnapshot:
    entries: tuple[BudgetedValidationCoverageEntry, ...]

    @property
    def outcomes(self) -> frozenset[BudgetedValidationOutcome]:
        return frozenset(
            entry.history.coverage_outcome
            for entry in self.entries
            if entry.counts_for_verdict
        )


class BudgetedValidationCoverageOwner:
    """Select exact current definitions without losing retained recovery work."""

    def __init__(self, store: BudgetedValidationStore) -> None:
        self._store = store

    def snapshot(
        self,
        configured: tuple[BudgetedValidationSuite, ...],
        *,
        retained_name: str | None = None,
    ) -> BudgetedValidationCoverageSnapshot:
        retained = {
            (item.suite.name, item.history.suite_identity): item
            for item in self._store.inventory()
            if retained_name is None or item.suite.name == retained_name
        }
        current: list[BudgetedValidationCoverageEntry] = []
        current_keys: set[tuple[str, str]] = set()
        for suite in configured:
            history = self._store.read(suite)
            current_keys.add((suite.name, history.suite_identity))
            current.append(BudgetedValidationCoverageEntry(
                suite,
                history,
                suite.enabled or history.recovery_pending,
            ))
        include_retained_verdicts = not configured
        historical = tuple(
            BudgetedValidationCoverageEntry(
                item.suite,
                item.history,
                include_retained_verdicts
                and (item.suite.enabled or item.history.recovery_pending),
            )
            for key, item in retained.items()
            if key not in current_keys
        )
        return BudgetedValidationCoverageSnapshot((*current, *historical))
