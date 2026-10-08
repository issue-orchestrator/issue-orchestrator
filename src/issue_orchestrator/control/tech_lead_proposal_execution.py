"""Consent checking and terminal finalization for approved tech-lead proposals."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Callable, TypeVar

from .actions import (
    ActionResult,
    ApplyOperatorDecisionAction,
    KillHungSessionAction,
    RecoverValidatedWorkAction,
    ReleaseWithheldReviewAction,
    RequestReworkAction,
    ResetRetryIssueAction,
    ResolveBlockAction,
)
from .claim_gate import ClaimLostError
from .reconciliation import ReconciliationRequired
from .tech_lead_charter_lifecycle import link_approved_proposal
from .tech_lead_reset_retry import STALE_DOWNGRADE_MODE

if TYPE_CHECKING:
    from ..ports import RepositoryHost
    from ..ports.tech_lead_authority import TechLeadAuthorityStore
    from .tech_lead_approval import TechLeadApprovals

logger = logging.getLogger(__name__)

_TechLeadOpAction = TypeVar(
    "_TechLeadOpAction",
    ResetRetryIssueAction,
    ResolveBlockAction,
    KillHungSessionAction,
    RequestReworkAction,
    RecoverValidatedWorkAction,
    ReleaseWithheldReviewAction,
    ApplyOperatorDecisionAction,
    ResolveBlockAction,
)


def _terminal_outcome_comment(
    result: ActionResult, op_type: str, target: int
) -> str | None:
    if result.success:
        # A decision step refused at write time, after the item moved (#8691):
        # said here, so "executed" never hides a step that did not run.
        refused = "".join(f"\n- Not applied: {item}" for item in result.details.get("steps_refused") or ())
        return (
            "## ✅ Approved tech_lead operation executed\n\n"
            f"`{op_type}` for #{target} was executed after re-validating its"
            f" preconditions.{refused and chr(10) + chr(10) + 'Refused at write time (do these by hand):' + refused}"
            " Closing this proposal."
        )
    if result.details.get("mode") == STALE_DOWNGRADE_MODE:
        stale = result.details.get("skip_reason", "preconditions no longer hold")
        return (
            "## ⏸️ Preconditions no longer hold\n\n"
            f"`{op_type}` for #{target} was approved, but re-validation found"
            f" the recorded preconditions stale: {stale}\n\n"
            "No changes were made. Closing this proposal."
        )
    return None


def finalize_tech_lead_op_execution(
    result: ActionResult,
    action: _TechLeadOpAction,
    *,
    repository_host: RepositoryHost | None,
    ops: TechLeadAuthorityStore | None,
    before_finalize_write: Callable[[], None] | None = None,
) -> ActionResult:
    """Finalize a terminal proposal result; preserve loud failures for retry."""
    proposal_issue = action.proposal_issue_number
    if not proposal_issue:
        return result
    op_type = action.op_type
    comment = _terminal_outcome_comment(result, op_type, action.issue_number)
    if comment is None:
        return result
    if repository_host is None or ops is None:
        return ActionResult.fail(
            action,
            "tech_lead proposal finalization requires repository_host and the"
            " TechLeadAuthorityStore wired into this applier",
        )
    try:
        # Link the outcome BEFORE the proposal closes: a closed proposal whose
        # op row survives is what terminal cleanup reads as "declined", so the
        # approval must already be on the record by then (#7330 review r2 F1).
        link_approved_proposal(ops, proposal_issue, applied=result.success)
        if before_finalize_write is not None:
            before_finalize_write()
        repository_host.add_comment(proposal_issue, comment)
        if before_finalize_write is not None:
            before_finalize_write()
        repository_host.update_issue_state(proposal_issue, "closed")
        ops.discard_op(issue_number=proposal_issue)
    except (ClaimLostError, ReconciliationRequired):
        raise
    except Exception as error:
        logger.exception(
            "Failed to finalize tech_lead proposal #%d after %s",
            proposal_issue,
            op_type,
        )
        return ActionResult.fail(
            action,
            f"op outcome reached but proposal #{proposal_issue} finalization"
            f" failed: {error}",
            proposal_issue_number=proposal_issue,
        )
    logger.info(
        "[tech_lead] Proposal #%d finalized (%s, success=%s)",
        proposal_issue,
        op_type,
        result.success,
    )
    return result


def _approval_confirmed(
    repository_host: RepositoryHost, approvals: "TechLeadApprovals", proposal_issue: int
) -> bool:
    """True only when a FRESH read shows the proposal open and maintainer-approved.

    The approval owner re-reads the standing ``approved`` label's events here,
    bypassing its cache (#7763): an approval removed, re-applied by a bot, or
    stripped since the tick planned the execution withholds it.
    """
    try:
        return approvals.confirm(repository_host.get_issue(proposal_issue))
    except Exception:
        logger.exception(
            "[tech_lead] Fresh consent read for proposal #%d failed; treating"
            " approval as unconfirmed and preserving the op (#6779 R16)",
            proposal_issue,
        )
        return False


def _withheld_for_withdrawn_approval(
    action: _TechLeadOpAction,
    repository_host: RepositoryHost | None,
    approvals: "TechLeadApprovals | None",
) -> ActionResult | None:
    proposal_issue = action.proposal_issue_number
    if not proposal_issue:
        return None
    if repository_host is None or approvals is None:
        return ActionResult.fail(
            action,
            "approved tech_lead op consent re-check requires repository_host"
            " and the approval owner wired into this applier",
        )
    if _approval_confirmed(repository_host, approvals, proposal_issue):
        return None
    logger.info(
        "[tech_lead] Proposal #%d no longer confirms operator approval before"
        " apply (unapproved, closed, or unreadable): preserving its op inert"
        " (#6779 R16)",
        proposal_issue,
    )
    return ActionResult.fail(
        action,
        f"proposal #{proposal_issue} no longer confirms operator approval;"
        " op preserved inert",
        proposal_issue_number=proposal_issue,
    )


def execute_approved_tech_lead_op(
    action: _TechLeadOpAction,
    apply_fn: Callable[[_TechLeadOpAction], ActionResult],
    *,
    repository_host: RepositoryHost | None,
    ops: TechLeadAuthorityStore | None,
    approvals: "TechLeadApprovals | None",
    before_finalize_write: Callable[[], None] | None = None,
) -> ActionResult:
    """Reconfirm per-instance consent immediately before executing and finalizing."""
    inert = _withheld_for_withdrawn_approval(action, repository_host, approvals)
    if inert is not None:
        return inert
    return finalize_tech_lead_op_execution(
        apply_fn(action),
        action,
        repository_host=repository_host,
        ops=ops,
        before_finalize_write=before_finalize_write,
    )
