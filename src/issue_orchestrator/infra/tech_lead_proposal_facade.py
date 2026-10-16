"""The engine's Tech lead page facade (#7763): one section read, one command.

The command runs under the reentrant state lock, like tech-lead admission and
launch: the dashboard thread and the engine tick must not interleave consent or
execution. The section read does NOT take it (#8988): the tick captures what the
page is built from under the lock it already holds and publishes it to the
engine's :class:`~.tech_lead_page_snapshot.TechLeadPageSnapshot`, and the read
serves that, so a long planning cycle never makes the page miss its answer.
Policy stays in the owners — the approval owner for Approve/Decline, the page
projection for presentation; this module only gathers what the engine already
holds.
"""

from __future__ import annotations

import logging
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
from .tech_lead_page_snapshot import TechLeadPageNotObserved

if TYPE_CHECKING:
    from ..contracts.ui_openapi_models import TechLeadPageSectionPayload
    from ..ports.engine_audit import NeedsHumanCauseAuditReader
    from ..ports.issue import Issue
    from .orchestrator import Orchestrator

logger = logging.getLogger(__name__)

#: Charter decisions read per page: the doing lane looks back one day, and the
#: triage lane keeps each item's latest class.
PAGE_DECISION_LIMIT = 300


def tech_lead_page_section(orchestrator: Orchestrator) -> "TechLeadPageSectionPayload":
    """This engine's section of the cross-repo Tech lead page.

    Built from the tick's latest publication, never under the state lock
    (#8988); raises :class:`TechLeadPageNotObserved` while there is none. The
    merge-held PRs' mergeability and checks are read here, through a cache that
    bounds them to a few reads every few minutes
    (:mod:`~..control.merge_hold_status`), so they never hold up a tick.
    """
    inputs = orchestrator.tech_lead_page.latest()
    held = [row.issue_number for row in inputs.needs_human_causes if row.cause in MERGE_HOLD_CAUSES]
    return build_tech_lead_page_section(
        replace(inputs, merge_statuses=orchestrator.deps.merge_hold_statuses.read(held))
    )


def publish_tech_lead_page(orchestrator: Orchestrator) -> None:
    """Capture what the page is built from and publish it (#8988).

    The tick calls this at the end of every cycle and the proposal command after
    a decision, each under the state lock it already holds (reentrant), so the
    capture sees one consistent state. A capture that fails withholds the page
    with its reason, which the page route answers as "not reporting": a page the
    engine cannot build must not take the tick down with it.
    """
    with orchestrator.state_lock:
        try:
            inputs = _capture(orchestrator)
        except TechLeadPageNotObserved as unknown:
            orchestrator.tech_lead_page.withhold(str(unknown))
        except Exception as error:  # the tick carries on; the page says it cannot answer
            logger.exception("[tech-lead] could not capture the Tech lead page")
            orchestrator.tech_lead_page.withhold(f"the page could not be captured: {error}")
        else:
            orchestrator.tech_lead_page.publish(inputs)


def _capture(orchestrator: Orchestrator) -> TechLeadPageInputs:
    """Everything the page reads from engine-held state; the caller holds the lock."""
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
    return TechLeadPageInputs(
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
        rulings=deps.standing_rulings.synced(),
        known_proposals=approvals.indexed_proposals() if approvals is not None else frozenset(),
        delivery=state.integration_delivery,
    )


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
    the scan interval. The page is republished before the lock is released,
    so the refresh that follows the decision reads it (#8988).
    """
    from ..control.tech_lead_approval_writes import apply_operator_proposal_command

    with orchestrator.state_lock:
        approvals = orchestrator.deps.action_applier.tech_lead_approvals
        ops = orchestrator.deps.services.tech_lead_authority
        if approvals is None or ops is None:
            raise RuntimeError("The tech-lead approval owner is not wired")
        outcome = apply_operator_proposal_command(
            command, repository=orchestrator.deps.repository_host, ops=ops, approvals=approvals,
            filtering_label=orchestrator.config.filtering.label,
        )
        if outcome.outcome == "approved":
            orchestrator.state.tech_lead_approval_scan_at = 0.0
        publish_tech_lead_page(orchestrator)
        return outcome
