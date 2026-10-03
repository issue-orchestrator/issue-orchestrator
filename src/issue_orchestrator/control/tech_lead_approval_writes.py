"""Every approval-label write the engine makes (#7763).

Two entry points, one owner (:class:`~.tech_lead_approval.TechLeadApprovals`):

* :func:`apply_settle_proposal_approval` — the applier boundary for the
  transitions a tick planned (admit, reject a claim that does not count,
  restore the waiting state). Each re-reads the issue fresh and re-checks
  before it writes.
* :func:`apply_operator_proposal_command` — the Control Center's Approve and
  Decline. Approve applies ``approved`` with the engine's credential and
  records the exact label event it produced as the operator's act, because
  the event's actor (the engine's own identity) cannot carry the operator's
  authority. Decline closes the proposal. Neither executes anything: the
  next tick's reconciliation and the consent-checked dispatcher do, under the
  same rules as an approval given on GitHub.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Callable, assert_never

from ..domain.scoped_rework import (
    TechLeadProposalCommand,
    TechLeadProposalCommandOutcome,
)
from ..domain.tech_lead_approval import (
    APPROVED_LABEL,
    AWAITING_APPROVAL_LABEL,
    GATED_PROPOSAL_LABELS,
    ApprovalTransition,
    OperatorApprovalRecord,
    ProposalLabelState,
    proposal_label_state,
    proposal_state,
)
from .actions import Action, ActionResult, SettleProposalApprovalAction
from .tech_lead_charter_lifecycle import link_declined_proposal

if TYPE_CHECKING:
    from ..ports import RepositoryHost
    from ..ports.issue import Issue
    from ..ports.tech_lead_authority import TechLeadAuthorityStore
    from .tech_lead_approval import TechLeadApprovals

logger = logging.getLogger(__name__)


def _labels_folded(issue: "Issue") -> set[str]:
    return {str(label).casefold() for label in issue.labels}


def apply_settle_proposal_approval(
    action: "Action",
    *,
    approvals: "TechLeadApprovals | None",
    repository: "RepositoryHost | None",
) -> ActionResult:
    """Apply one planned approval-label transition after a fresh re-check."""
    assert isinstance(action, SettleProposalApprovalAction)
    if approvals is None or repository is None:
        return ActionResult.fail(
            action,
            "approval settlement requires the approval owner and repository_host"
            " wired into this applier",
        )
    issue = repository.get_issue(action.issue_number)
    if issue is None or issue.state != "open":
        return ActionResult.ok(action, settled="closed")
    transition = action.transition
    assert transition is not None  # enforced by SettleProposalApprovalAction
    match transition:
        case ApprovalTransition.ADMIT:
            return _admit(action, issue, approvals, repository)
        case ApprovalTransition.REJECT_CLAIM:
            return _reject_claim(action, issue, approvals, repository)
        case ApprovalTransition.RESTORE_WAITING:
            return _restore_waiting(action, issue, repository)
        case _:
            assert_never(transition)


def _admit(
    action: "SettleProposalApprovalAction",
    issue: "Issue",
    approvals: "TechLeadApprovals",
    repository: "RepositoryHost",
) -> ActionResult:
    verdict = approvals.verify(issue, fresh=True)
    if not verdict.approved:
        return ActionResult.fail(
            action,
            f"#{issue.number} no longer carries a verified approval"
            f" ({verdict.describe()}); not admitted",
        )
    if AWAITING_APPROVAL_LABEL.casefold() in _labels_folded(issue):
        repository.remove_label(issue.number, AWAITING_APPROVAL_LABEL)
    repository.add_comment(
        issue.number,
        "## ✅ Approved\n\n"
        f"This tech-lead proposal was {verdict.describe()}. It now joins the"
        " work queue.",
    )
    logger.info(
        "[tech_lead] Proposal #%d admitted to the work queue (%s)",
        issue.number,
        verdict.describe(),
    )
    return ActionResult.ok(action, settled="admitted", approver=verdict.actor)


def _reject_claim(
    action: "SettleProposalApprovalAction",
    issue: "Issue",
    approvals: "TechLeadApprovals",
    repository: "RepositoryHost",
) -> ActionResult:
    verdict = approvals.verify(issue, fresh=True)
    if verdict.approved or not verdict.rejected_claim:
        return ActionResult.ok(action, settled="unchanged")
    folded = _labels_folded(issue)
    if AWAITING_APPROVAL_LABEL.casefold() not in folded:
        repository.add_label(issue.number, AWAITING_APPROVAL_LABEL)
    for label in issue.labels:
        if str(label).casefold() == APPROVED_LABEL.casefold():
            repository.remove_label(issue.number, label)
    repository.add_comment(
        issue.number,
        "## ⛔ Approval not accepted\n\n"
        f"The `{APPROVED_LABEL}` label was removed: {verdict.describe()}."
        " Only a repository maintainer (admin or maintain role) approves a"
        " tech-lead proposal, either by adding the label on GitHub or with"
        " Approve in the Control Center's Approvals inbox. This proposal is"
        " still awaiting approval.",
    )
    logger.warning(
        "[tech_lead] Rejected an approval claim on proposal #%d: %s",
        issue.number,
        verdict.describe(),
    )
    return ActionResult.ok(action, settled="claim_rejected", actor=verdict.actor)


def _restore_waiting(
    action: "SettleProposalApprovalAction",
    issue: "Issue",
    repository: "RepositoryHost",
) -> ActionResult:
    """Put back whichever gate labels a strip took (provenance, waiting, or both)."""
    if proposal_state(issue.labels, issue.body) is not ProposalLabelState.AWAITING:
        return ActionResult.ok(action, settled="unchanged")
    folded = _labels_folded(issue)
    restored = [label for label in GATED_PROPOSAL_LABELS if label.casefold() not in folded]
    for label in restored:
        repository.add_label(issue.number, label)
    return ActionResult.ok(action, settled="waiting_restored" if restored else "unchanged")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def apply_operator_proposal_command(
    command: TechLeadProposalCommand,
    *,
    repository: "RepositoryHost",
    ops: "TechLeadAuthorityStore",
    approvals: "TechLeadApprovals",
    now: Callable[[], str] = _utc_now,
) -> TechLeadProposalCommandOutcome:
    """The Control Center's Approve / Decline for ANY tech-lead proposal.

    The one command path for every proposal kind (act-level ops, decisions,
    follow-up issues, promoted findings). Approval is recorded, never
    executed here: the engine's next tick verifies it like any other and
    executes or admits exactly once.
    """
    number = command.proposal_issue_number
    try:
        issue = repository.get_issue(number)
        if issue is None or issue.state != "open":
            return TechLeadProposalCommandOutcome("unavailable", "Proposal is closed or missing", number)
        if not proposal_label_state(issue.labels).is_proposal:
            return TechLeadProposalCommandOutcome(
                "unavailable", "This issue is not a tech-lead proposal", number
            )
        started = _execution_started(ops, number)
        if started:
            return TechLeadProposalCommandOutcome("unavailable", started, number)
        if command.decision == "decline":
            return _decline(issue, repository=repository, ops=ops, approvals=approvals)
        return _approve(issue, repository=repository, approvals=approvals, now=now)
    except Exception as exc:
        logger.exception("[tech_lead] Proposal command on #%d failed", number)
        return TechLeadProposalCommandOutcome("failed", str(exc), number)


def _execution_started(ops: "TechLeadAuthorityStore", number: int) -> str:
    op = ops.load_op(issue_number=number)
    if op is None or op.rework_request is None:
        return ""
    if ops.load_rework_receipt(op.rework_request.key) is not None:
        return "Execution has started; the recorded outcome is authoritative"
    return ""


def _decline(
    issue: "Issue",
    *,
    repository: "RepositoryHost",
    ops: "TechLeadAuthorityStore",
    approvals: "TechLeadApprovals",
) -> TechLeadProposalCommandOutcome:
    number = issue.number
    repository.add_comment(
        number,
        "## ✖️ Declined\n\nThe operator declined this tech-lead proposal in the"
        " Control Center. Closing it; nothing was executed.",
    )
    repository.update_issue_state(number, "closed")
    approvals.records.discard_operator_approval(number)
    approvals.forget_from_scope(number)
    if ops.load_op(issue_number=number) is not None:
        link_declined_proposal(ops, number)
        ops.discard_op(issue_number=number)
    return TechLeadProposalCommandOutcome("declined", "Proposal declined and closed", number)


def _approve(
    issue: "Issue",
    *,
    repository: "RepositoryHost",
    approvals: "TechLeadApprovals",
    now: Callable[[], str],
) -> TechLeadProposalCommandOutcome:
    number = issue.number
    if approvals.verify(issue, fresh=True).approved:
        return TechLeadProposalCommandOutcome(
            "approved", "Already approved; the engine will act on it", number
        )
    # An `approved` label that does not count (a bot's) would make our add a
    # GitHub no-op with no new event to bind to, so take it off first.
    for label in issue.labels:
        if str(label).casefold() == APPROVED_LABEL.casefold():
            repository.remove_label(number, label)
    repository.add_label(number, APPROVED_LABEL)
    event = approvals.evidence.latest_label_event(number, APPROVED_LABEL)
    if event is None:
        raise RuntimeError(
            f"applied {APPROVED_LABEL!r} to #{number} but GitHub shows no labeled"
            " event to bind the approval to"
        )
    if approvals.evidence.is_own_write(event):
        # The engine's App wrote it: that exact event is the operator's act.
        approvals.records.record_operator_approval(
            OperatorApprovalRecord(number, event.event_id, now())
        )
    elif not approvals.verify(repository.get_issue(number) or issue, fresh=True).approved:
        # A personal-token engine writes as its user, whose label is judged
        # like anyone's; and an event someone else's write produced (a relabel
        # racing ours) is never bound to the operator (#7763 review F3).
        raise RuntimeError(
            f"the {APPROVED_LABEL!r} label on #{number} cannot be attributed to this"
            " engine's write and is not a maintainer's; approval not recorded"
        )
    repository.add_comment(
        number,
        "## 👍 Approved in the Control Center\n\nThe operator approved this"
        " tech-lead proposal. The engine re-validates its preconditions and"
        " acts on it exactly once.",
    )
    return TechLeadProposalCommandOutcome(
        "approved",
        "Approval recorded; the engine will re-validate and act on it",
        number,
    )


__all__ = [
    "apply_operator_proposal_command",
    "apply_settle_proposal_approval",
]
