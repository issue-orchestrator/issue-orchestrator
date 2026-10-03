"""The engine's Tech lead page facade (#7763): one section read, one command.

Both run under the reentrant state lock, like tech-lead admission and launch:
the dashboard thread and the engine tick must not interleave consent or
execution, and the page reads the in-memory state the tick writes. Policy stays
in the owners — the approval owner for Approve/Decline, the page projection
for presentation; this module only gathers what the engine already holds.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from typing import TYPE_CHECKING, cast


from ..control.health_review_trigger import health_review_interval_minutes
from ..domain.scoped_rework import (
    TechLeadProposalCommand,
    TechLeadProposalCommandOutcome,
)
from ..view_models.tech_lead_activity import read_tech_lead_activity
from ..control.scoped_rework_receipt_status import rework_receipt_status
from ..view_models.tech_lead_page import (
    MERGE_HOLD_CAUSES,
    ReworkReceiptView,
    TechLeadPageInputs,
    build_tech_lead_page_section,
)

if TYPE_CHECKING:
    from ..contracts.ui_openapi_models import TechLeadPageSectionPayload
    from ..ports.engine_audit import NeedsHumanCauseAuditReader
    from ..ports.issue import Issue
    from .orchestrator import Orchestrator

#: Charter decisions read per page: the doing lane looks back one day, and the
#: triage lane keeps each item's latest class.
PAGE_DECISION_LIMIT = 300


def tech_lead_page_section(orchestrator: Orchestrator) -> "TechLeadPageSectionPayload":
    """This engine's section of the cross-repo Tech lead page.

    Everything comes from engine-held state under the state lock, except the
    merge-held PRs' mergeability and checks, which are read AFTER the lock is
    released (never holding up a tick) through a cache that bounds them to a
    few reads every few minutes (:mod:`~..control.merge_hold_status`).
    """
    with orchestrator.state_lock:
        deps, state, config = orchestrator.deps, orchestrator.state, orchestrator.config
        authority = deps.services.tech_lead_authority
        approvals = deps.action_applier.tech_lead_approvals
        if approvals is not None and not approvals.scope_observed:
            # An unobserved scope is unknown, not empty: publishing "nothing
            # waiting" here would be an all-clear nobody checked (#7763 r10 F1).
            raise TechLeadPageNotObserved("the approval scope has not been observed yet")
        labels = deps.label_manager
        issues: dict[int, "Issue"] = {}
        for issue in (*state.cached_scope_issues, *state.cached_queue_issues):
            issues.setdefault(issue.number, issue)
        # The sqlite claim store is also the needs-human cause audit reader.
        causes = cast("NeedsHumanCauseAuditReader", deps.pending_work_claims)
        board = deps.fact_gatherer.board_publisher
        runs = read_tech_lead_activity(orchestrator.tech_lead_run_history, limit=1).entries
        inputs = TechLeadPageInputs(
            repository=config.repo or "",
            now=datetime.now(timezone.utc),
            proposals=approvals.observed_scope() if approvals is not None else (),
            ops=dict(authority.list_ops()) if authority is not None else {},
            rework_receipts=_rework_receipts(orchestrator),
            needs_human_causes=causes.list_needs_human_causes(),
            issues=tuple(issues.values()),
            tech_lead_needs_human_label=labels.tech_lead_needs_human,
            blocked_numbers=frozenset(
                number for number, issue in issues.items() if labels.is_blocking_any(issue.labels)
            ),
            decisions=(
                authority.charter_ledger.list_recent(limit=PAGE_DECISION_LIMIT)
                if authority is not None
                else ()
            ),
            parked=deps.action_liveness.owner.parked(),
            case_files=board.case_files() if board is not None else (),
            health_interval_minutes=health_review_interval_minutes(config) if config.tech_lead_enabled else 0,
            last_health_review_at=state.last_health_review_at,
            latest_run=runs[0] if runs else None,
            merge_statuses={},
            known_proposals=approvals.indexed_proposals() if approvals is not None else frozenset(),
        )
    held = [row.issue_number for row in inputs.needs_human_causes if row.cause in MERGE_HOLD_CAUSES]
    return build_tech_lead_page_section(
        replace(inputs, merge_statuses=orchestrator.deps.merge_hold_statuses.read(held))
    )


class TechLeadPageNotObserved(RuntimeError):
    """The engine has not completed an approval-scope observation yet."""


def _rework_receipts(orchestrator: Orchestrator) -> tuple[ReworkReceiptView, ...]:
    """Approved scoped rework, with its live status from the rework owner."""
    owner = orchestrator.deps.action_applier.request_rework
    if owner is None:
        return ()
    views = []
    for receipt in owner.receipts.list_rework_receipts():
        status, detail = rework_receipt_status(owner, receipt)
        views.append(
            ReworkReceiptView(
                proposal_issue_number=receipt.proposal_issue_number,
                request_key=receipt.request.key,
                issue_number=receipt.request.target.issue_number,
                status=status,
                detail=detail,
            )
        )
    return tuple(views)


def proposal_command(
    orchestrator: Orchestrator, command: TechLeadProposalCommand
) -> TechLeadProposalCommandOutcome:
    """Approve or Decline ANY tech-lead proposal (#7763), under the state lock.

    The one command path: the Control Center's Tech lead page and nothing
    else. An approval re-arms the approval scope scan so the next tick
    verifies it and executes or admits the proposal, instead of waiting out
    the scan interval.
    """
    from ..control.tech_lead_approval_writes import apply_operator_proposal_command

    with orchestrator.state_lock:
        approvals = orchestrator.deps.action_applier.tech_lead_approvals
        ops = orchestrator.deps.services.tech_lead_authority
        if approvals is None or ops is None:
            raise RuntimeError("The tech-lead approval owner is not wired")
        outcome = apply_operator_proposal_command(
            command, repository=orchestrator.deps.repository_host, ops=ops, approvals=approvals
        )
        if outcome.outcome == "approved":
            orchestrator.state.tech_lead_approval_scan_at = 0.0
        return outcome
