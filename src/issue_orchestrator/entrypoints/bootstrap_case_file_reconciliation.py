"""Compose the bounded reconciliation command using normal lifecycle owners."""

from typing import TYPE_CHECKING

from ..control.tech_lead_case_file_reconciliation import CaseFileReconciliationHost
from ..execution.case_file_reconciliation_adapter import CaseFileReconciliationAdapter

if TYPE_CHECKING:
    from ..infra.orchestrator import Orchestrator


def build_case_file_reconciliation_host(orchestrator: "Orchestrator") -> CaseFileReconciliationHost:
    """Keep repository/ledger/applier wiring inside the composition root."""
    deps = orchestrator.deps
    return CaseFileReconciliationAdapter(
        list_pattern_evidence=deps.services.tech_lead_authority.list_pattern_evidence,
        get_issue_state=deps.repository_host.get_issue_state,
        apply_all=deps.action_applier.apply_all,
    )
