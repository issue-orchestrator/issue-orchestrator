"""Read-only operator projection over the existing stored-op lifecycle."""

from __future__ import annotations

from dataclasses import asdict, dataclass

from ..domain.scoped_rework import ReworkRequest, ReworkReceipt
from ..domain.tech_lead_session import is_proposed_tech_lead_gate
from .scoped_rework import RequestReworkExecutor
from .scoped_rework_reuse import claimed_work_successor


@dataclass(frozen=True, slots=True)
class ReworkProposalView:
    proposal_issue_number: int
    repository: str
    pr_number: int
    issue_number: int
    expected_head: str
    evidence_identity: str
    feedback: str
    report: str
    status: str
    detail: str
    mutations: str
    can_approve: bool
    can_decline: bool
    forward_issue_number: int = 0

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def project_rework_proposals(
    owner: RequestReworkExecutor,
) -> tuple[ReworkProposalView, ...]:
    views: list[ReworkProposalView] = []
    represented: set[str] = set()
    for number, op in owner.receipts.list_ops():
        request = op.rework_request
        if request is None:
            continue
        proposal = owner.repository.get_issue(number)
        if proposal is None or proposal.state != "open":
            continue
        represented.add(request.key)
        pr = owner.repository.get_pr(request.target.pr_number)
        issue = owner.repository.get_issue(request.target.issue_number)
        stale = owner.stale_reason(request, pr, issue)
        gated = any(is_proposed_tech_lead_gate(label) for label in proposal.labels)
        status = "stale" if stale else "awaiting_approval" if gated else "approved"
        detail = (
            stale or "Awaiting approval"
            if gated
            else stale or "Approved; execution waits for the engine"
        )
        receipt = owner.receipts.load_rework_receipt(request.key)
        if receipt is not None:
            views.append(_receipt_view(owner, receipt, number))
        else:
            views.append(
                _view(request, number, status, detail, gated and not stale, True)
            )
    for receipt in owner.receipts.list_rework_receipts():
        request = receipt.request
        if request.key in represented:
            continue
        views.append(_receipt_view(owner, receipt, receipt.proposal_issue_number))
    return tuple(views)


def _receipt_view(owner: RequestReworkExecutor, receipt: ReworkReceipt, number: int) -> ReworkProposalView:
    status = receipt.status
    detail = receipt.detail or status.replace("_", " ").capitalize()
    if receipt.has_claimed_work:
        successor = claimed_work_successor(owner, receipt)
        if successor == "deferred":
            status, detail = "queued", "Provider deferred this work; the exact durable request is retained for retry"
        elif successor == "unavailable":
            status, detail = "unavailable", "No active run or applicable durable deferred claim owns this instruction"
    elif status == "queued" and owner.is_active(receipt.request.target.issue_number):
        detail = "Queued behind existing work; the next rework launch reads the approved instruction"
    return _view(receipt.request, number, status, detail, False, False, receipt.forward_issue_number)


def _view(
    request: ReworkRequest,
    number: int,
    status: str,
    detail: str,
    can_approve: bool,
    can_decline: bool,
    forward: int = 0,
) -> ReworkProposalView:
    target = request.target
    return ReworkProposalView(
        number,
        target.repository,
        target.pr_number,
        target.issue_number,
        target.head_sha,
        request.evidence_identity,
        request.feedback,
        request.report,
        status,
        detail,
        f"Preserve branch {target.branch}; invalidate code-reviewed and tech-lead-reviewed; queue normal rework. Clear the observed operator human block only if no independent cause holds it. A merged PR creates one forward fix.",
        can_approve,
        can_decline,
        forward,
    )
