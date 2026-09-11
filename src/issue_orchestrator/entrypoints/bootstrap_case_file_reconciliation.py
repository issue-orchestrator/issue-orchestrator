"""Compose the bounded reconciliation command using normal lifecycle owners."""

from typing import TYPE_CHECKING

from ..control.tech_lead_case_file_reconciliation import CaseFileReconciliationHost
from ..control.tech_lead_case_file_lifecycle_reconciliation import (
    CaseFileLifecycleReconciler,
)
from ..execution.case_file_reconciliation_adapter import CaseFileReconciliationAdapter

if TYPE_CHECKING:
    from ..infra.config import Config
    from ..infra.orchestrator import Orchestrator


def build_case_file_reconciliation_host(
    orchestrator: "Orchestrator",
) -> CaseFileReconciliationHost:
    """Keep repository/ledger/applier wiring inside the composition root."""
    deps = orchestrator.deps
    return CaseFileReconciliationAdapter(
        list_pattern_evidence=deps.services.tech_lead_authority.list_pattern_evidence,
        get_issue_state=deps.repository_host.get_issue_state,
        apply_all=deps.action_applier.apply_all,
    )


def build_case_file_lifecycle_reconciler(
    config: "Config",
    *,
    publish_local_seed: bool,
) -> CaseFileLifecycleReconciler:
    """Compose durable lifecycle authority without starting background services."""
    from ..control.label_manager import LabelManager
    from ..control.mutation_gate import ReconciliationGate
    from ..control.reconciliation import ExpectedState
    from ..execution.providers import (
        create_fresh_issue_reader,
        create_repository_host,
    )
    from .bootstrap_tech_lead import (
        create_pattern_registry,
        create_tech_lead_authority_store,
    )

    if config.repo is None:
        raise ValueError("the configured repository could not be resolved")
    repository_host = create_repository_host(config.repo, config)
    registry = create_pattern_registry(
        config,
        repository_host,
        create_tech_lead_authority_store(config),
        shared_required=True,
        publish_local_seed=publish_local_seed,
    )
    labels = LabelManager(config)
    gate = ReconciliationGate(
        fresh_issue_reader=create_fresh_issue_reader(config.repo, config),
        reconcile=True,
    )
    expected = ExpectedState.with_labels(forbidden={labels.needs_reconcile})
    return CaseFileLifecycleReconciler(
        registry=registry,
        repository_host=repository_host,
        require_mutation_authority=lambda issue: gate.require_state(expected, issue),
    )
