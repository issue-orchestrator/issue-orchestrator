"""The ONE exhaustive open tech-lead-agent scan, classified (#6778/#6779/#7763).

Extracted from the fact gatherer, which only orchestrates fact production.
The scoped/exhaustive anchor-discovery owner backs both this path and startup
recovery, so both apply ONE eligibility rule (#6763 finding 7) over the
COMPLETE open set (#6779 R4). Gated proposal issues carry the tech lead agent
label, so the SAME scan that finds batch/health anchors classifies them: an
op-backed proposal whose approval the approval owner VERIFIED is approved
(#7763); every other proposal is inert and excluded from anchor
classification. A backlog of proposals can never hide an older approved op
or an anchor.

Read-only apart from the approval owner's evidence reads, which happen only
for scanned items that carry ``approved``. Ledger rows absent from the scan
are returned as terminal-cleanup CANDIDATES for the planner's
confirm-and-discard action, never mutated here (#6779 R10).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .health_review_trigger import (
    classify_tech_lead_anchor_issues,
    discover_open_tech_lead_anchor_issues,
)
from .tech_lead_case_files import split_tech_lead_case_file_issues
from .tech_lead_proposals import reconcile_tech_lead_proposals

if TYPE_CHECKING:
    from ..domain.tech_lead_approval import ApprovalVerdict
    from ..domain.tech_lead_session import (
        ApprovedTechLeadOp,
        StoredTechLeadOp,
        TechLeadCaseFileSummary,
    )
    from ..infra.config import Config
    from ..ports import RepositoryHost
    from ..ports.issue import Issue
    from .tech_lead_approval import TechLeadApprovals


@dataclass(frozen=True)
class TechLeadAnchorScan:
    """What one exhaustive tech-lead-agent scan observed."""

    batch_anchor: int | None
    health_anchor: int | None
    approved_ops: tuple["ApprovedTechLeadOp", ...]
    absent_op_candidates: tuple[int, ...]
    case_files: tuple["TechLeadCaseFileSummary", ...]
    issues: tuple["Issue", ...]
    verdicts: Mapping[int, "ApprovalVerdict"]


def classify_tech_lead_anchor_scan(
    repository_host: "RepositoryHost",
    config: "Config",
    *,
    ops: Mapping[int, "StoredTechLeadOp"],
    approvals: "TechLeadApprovals | None",
    pending_markers: tuple[str, ...] = (),
) -> TechLeadAnchorScan:
    """Run and classify the exhaustive scan.

    ``ops`` is the caller-provided local authority-store ledger (the caller
    already read it to decide whether a scan is worthwhile — #6779 R12). With
    no approval owner wired nothing can be verified, so no op is approved.
    """
    existing = discover_open_tech_lead_anchor_issues(repository_host, config)
    verdicts = approvals.verify_claims(existing) if approvals is not None else {}
    reconciled = reconcile_tech_lead_proposals(
        existing, ops=ops, verdicts=verdicts, pending_markers=pending_markers,
        known=approvals.known_proposals() if approvals is not None else frozenset(),
    )
    remaining, case_files = split_tech_lead_case_file_issues(
        reconciled.anchor_candidate_issues
    )
    batch, health = classify_tech_lead_anchor_issues(
        remaining, config.filtering.label
    )
    return TechLeadAnchorScan(
        batch_anchor=batch,
        health_anchor=health,
        approved_ops=reconciled.approved,
        absent_op_candidates=reconciled.absent_op_issue_numbers,
        case_files=tuple(case_files),
        # UNFILTERED: reconciliation drops proposals from anchor candidates only.
        issues=tuple(existing),
        verdicts=verdicts,
    )


__all__ = ["TechLeadAnchorScan", "classify_tech_lead_anchor_scan"]
