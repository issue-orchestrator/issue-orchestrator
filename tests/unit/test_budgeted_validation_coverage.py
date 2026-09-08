"""Coverage selection binds current config and retained work consistently."""

from dataclasses import replace
from datetime import datetime, timezone

from issue_orchestrator.adapters.budgeted_validation_store import (
    FileBudgetedValidationStore,
    suite_identity,
)
from issue_orchestrator.control.budgeted_validation_coverage import (
    BudgetedValidationCoverageOwner,
)
from issue_orchestrator.domain.budgeted_validation import (
    BudgetedValidationHistory,
    BudgetedValidationOutcome,
    BudgetedValidationProbe,
    BudgetedValidationRun,
)
from issue_orchestrator.infra.budgeted_validation_config import parse_budgeted_validation

NOW = datetime(2026, 9, 8, tzinfo=timezone.utc)


def _pending(suite):
    return BudgetedValidationRun(
        "pending", NOW, None,
        BudgetedValidationProbe("head", BudgetedValidationOutcome.UNAVAILABLE, ""),
        "scheduled", suite,
    )


def test_disabled_current_suite_keeps_its_original_pending_run_in_verdict(tmp_path):
    enabled = parse_budgeted_validation({"agents": {"command": ["test"]}})["agents"]
    disabled = replace(enabled, enabled=False)
    store = FileBudgetedValidationStore(tmp_path)
    store.run_exclusive(lambda journal: journal.write(
        enabled,
        BudgetedValidationHistory(suite_identity(enabled)).append(_pending(enabled)),
    ))

    snapshot = BudgetedValidationCoverageOwner(store).snapshot((disabled,))

    assert snapshot.outcomes == frozenset({BudgetedValidationOutcome.UNAVAILABLE})
    assert snapshot.entries[0].suite is disabled
    assert snapshot.entries[0].counts_for_verdict


def test_changed_current_definition_cannot_inherit_old_green_coverage(tmp_path):
    old = parse_budgeted_validation({"agents": {"command": ["old"]}})["agents"]
    current = parse_budgeted_validation({"agents": {"command": ["new"]}})["agents"]
    store = FileBudgetedValidationStore(tmp_path)
    green = replace(
        _pending(old), id="green", finished_at=NOW,
        probe=BudgetedValidationProbe("old", BudgetedValidationOutcome.PASSED, "evidence"),
    )
    store.run_exclusive(lambda journal: journal.write(
        old, BudgetedValidationHistory(suite_identity(old)).append(green),
    ))

    snapshot = BudgetedValidationCoverageOwner(store).snapshot((current,))

    assert snapshot.outcomes == frozenset({BudgetedValidationOutcome.UNAVAILABLE})
    assert [entry.suite.command for entry in snapshot.entries] == [("new",), ("old",)]
    assert [entry.counts_for_verdict for entry in snapshot.entries] == [True, False]


def test_retained_name_filter_excludes_other_suite_history(tmp_path):
    suites = parse_budgeted_validation({
        "agents": {"command": ["test"]},
        "other": {"command": ["other"]},
    })
    store = FileBudgetedValidationStore(tmp_path)
    for suite in suites.values():
        store.run_exclusive(lambda journal, suite=suite: journal.write(
            suite,
            BudgetedValidationHistory(suite_identity(suite)).append(_pending(suite)),
        ))

    snapshot = BudgetedValidationCoverageOwner(store).snapshot(
        (suites["agents"],), retained_name="agents",
    )

    assert [entry.suite.name for entry in snapshot.entries] == ["agents"]
