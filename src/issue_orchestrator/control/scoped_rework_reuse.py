"""Live successor proof for the shared mandatory proposal-reuse command."""

from __future__ import annotations
from typing import TYPE_CHECKING, Literal
from ..domain.scoped_rework import ReworkReceipt

if TYPE_CHECKING:
    from .scoped_rework import RequestReworkExecutor


def validate_receipt_owner(
    owner: RequestReworkExecutor, receipt: ReworkReceipt
) -> None:
    target = receipt.request.target
    if receipt.has_completed_work:
        if receipt.attempt is None:
            raise ValueError("completed rework has no consuming run identity")
        return
    if receipt.has_forward_work:
        if owner.repository.get_issue_state(receipt.forward_issue_number) != "open":
            raise ValueError("forward-fix successor no longer owns runnable work")
        return
    if receipt.attempt is not None:
        if claimed_work_successor(owner, receipt) == "unavailable":
            raise ValueError("the exact consuming rework run is not active and no applicable durable deferred successor exists")
        return
    pr = owner.repository.get_pr(target.pr_number)
    issue = owner.repository.get_issue(target.issue_number)
    stale = owner.stale_reason(receipt.request, pr, issue)
    if (
        stale
        or not receipt.has_queued_work
        or pr is None
        or pr.state != "open"
        or owner.labels.needs_rework not in pr.labels
    ):
        raise ValueError(stale or "scoped request has no current queued rework owner")


def claimed_work_successor(
    owner: RequestReworkExecutor, receipt: ReworkReceipt,
) -> Literal["active", "deferred", "unavailable"]:
    """Shared consuming-work proof for mandatory reuse and operator projection."""
    target = receipt.request.target
    if not receipt.has_claimed_work or receipt.attempt is None:
        return "unavailable"
    if owner.is_attempt_active is not None and owner.is_attempt_active(target.issue_number, receipt.attempt):
        return "active"
    if owner.pending_successors.owns_scoped(receipt.request):
        stale = owner.stale_reason(receipt.request,
            owner.repository.get_pr(target.pr_number), owner.repository.get_issue(target.issue_number))
        if not stale:
            return "deferred"
    return "unavailable"
