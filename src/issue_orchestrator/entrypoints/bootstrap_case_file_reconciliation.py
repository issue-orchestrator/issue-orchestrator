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
    from ..ports import RepositoryHost


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
    apply_writes: bool,
) -> CaseFileLifecycleReconciler:
    """Compose durable lifecycle authority without starting background services.

    ``apply_writes`` selects between two DIFFERENT compositions, not one
    composition with a suppressed write. A preview gets read-only shared
    authority and no mutation authority at all, so nothing it can reach — not
    the local SQLite authority store, not the shared registry, not GitHub — is
    writable; an apply gets the write-through mirror and the reconciliation
    gate. Suppressing a single write on a write-capable composition was not
    enough: reads through the mirror still migrated local authority and
    discarded pending intents from a command that promised to write nothing
    (#7248 review F1/A1).
    """
    from ..execution.providers import create_repository_host

    if config.repo is None:
        raise ValueError("the configured repository could not be resolved")
    repository_host = create_repository_host(config.repo, config)
    if not apply_writes:
        return _build_lifecycle_preview(config, repository_host)
    return _build_lifecycle_applier(config, repository_host)


def _build_lifecycle_preview(
    config: "Config", repository_host: "RepositoryHost"
) -> CaseFileLifecycleReconciler:
    """Read-only lifecycle authority for a dry run."""
    from ..control.tech_lead_case_file_lifecycle_reconciliation import (
        CaseFileLifecycleReconciliationRefused,
    )
    from .bootstrap_tech_lead import create_pattern_registry_preview

    def _refuse(issue: int) -> None:
        # Unreachable while the reconciler only applies when asked to, and
        # wired anyway: the preview's guarantee must not rest on that caller
        # staying correct. The registry refuses the write on its own, and this
        # refuses the authority to attempt one.
        raise CaseFileLifecycleReconciliationRefused(
            f"a lifecycle dry run may not mutate #{issue}; re-run with --apply"
        )

    return CaseFileLifecycleReconciler(
        registry=create_pattern_registry_preview(config, repository_host),
        repository_host=repository_host,
        require_mutation_authority=_refuse,
    )


def _build_lifecycle_applier(
    config: "Config", repository_host: "RepositoryHost"
) -> CaseFileLifecycleReconciler:
    """Write-capable lifecycle authority behind the reconciliation gate."""
    from ..control.label_manager import LabelManager
    from ..control.mutation_gate import ReconciliationGate
    from ..control.reconciliation import ExpectedState
    from ..execution.providers import create_fresh_issue_reader
    from .bootstrap_tech_lead import (
        create_pattern_registry,
        create_tech_lead_authority_store,
    )

    assert config.repo is not None
    registry = create_pattern_registry(
        config,
        repository_host,
        create_tech_lead_authority_store(config),
        shared_required=True,
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
