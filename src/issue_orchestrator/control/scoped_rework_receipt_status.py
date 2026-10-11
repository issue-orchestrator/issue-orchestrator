"""What an approved scoped rework is doing now, from its durable receipt (#7763).

The Tech lead page's request_rework card (and its receipt-only rows) show this.
Kept from the retired rework-proposal panel's projection: the receipt's own
status, refined by the claimed-work successor proof when a run consumed the
instruction, or by whether the target is busy when it is still queued. Local
state only, except the successor proof's own bounded re-check of a deferred
claim (one PR and one issue read, only for a receipt in that state).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..domain.scoped_rework import ReworkReceipt
from .scoped_rework_reuse import claimed_work_successor

if TYPE_CHECKING:
    from .scoped_rework import RequestReworkExecutor

DEFERRED_DETAIL = "Provider deferred this work; the exact durable request is retained for retry"
UNAVAILABLE_DETAIL = "No active run or applicable durable deferred claim owns this instruction"
QUEUED_BEHIND_DETAIL = "Queued behind existing work; the next rework launch reads the approved instruction"


def rework_receipt_status(
    owner: "RequestReworkExecutor", receipt: ReworkReceipt
) -> tuple[str, str]:
    """``(status, detail)`` for one receipt."""
    status = receipt.status
    detail = receipt.detail or status.replace("_", " ").capitalize()
    if receipt.has_claimed_work:
        successor = claimed_work_successor(owner, receipt)
        if successor == "deferred":
            return "queued", DEFERRED_DETAIL
        if successor == "unavailable":
            return "unavailable", UNAVAILABLE_DETAIL
    elif status == "queued" and owner.is_active(receipt.request.target.issue_number):
        return status, QUEUED_BEHIND_DETAIL
    return status, detail


__all__ = ["rework_receipt_status"]
